"""Alerts settings API — read/write user alert config and send test notifications."""

from __future__ import annotations

import os
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel, Field

import src.alerts.alert_storage as alert_storage
import src.alerts.alert_worker as alert_worker
import src.alerts.delivery_status as delivery_status
import src.alerts.user_alert_storage as user_alert_storage
from dashboard.backend.api.history import build_symbol_catalog
from dashboard.backend.auth import require_user_id
from dashboard.backend.services.data_loader import get_data_loader
from src.alerts.alert_paths import (
    alert_rows,
    init_minimal_user_alerts_config,
    load_alerts_config,
    polish_alerts_config,
    save_alerts_config,
    strip_webhook_secrets_from_config,
    update_user_env_vars,
    user_alerts_config_path,
    user_config_dir,
)
from src.alerts.notifiers.email_delivery import email_delivery_configured
from src.alerts.symbol_prices import (
    prices_from_saved_daily_data,
    resolve_symbol_prices,
    saved_quote_details,
)
from src.cli.alerts_commands import _load_env, run_alert_test
from src.storage.alert_watches import InvalidAlertWatchConfig, validate_watches_config
from src.storage.database import database_enabled
from src.storage.user_alerts import (
    init_user_alerts_config,
    load_user_alerts_config,
    save_user_alerts_config,
)
from src.utils.tickers import normalize_ticker

router = APIRouter()


class AlertsStatusResponse(BaseModel):
    checks_on_fetch: bool = True
    last_data_date: Optional[str] = None
    tracked_symbols: List[str] = Field(default_factory=list)
    active_watches: int = 0
    last_triggered_at: Optional[str] = None
    latest_deliveries: List[Dict[str, Any]] = Field(default_factory=list)


class AlertsRunResponse(BaseModel):
    triggered: int
    last_data_date: Optional[str] = None
    events: List[Dict[str, Any]] = Field(default_factory=list)
    message: Optional[str] = None


class AlertDefaults(BaseModel):
    email_to: Optional[str] = None
    webhook_url: Optional[str] = None  # write-only: saved to ~/.markethelm/.env, never returned
    webhook_format: Optional[str] = None
    notify_email: Optional[bool] = None
    notify_webhook: Optional[bool] = None


class AlertsConfigBody(BaseModel):
    defaults: Optional[AlertDefaults] = None
    alerts: List[Dict[str, Any]] = Field(default_factory=list)


class ChannelStatus(BaseModel):
    email_smtp: bool
    email_recipients: bool
    webhook_url: bool


class AlertsConfigResponse(BaseModel):
    exists: bool
    config: AlertsConfigBody
    channels: ChannelStatus


class AlertInitResponse(BaseModel):
    message: str


class AlertTestRequest(BaseModel):
    id: str
    dry_run: bool = False


class AlertTestResponse(BaseModel):
    alert_id: str
    status: str
    notifiers: List[str]
    previews: Optional[List[Dict[str, Any]]] = None


class SymbolQuotesRequest(BaseModel):
    symbols: List[str] = Field(default_factory=list)


class SymbolQuotesResponse(BaseModel):
    prices: Dict[str, float] = Field(default_factory=dict)
    quote_meta: Dict[str, Dict[str, Optional[str]]] = Field(default_factory=dict)


def _quote_meta(prices: Dict[str, float]) -> Dict[str, Dict[str, Optional[str]]]:
    """Identify saved prices without claiming that a lookup returned a fresh trade."""
    saved = saved_quote_details()
    retrieved_at = datetime.now(timezone.utc).isoformat()
    return {
        symbol: {
            "source": "saved" if symbol in saved and saved[symbol]["price"] == price else "lookup",
            "as_of": (
                str(saved[symbol]["as_of"])
                if symbol in saved and saved[symbol]["price"] == price
                else None
            ),
            "retrieved_at": (
                None if symbol in saved and saved[symbol]["price"] == price else retrieved_at
            ),
        }
        for symbol, price in prices.items()
    }


def _empty_config() -> Dict[str, Any]:
    return {"defaults": {}, "alerts": []}


def _normalize_config(raw: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    if not raw:
        return _empty_config()
    alerts = raw.get("alerts")
    if not isinstance(alerts, list):
        raise HTTPException(status_code=400, detail="Config must include an 'alerts' array.")
    defaults = dict(raw.get("defaults") or {})
    webhook_format = defaults.get("webhook_format")
    if isinstance(webhook_format, str):
        defaults["webhook_format"] = webhook_format.strip().lower()
    return {"defaults": defaults, "alerts": alerts}


def _has_webhook_secret(config: Dict[str, Any]) -> bool:
    defaults = config.get("defaults") or {}
    if str(defaults.get("webhook_url") or "").strip():
        return True
    return any(
        isinstance(alert, dict) and str(alert.get("webhook_url") or "").strip()
        for alert in config.get("alerts") or []
    )


def _channel_status(
    config: Dict[str, Any],
    *,
    has_webhook_secret: Optional[bool] = None,
) -> ChannelStatus:
    defaults = config.get("defaults") or {}
    has_rule_email = any(alert.get("email_to") for alert in config.get("alerts", []))
    # Hosted (DB) mode must not treat process-wide ALERT_EMAIL_TO as this tenant's
    # recipients — same isolation rule as webhook env vars below.
    has_default_email = bool(defaults.get("email_to"))
    if not database_enabled():
        has_default_email = has_default_email or bool(os.environ.get("ALERT_EMAIL_TO"))
    if database_enabled():
        webhook_ready = bool(has_webhook_secret)
    else:
        webhook_ready = bool(
            has_webhook_secret if has_webhook_secret is not None else _has_webhook_secret(config)
        ) or bool(os.environ.get("ALERT_WEBHOOK_URL") or os.environ.get("DISCORD_WEBHOOK_URL"))
    return ChannelStatus(
        email_smtp=email_delivery_configured(),
        email_recipients=has_default_email or has_rule_email,
        webhook_url=webhook_ready,
    )


def _public_config(config: Dict[str, Any]) -> Dict[str, Any]:
    """Config safe to send to the browser — no webhook URLs."""
    return strip_webhook_secrets_from_config(config)


def _reject_env_control_chars(label: str, value: str) -> str:
    """Strip is not enough — CR/LF in webhook secrets would inject .env lines."""
    cleaned = value.strip()
    if any(ch in cleaned for ch in ("\r", "\n", "\0")):
        raise HTTPException(
            status_code=400,
            detail=f"{label} contains invalid control characters.",
        )
    return cleaned


def _persist_webhook_secret(defaults: Optional[AlertDefaults]) -> None:
    if defaults is None:
        return
    updates: Dict[str, str] = {}
    if defaults.webhook_url and defaults.webhook_url.strip():
        updates["DISCORD_WEBHOOK_URL"] = _reject_env_control_chars(
            "Webhook URL", defaults.webhook_url
        )
    if defaults.webhook_format and defaults.webhook_format.strip():
        updates["ALERT_WEBHOOK_FORMAT"] = _reject_env_control_chars(
            "Webhook format", defaults.webhook_format
        ).lower()
    if updates:
        try:
            update_user_env_vars(updates)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc


def _load_raw_config(user_id: Optional[str]) -> tuple[bool, Optional[Dict[str, Any]]]:
    if database_enabled():
        if not user_id:
            raise HTTPException(status_code=401, detail="Authentication required.")
        return load_user_alerts_config(user_id)
    path = user_alerts_config_path()
    if not path.exists():
        return False, None
    _, raw = load_alerts_config(path)
    return raw is not None, raw


def _save_raw_config(user_id: Optional[str], config: Dict[str, Any]) -> None:
    if database_enabled():
        if not user_id:
            raise HTTPException(status_code=401, detail="Authentication required.")
        try:
            save_user_alerts_config(user_id, config)
        except InvalidAlertWatchConfig as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return
    # File mode previously skipped watch validation — negative / huge cooldown
    # (and other InvalidAlertWatchConfig rules) could persist and fire every tick
    # or OverflowError the evaluator. Reuse the same coerce path as hosted.
    try:
        validate_watches_config("file-mode", config)
    except InvalidAlertWatchConfig as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    save_alerts_config(config)


@router.get("/config", response_model=AlertsConfigResponse)
async def get_alerts_config(
    user_id: Optional[str] = Depends(require_user_id),
) -> AlertsConfigResponse:
    """Return the user alerts config and channel readiness (no secrets)."""
    _load_env()
    exists, raw = _load_raw_config(user_id)
    if raw is not None:
        raw = polish_alerts_config(raw, seed_env_email=not database_enabled())
        if not database_enabled():
            scrubbed = strip_webhook_secrets_from_config(raw)
            if scrubbed != raw:
                save_alerts_config(scrubbed)
                raw = scrubbed
    has_webhook_secret = _has_webhook_secret(raw or {})
    config = _public_config(_normalize_config(raw))
    defaults = dict(config.get("defaults") or {})
    # File-mode only: surface process-wide format defaults in the UI. Hosted
    # tenants must not inherit ALERT_WEBHOOK_FORMAT / DISCORD_WEBHOOK_URL —
    # that leaks into GET responses and can be persisted on the next PUT.
    if not defaults.get("webhook_format") and not database_enabled():
        env_format = (os.environ.get("ALERT_WEBHOOK_FORMAT") or "").strip().lower()
        if env_format in {"discord", "slack", "json"}:
            defaults["webhook_format"] = env_format
        elif os.environ.get("DISCORD_WEBHOOK_URL"):
            defaults["webhook_format"] = "discord"
    config["defaults"] = defaults
    return AlertsConfigResponse(
        exists=exists,
        config=AlertsConfigBody(**config),
        channels=_channel_status(config, has_webhook_secret=has_webhook_secret),
    )


@router.put("/config", response_model=AlertsConfigResponse)
async def put_alerts_config(
    body: AlertsConfigBody,
    user_id: Optional[str] = Depends(require_user_id),
) -> AlertsConfigResponse:
    """Save alerts config to the user config path."""
    _load_env()
    if not database_enabled():
        _persist_webhook_secret(body.defaults)
    _load_env()
    config = _normalize_config(body.model_dump())
    seen_ids: set[str] = set()
    for alert in config["alerts"]:
        # Watch sync strips ids; reject whitespace-only before a silent drop.
        alert_id = str(alert.get("id") or "").strip()
        if not alert_id:
            raise HTTPException(status_code=400, detail="Each alert must have an id.")
        # Hosted sync PK is (user_id, alert_id); catch dupes before IntegrityError 500.
        if alert_id in seen_ids:
            raise HTTPException(
                status_code=400,
                detail=f"Duplicate alert id '{alert_id}'.",
            )
        seen_ids.add(alert_id)
        alert["id"] = alert_id
        condition = alert.get("condition") or {}
        if isinstance(condition, dict) and condition.get("type") in {
            "price_threshold",
            "rsi_threshold",
        }:
            # File mode never hits InvalidAlertWatchConfig; reject blank/sentinel
            # symbols and missing operators here so both modes fail closed.
            symbol = normalize_ticker(condition.get("symbol"))
            if not symbol:
                raise HTTPException(
                    status_code=400,
                    detail=f"Alert '{alert_id}' must have a valid symbol.",
                )
            condition["symbol"] = symbol
            raw_operator = condition.get("operator")
            if raw_operator is None or not str(raw_operator).strip():
                raise HTTPException(
                    status_code=400,
                    detail=f"Alert '{alert_id}' must have an operator.",
                )
            condition["operator"] = str(raw_operator).strip()
            if condition.get("type") == "rsi_threshold":
                period = condition.get("period", 14)
                try:
                    period_n = int(period)
                except (TypeError, ValueError) as exc:
                    raise HTTPException(
                        status_code=400,
                        detail=f"Alert '{alert_id}' has an invalid RSI period.",
                    ) from exc
                if period_n < 2 or period_n > 50:
                    raise HTTPException(
                        status_code=400,
                        detail=f"Alert '{alert_id}' has an invalid RSI period.",
                    )
                condition["period"] = period_n
            alert["condition"] = condition
        elif isinstance(condition, dict) and condition.get("type") == "compound":
            # Hosted validate_watches_config covers this; file mode needs a light gate.
            op = str(condition.get("op") or "").strip().lower()
            leaves = condition.get("conditions")
            if op not in {"and", "or"} or not isinstance(leaves, list) or not leaves:
                raise HTTPException(
                    status_code=400,
                    detail=(
                        f"Alert '{alert_id}' compound rule must use op "
                        "'and' or 'or' with conditions."
                    ),
                )
            alert["condition"] = condition
    _save_raw_config(user_id, config)
    status_source = config
    if database_enabled() and user_id:
        _, saved = load_user_alerts_config(user_id)
        if saved is not None:
            status_source = saved
    status_source = polish_alerts_config(status_source, seed_env_email=not database_enabled())
    has_webhook_secret = _has_webhook_secret(status_source)
    public = _public_config(status_source)
    return AlertsConfigResponse(
        exists=True,
        config=AlertsConfigBody(**public),
        channels=_channel_status(public, has_webhook_secret=has_webhook_secret),
    )


@router.post("/init", response_model=AlertInitResponse)
async def post_alerts_init(
    force: bool = False,
    user_id: Optional[str] = Depends(require_user_id),
) -> AlertInitResponse:
    """Create an empty alerts config for onboarding."""
    _load_env()
    if database_enabled():
        if not user_id:
            raise HTTPException(status_code=401, detail="Authentication required.")
        try:
            init_user_alerts_config(user_id, force=force)
        except FileExistsError:
            raise HTTPException(
                status_code=409,
                detail="Alerts config already exists for this user. Pass ?force=true to overwrite.",
            )
        return AlertInitResponse(
            message="Alerts config created. Edit rules in Settings and send a test notification.",
        )
    try:
        init_minimal_user_alerts_config(force=force)
    except FileExistsError:
        raise HTTPException(
            status_code=409,
            detail=f"{user_config_dir() / 'alerts.json'} already exists. Pass ?force=true to "
            "overwrite.",
        )
    except FileNotFoundError as exc:
        raise HTTPException(status_code=500, detail=str(exc))
    return AlertInitResponse(
        message="Alerts config created. Edit rules in Settings and send a test notification.",
    )


@router.get("/health")
async def alerts_health() -> Dict[str, bool]:
    """Lightweight probe so the UI can detect a stale dashboard backend."""
    _load_env()
    return {
        "ok": True,
        "quotes": True,
        "live_quotes_configured": bool(os.environ.get("FINNHUB_API_KEY")),
    }


@router.get("/symbols")
async def get_alert_symbol_catalog(
    response: Response,
    _user_id: Optional[str] = Depends(require_user_id),
):
    """Searchable company list for alert setup (major US indices + tracked symbols).

    Hosted mode requires auth so anonymous clients cannot scrape the catalog /
    tracked-symbol metadata. File mode (``require_user_id`` → ``None``) stays open.
    """
    response.headers["Cache-Control"] = "private, max-age=300"
    symbols, names = build_symbol_catalog()

    tracked: List[str] = []
    saved_prices = prices_from_saved_daily_data()
    saved_meta = _quote_meta(saved_prices)
    try:
        loader = get_data_loader()
        df = loader.load_projections()
        if not df.empty and "symbol" in df.columns:
            tracked = sorted(
                {key for key in (normalize_ticker(s) for s in df["symbol"].unique()) if key}
            )
    except (ValueError, Exception):
        pass
    return {
        "symbols": symbols,
        "names": names,
        "count": len(symbols),
        "tracked_symbols": tracked,
        "prices": {symbol: saved_prices[symbol] for symbol in symbols if symbol in saved_prices},
        "quote_meta": {symbol: saved_meta[symbol] for symbol in symbols if symbol in saved_meta},
    }


@router.get("/quotes", response_model=SymbolQuotesResponse)
async def get_symbol_quotes(
    symbols: str = Query("", description="Comma-separated tickers"),
    _user_id: Optional[str] = Depends(require_user_id),
) -> SymbolQuotesResponse:
    """Latest prices for up to 15 symbols (saved data, then live quotes).

    Hosted mode requires auth so anonymous clients cannot burn the shared
    Finnhub quote budget. File mode (``require_user_id`` → ``None``) stays open.
    """
    _load_env()
    parsed = [key for key in (normalize_ticker(symbol) for symbol in symbols.split(",")) if key][
        :15
    ]
    if not parsed:
        return SymbolQuotesResponse(prices={})
    prices = await run_in_threadpool(resolve_symbol_prices, parsed, fetch_missing=True)
    return SymbolQuotesResponse(prices=prices, quote_meta=_quote_meta(prices))


@router.post("/quotes", response_model=SymbolQuotesResponse)
async def post_symbol_quotes(
    body: SymbolQuotesRequest,
    _user_id: Optional[str] = Depends(require_user_id),
) -> SymbolQuotesResponse:
    """Latest prices for up to 15 symbols (saved data, then live quotes).

    Hosted mode requires auth so anonymous clients cannot burn the shared
    Finnhub quote budget. File mode (``require_user_id`` → ``None``) stays open.
    """
    _load_env()
    symbols = [key for key in (normalize_ticker(symbol) for symbol in body.symbols) if key][:15]
    if not symbols:
        return SymbolQuotesResponse(prices={})
    prices = await run_in_threadpool(resolve_symbol_prices, symbols, fetch_missing=True)
    return SymbolQuotesResponse(prices=prices, quote_meta=_quote_meta(prices))


@router.get("/status", response_model=AlertsStatusResponse)
async def get_alerts_status(
    user_id: Optional[str] = Depends(require_user_id),
) -> AlertsStatusResponse:
    """Alert status: active watches, last data date, and last trigger time."""
    _load_env()
    tracked: List[str] = []
    last_date: Optional[str] = None
    active_watches = 0
    last_triggered: Optional[str] = None
    latest_deliveries: List[Dict[str, Any]] = []

    try:
        loader = get_data_loader()
        last_date = loader.get_latest_date()
        df = loader.load_projections()
        if not df.empty and "symbol" in df.columns:
            tracked = sorted(
                {key for key in (normalize_ticker(s) for s in df["symbol"].unique()) if key}
            )
    except (ValueError, Exception):
        pass

    path = user_alerts_config_path()
    if database_enabled():
        if user_id:
            _, raw = load_user_alerts_config(user_id)
            if raw:
                alerts = alert_rows(raw)
                active_watches = sum(
                    1 for alert in alerts if isinstance(alert, dict) and alert.get("enabled")
                )
    elif path.exists():
        _, raw = load_alerts_config(path)
        if raw:
            alerts = polish_alerts_config(raw).get("alerts") or []
            active_watches = sum(
                1 for alert in alerts if isinstance(alert, dict) and alert.get("enabled")
            )

    try:
        if database_enabled() and user_id:
            storage = user_alert_storage.UserAlertStorage(user_id)
            last_triggered = storage.latest_event_timestamp()
            latest_deliveries = storage.latest_delivery_by_channel()
        else:

            storage = alert_storage.AlertStorage()
            last_triggered = storage.latest_event_timestamp()
            latest_deliveries = delivery_status.latest_deliveries_by_channel(storage)
    except Exception:
        pass

    return AlertsStatusResponse(
        checks_on_fetch=True,
        last_data_date=last_date,
        tracked_symbols=tracked,
        active_watches=active_watches,
        last_triggered_at=last_triggered,
        latest_deliveries=latest_deliveries,
    )


@router.post("/run", response_model=AlertsRunResponse)
async def run_alerts_now(
    user_id: Optional[str] = Depends(require_user_id),
) -> AlertsRunResponse:
    """Evaluate active alerts against saved data and live quotes for watch symbols."""
    try:
        raw = alert_worker.run_user_check(user_id) if user_id else alert_worker.run_check_once()
    except Exception:
        raise HTTPException(status_code=500, detail="Alert check failed.")
    if raw.get("message") == "No market data available.":
        raise HTTPException(
            status_code=404,
            detail="No market data available. Add a watch or fetch dashboard data first.",
        )
    return AlertsRunResponse(
        triggered=raw["triggered"],
        last_data_date=raw.get("last_data_date"),
        events=raw.get("events") or [],
        message=raw.get("message"),
    )


@router.post("/test", response_model=AlertTestResponse)
async def post_alert_test(
    body: AlertTestRequest,
    user_id: Optional[str] = Depends(require_user_id),
) -> AlertTestResponse:
    """Send a test notification for one alert rule."""
    _load_env()
    config_payload: Optional[Dict[str, Any]] = None
    storage = None
    if database_enabled():
        if not user_id:
            raise HTTPException(status_code=401, detail="Authentication required.")
        exists, raw = load_user_alerts_config(user_id)
        if not exists or raw is None:
            raise HTTPException(status_code=404, detail="No alerts config for this user.")
        config_payload = raw

        storage = user_alert_storage.UserAlertStorage(user_id)
    try:
        result = run_alert_test(
            body.id,
            dry_run=body.dry_run,
            config=config_payload,
            storage=storage,
        )
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except RuntimeError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return AlertTestResponse(
        alert_id=result["alert_id"],
        status=result["status"],
        notifiers=result.get("notifiers") or [],
        previews=result.get("previews"),
    )

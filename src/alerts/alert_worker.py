"""Scheduled alert evaluation worker (cron, Task Scheduler, systemd, or long-running process)."""

from __future__ import annotations

import math
import os
import signal
import time
import uuid
from typing import Any, Callable, Dict, List, Optional

from src.core.logger import get_logger

logger = get_logger(__name__)

DEFAULT_INTERVAL_SECONDS = 300
MIN_INTERVAL_SECONDS = 60
# Hard ceiling so a poisoned interval cannot sleep the worker loop "forever"
# (e.g. 1e18) and silently stop alert evaluation after the first tick.
MAX_INTERVAL_SECONDS = 86400
_PROCESS_WORKER_ID = f"worker-{uuid.uuid4().hex[:12]}"


def resolve_interval_seconds(explicit: Optional[int] = None) -> int:
    """Seconds between checks when running in loop mode."""
    if explicit is not None:
        return min(MAX_INTERVAL_SECONDS, max(MIN_INTERVAL_SECONDS, explicit))
    raw = os.environ.get("ALERT_CHECK_INTERVAL_SECONDS", "").strip()
    if raw:
        try:
            return min(MAX_INTERVAL_SECONDS, max(MIN_INTERVAL_SECONDS, int(raw)))
        except ValueError:
            logger.warning(
                "Invalid ALERT_CHECK_INTERVAL_SECONDS=%r; using default %ss",
                raw,
                DEFAULT_INTERVAL_SECONDS,
            )
    return DEFAULT_INTERVAL_SECONDS


def run_check_once() -> Dict[str, Any]:
    from src.storage.database import database_enabled

    if database_enabled():
        return run_db_worker_cycle()

    from src.alerts.alert_runner import evaluate_alerts_from_latest_data

    return evaluate_alerts_from_latest_data()


def run_user_check(user_id: str) -> Dict[str, Any]:
    """Evaluate and deliver only one hosted user's configured alerts."""
    from src.alerts.alert_engine import AlertEngine
    from src.alerts.market_snapshot import load_market_snapshot
    from src.alerts.user_alert_storage import UserAlertStorage
    from src.storage.database import database_enabled
    from src.storage.user_alerts import load_user_alerts_config

    if not database_enabled():
        raise RuntimeError("User-scoped alert checks require MARKETHELM_DATABASE_URL")

    exists, config = load_user_alerts_config(user_id)
    if not exists or not config:
        return {
            "triggered": 0,
            "events": [],
            "last_data_date": None,
            "message": "No active watches configured.",
        }

    engine = AlertEngine.from_config_dict(
        config,
        storage=UserAlertStorage(user_id),
    )
    if not engine:
        return {
            "triggered": 0,
            "events": [],
            "last_data_date": None,
            "message": "No active watches configured.",
        }

    watch_symbols: List[str] = []
    from src.alerts.alert_rules import condition_watch_symbols

    for alert in engine.alerts:
        # Truthy non-dict conditions (str/list) AttributeError on .get; mirror
        # alert_paths.get_enabled_watch_symbols / AlertEngine.evaluate soft-fail.
        raw_condition = alert.get("condition")
        condition = raw_condition if isinstance(raw_condition, dict) else {}
        for symbol in condition_watch_symbols(condition):
            if symbol not in watch_symbols:
                watch_symbols.append(symbol)

    last_date, _prices, stocks = load_market_snapshot(
        watch_symbols,
        fetch_missing_quotes=True,
    )
    if not stocks:
        return {
            "triggered": 0,
            "events": [],
            "last_data_date": last_date,
            "message": "No market data available.",
        }

    events = engine.evaluate(stocks)
    return {
        "triggered": len(events),
        "events": events,
        "last_data_date": last_date,
        "message": None if events else "No alerts triggered on latest data.",
    }


def run_db_worker_cycle(worker_id: Optional[str] = None) -> Dict[str, Any]:
    """Orchestrator tick + job queue processing for hosted multi-user mode."""
    from src.alerts.alert_orchestrator import run_orchestrator_tick
    from src.alerts.job_processor import process_job_queue
    from src.storage.database import init_database

    init_database()
    wid = worker_id or _PROCESS_WORKER_ID
    from src.storage.health import record_worker_heartbeat

    try:
        tick = run_orchestrator_tick()
        stats = process_job_queue(wid)
    except Exception as exc:
        record_worker_heartbeat(wid, "error", {"error": type(exc).__name__})
        raise
    result = {
        "triggered": stats.get("delivered", 0),
        "events": [],
        "last_data_date": tick.get("last_data_date"),
        "message": tick.get("message"),
        "enqueued": tick.get("enqueued", 0),
        "jobs": stats,
    }
    record_worker_heartbeat(wid, "healthy", {"enqueued": result["enqueued"], "jobs": stats})
    return result


def _coerce_triggered_count(raw: Any) -> int:
    """Coerce triggered count for logging; Inf/NaN must not abort the worker loop."""
    if isinstance(raw, bool):
        return int(raw)
    if isinstance(raw, float) and not math.isfinite(raw):
        return 0
    try:
        return int(raw or 0)
    except (TypeError, ValueError, OverflowError):
        return 0


def log_check_result(result: Dict[str, Any]) -> None:
    triggered = _coerce_triggered_count(result.get("triggered"))
    last_date = result.get("last_data_date")
    message = result.get("message")
    jobs = result.get("jobs")
    if jobs:
        logger.info(
            "DB worker: enqueued=%s evaluated=%s delivered=%s failed=%s (data %s)",
            result.get("enqueued", 0),
            jobs.get("evaluated", 0),
            jobs.get("delivered", 0),
            jobs.get("failed", 0),
            last_date,
        )
        return
    if triggered:
        logger.info("Alert check triggered %s watch(es) (data %s)", triggered, last_date)
        for event in result.get("events") or []:
            logger.info(
                "  - %s symbols=%s",
                event.get("alert_name", event.get("alert_id")),
                event.get("symbols"),
            )
    elif message:
        logger.info("Alert check: %s (data %s)", message, last_date)
    else:
        logger.info("Alert check complete: no triggers (data %s)", last_date)


def run_worker_once() -> Dict[str, Any]:
    result = run_check_once()
    log_check_result(result)
    return result


def run_worker_loop(
    interval_seconds: Optional[int] = None,
    *,
    should_stop: Optional[Callable[[], bool]] = None,
) -> None:
    """Evaluate watches on a fixed interval until SIGINT/SIGTERM."""
    interval = resolve_interval_seconds(interval_seconds)
    stop = False

    def _request_stop(*_args: object) -> None:
        nonlocal stop
        stop = True

    signal.signal(signal.SIGINT, _request_stop)
    signal.signal(signal.SIGTERM, _request_stop)

    from src.storage.database import database_enabled

    mode = "multi-user job queue" if database_enabled() else "file-backed"
    logger.info("Alert worker started (%s, interval %ss); Ctrl+C to stop", mode, interval)
    while not stop:
        run_worker_once()
        if stop:
            break
        deadline = time.monotonic() + interval
        while time.monotonic() < deadline:
            if stop or (should_stop and should_stop()):
                stop = True
                break
            time.sleep(1)
    logger.info("Alert worker stopped")

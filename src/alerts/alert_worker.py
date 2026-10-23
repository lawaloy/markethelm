"""Scheduled alert evaluation worker (cron, Task Scheduler, systemd, or long-running process)."""

from __future__ import annotations

import math
import os
import signal
import time
import uuid
from typing import Any, Callable, Dict, List, Optional

import src.alerts.alert_engine as alert_engine
import src.alerts.alert_orchestrator as alert_orchestrator
import src.alerts.alert_rules as alert_rules
import src.alerts.alert_runner as alert_runner
import src.alerts.job_processor as job_processor
import src.alerts.market_snapshot as market_snapshot
import src.alerts.user_alert_storage as user_alert_storage
import src.storage.database as database
import src.storage.health as health
import src.storage.user_alerts as user_alerts
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
    if database.database_enabled():
        return run_db_worker_cycle()

    return alert_runner.evaluate_alerts_from_latest_data()


def run_user_check(user_id: str) -> Dict[str, Any]:
    """Evaluate and deliver only one hosted user's configured alerts."""

    if not database.database_enabled():
        raise RuntimeError("User-scoped alert checks require MARKETHELM_DATABASE_URL")

    exists, config = user_alerts.load_user_alerts_config(user_id)
    if not exists or not config:
        return {
            "triggered": 0,
            "events": [],
            "last_data_date": None,
            "message": "No active watches configured.",
        }

    engine = alert_engine.AlertEngine.from_config_dict(
        config,
        storage=user_alert_storage.UserAlertStorage(user_id),
    )
    if not engine:
        return {
            "triggered": 0,
            "events": [],
            "last_data_date": None,
            "message": "No active watches configured.",
        }

    watch_symbols: List[str] = []

    for alert in engine.alerts:
        # Truthy non-dict conditions (str/list) AttributeError on .get; mirror
        # alert_paths.get_enabled_watch_symbols / AlertEngine.evaluate soft-fail.
        raw_condition = alert.get("condition")
        condition = raw_condition if isinstance(raw_condition, dict) else {}
        for symbol in alert_rules.condition_watch_symbols(condition):
            if symbol not in watch_symbols:
                watch_symbols.append(symbol)

    last_date, _prices, stocks = market_snapshot.load_market_snapshot(
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

    database.init_database()
    wid = worker_id or _PROCESS_WORKER_ID

    try:
        tick = alert_orchestrator.run_orchestrator_tick()
        stats = job_processor.process_job_queue(wid)
    except Exception as exc:
        health.record_worker_heartbeat(wid, "error", {"error": type(exc).__name__})
        raise
    result = {
        "triggered": stats.get("delivered", 0),
        "events": [],
        "last_data_date": tick.get("last_data_date"),
        "message": tick.get("message"),
        "enqueued": tick.get("enqueued", 0),
        "jobs": stats,
    }
    health.record_worker_heartbeat(wid, "healthy", {"enqueued": result["enqueued"], "jobs": stats})
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

    mode = "multi-user job queue" if database.database_enabled() else "file-backed"
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

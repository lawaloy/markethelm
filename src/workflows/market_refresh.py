"""Scheduled hosted market-data collection, independent of a hosting provider."""

from __future__ import annotations

import argparse
import json
import os
import signal
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone
from typing import Sequence

import src.storage.market_refresh as refresh_store
from src.core.logger import configure_logging, get_logger
from src.core.market_refresh import MarketRefreshSettings, market_refresh_enabled
from src.storage.database import database_enabled, init_database
from src.workflows.tracker import StockTrackerWorkflow

logger = get_logger(__name__)


def _collect(settings: MarketRefreshSettings) -> int:
    # An orphaned child must exit before the database lease expires, even if its
    # parent/container supervisor disappears. No provider errors go to public probes.
    watchdog = threading.Timer(settings.timeout_seconds, os._exit, args=(124,))
    watchdog.daemon = True
    watchdog.start()
    try:
        result = StockTrackerWorkflow(load_alerts=False).run(
            use_screener=True, top_n_stocks=settings.top_n, evaluate_alerts=False
        )
        return 0 if result.get("success") else 1
    finally:
        watchdog.cancel()


def _run_collector(settings: MarketRefreshSettings) -> str | None:
    env = os.environ.copy()
    env.update(
        {
            "MARKET_REFRESH_" + name.upper(): str(getattr(settings, name))
            for name in settings.__dataclass_fields__
        }
    )
    child = subprocess.Popen(
        [sys.executable, "-m", "src.workflows.market_refresh", "--collect"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        env=env,
    )
    try:
        code = child.wait(timeout=settings.timeout_seconds)
        if code == 124:
            return "timeout"
        return None if code == 0 else "collector_failed"
    except subprocess.TimeoutExpired:
        return "timeout"
    finally:
        if child.poll() is None:
            child.kill()
            child.wait()


def run_once(
    settings: MarketRefreshSettings, *, now: datetime | None = None, retry_failed: bool = False
) -> dict:
    """Collect the most recent due session once, with persistent retry limits."""
    now = now or refresh_store.utc_now()
    if now.utcoffset() is None:
        raise ValueError("timestamp must include a timezone")
    now = now.astimezone(timezone.utc)
    session = refresh_store.expected_session(now, settings)
    owner, status = refresh_store.claim_run(session, now, settings, retry_failed=retry_failed)
    if owner is None:
        return {"status": status, "session": session}
    error = "interrupted"
    count = 0
    try:
        error = _run_collector(settings)
        if error is None:
            count = refresh_store.verified_quote_count(
                session, now.isoformat(), refresh_store.utc_now()
            )
            if count < settings.min_symbols:
                error = "insufficient_fresh_data"
    except Exception as exc:
        error = "collector_error"
        logger.error("Market collection failed: %s", type(exc).__name__)
    finally:
        finished = refresh_store.finish_run(
            owner, refresh_store.utc_now(), settings, error_code=error, quote_count=count
        )
    result = {
        "status": "error" if error else "success",
        "session": session,
        "quote_count": count,
        "error_code": error,
    }
    if not finished:
        result.update(status="error", error_code="lease_lost")
    logger.info("Scheduled market collection: %s", result)
    return result


def _terminate(_signal, _frame) -> None:
    raise KeyboardInterrupt


def main(argv: Sequence[str] | None = None) -> int:
    configure_logging()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--check", action="store_true", help="Exit nonzero when market data is stale"
    )
    parser.add_argument("--loop", action="store_true", help="Poll for due sessions continuously")
    parser.add_argument(
        "--retry-failed", action="store_true", help="Reset failed-session retry budget"
    )
    parser.add_argument("--collect", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    if sum((args.check, args.loop, args.collect)) > 1:
        parser.error("choose only one of --check, --loop, or --collect")
    if args.check and args.retry_failed:
        parser.error("--check cannot retry a collection")
    if args.loop and args.retry_failed:
        parser.error("--retry-failed is a one-shot operator action")
    if not database_enabled() or not market_refresh_enabled():
        logger.error("Set MARKETHELM_DATABASE_URL and MARKETHELM_MARKET_REFRESH_ENABLED=true")
        return 2
    try:
        settings = MarketRefreshSettings.from_env()
    except ValueError as exc:
        logger.error("Invalid collector configuration: %s", exc)
        return 2
    # Start the child watchdog before any database or provider work. The parent
    # has already initialized the schema and claimed the scheduled run.
    if args.collect:
        return _collect(settings)
    try:
        init_database()
    except Exception as exc:
        logger.error("Collector database unavailable: %s", type(exc).__name__)
        return 2
    if args.check:
        try:
            result = refresh_store.market_data_health(refresh_store.utc_now(), settings)
        except Exception:
            result = {"ok": False, "reason": "collector_health_unavailable"}
        print(json.dumps(result))
        return 0 if result["ok"] else 1
    previous = signal.signal(signal.SIGTERM, _terminate)
    try:
        while True:
            try:
                result = run_once(settings, retry_failed=args.retry_failed)
            except Exception as exc:
                logger.error("Scheduled market collection unavailable: %s", type(exc).__name__)
                result = {"status": "error"}
            if not args.loop:
                return 1 if result["status"] in {"error", "attempts_exhausted"} else 0
            time.sleep(settings.poll_seconds)
    except KeyboardInterrupt:
        return 130
    finally:
        signal.signal(signal.SIGTERM, previous)


if __name__ == "__main__":
    raise SystemExit(main())

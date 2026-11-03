"""Synthetic live-forecast fixtures, never production performance evidence."""

from datetime import datetime, timezone

from src.analysis.market_calendar import trading_session_after_timestamp
from src.storage.forecast_ledger import record_forecast_run

NOW = datetime(2026, 7, 6, 21, tzinfo=timezone.utc)
LATER = datetime(2026, 7, 14, 21, tzinfo=timezone.utc)


def stock(**changes):
    return {
        "symbol": "AAPL",
        "close": 100.0,
        "change_percent": 1.0,
        "volume": 1000000,
        "market_cap": 100000,
        "quote_timestamp": "2026-07-06T20:00:00+00:00",
        "outcome_session": "2026-07-02",
        "outcome_close": 99.0,
        "outcome_final": True,
        **changes,
    }


def projection(now=NOW, **changes):
    return {
        "symbol": "AAPL",
        "current_price": 100.0,
        "target_mid": 102.0,
        "target_low": 99.0,
        "target_high": 105.0,
        "confidence": 80.0,
        "generated_at": now.isoformat(),
        "projection_date": trading_session_after_timestamp(now, 5).isoformat(),
        "projection_horizon_sessions": 5,
        "projection_calendar": "XNYS",
        "projection_model_version": "heuristic-v1",
        **changes,
    }


def outcome(**changes):
    return stock(
        **{
            "close": 105.0,
            "quote_timestamp": "2026-07-14T20:00:00+00:00",
            "outcome_session": "2026-07-13",
            "outcome_close": 104.0,
            **changes,
        }
    )


def record(path, p=None, rows=None, now=NOW, **kwargs):
    return record_forecast_run(
        {"AAPL": projection() if p is None else p},
        [stock()] if rows is None else rows,
        data_dir=path,
        now=now,
        **kwargs,
    )

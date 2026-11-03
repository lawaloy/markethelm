"""Synthetic live-forecast fixtures, never production performance evidence."""

from datetime import datetime, timedelta, timezone

from src.analysis.market_calendar import get_market_calendar, trading_session_after_timestamp
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


def training_samples():
    calendar = get_market_calendar()
    sessions = calendar.sessions_in_range("2026-01-05", "2026-03-02")
    rows = []
    for index, session in enumerate(sessions[:25]):
        for number in range(30):
            movement = 0.02 if number % 2 else -0.02
            rows.append(
                {
                    "runDate": session.date().isoformat(),
                    "targetDate": sessions[index + 5].date().isoformat(),
                    "symbol": f"S{number:02d}",
                    "current": 100.0,
                    "predicted": 100 * (1 + movement),
                    "actual": 100 * (1.01 + 0.1 * movement),
                    "confidenceBand": "60-69",
                    "confidence": 60,
                    "generatedAt": (
                        calendar.session_close(session).to_pydatetime() + timedelta(hours=1)
                    ).isoformat(),
                    "outcomeAvailableAt": calendar.session_close(sessions[index + 6]).isoformat(),
                    "actualProvenance": "verified_previous_close",
                    "generationProvenance": "timestamped",
                }
            )
    return rows

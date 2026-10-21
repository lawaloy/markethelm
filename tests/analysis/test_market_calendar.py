"""Exchange-session semantics for projection horizons."""

from datetime import date, datetime, timezone

import pytest

from src.analysis.market_calendar import (
    last_completed_session,
    previous_close_session_at,
    projection_target_date,
    trading_session_after,
    trading_session_after_timestamp,
)


def test_five_sessions_skip_weekend_and_independence_day_holiday():
    assert trading_session_after("2026-07-02", 5) == date(2026, 7, 10)


def test_non_session_run_anchors_to_previous_session():
    assert trading_session_after("2026-07-04", 5) == date(2026, 7, 10)


@pytest.mark.parametrize(
    ("timestamp", "completed", "target"),
    [
        (datetime(2026, 7, 6, 12, tzinfo=timezone.utc), date(2026, 7, 2), date(2026, 7, 10)),
        (datetime(2026, 7, 6, 15, tzinfo=timezone.utc), date(2026, 7, 2), date(2026, 7, 10)),
        (datetime(2026, 7, 6, 21, tzinfo=timezone.utc), date(2026, 7, 6), date(2026, 7, 13)),
    ],
)
def test_timestamp_horizon_anchors_to_last_completed_session(timestamp, completed, target):
    assert last_completed_session(timestamp) == completed
    assert trading_session_after_timestamp(timestamp, 5) == target


def test_timestamp_horizon_rejects_ambiguous_naive_datetime():
    with pytest.raises(ValueError, match="timezone"):
        trading_session_after_timestamp(datetime(2026, 7, 6, 8), 5)


@pytest.mark.parametrize(
    ("timestamp", "session"),
    [
        (datetime(2026, 7, 6, 19, tzinfo=timezone.utc), date(2026, 7, 2)),
        (datetime(2026, 7, 6, 21, tzinfo=timezone.utc), date(2026, 7, 2)),
        (datetime(2026, 7, 4, 21, tzinfo=timezone.utc), None),
    ],
)
def test_previous_close_maps_only_from_a_current_market_session(timestamp, session):
    assert previous_close_session_at(timestamp) == session


def test_horizon_must_be_positive():
    with pytest.raises(ValueError, match="at least 1"):
        trading_session_after("2026-07-02", 0)


def test_unknown_calendar_has_actionable_error():
    with pytest.raises(ValueError, match="unknown or unavailable"):
        trading_session_after("2026-07-02", 5, "NOT-A-CALENDAR")


@pytest.mark.parametrize(
    "projection",
    [
        {},
        {"projection_date": None, "generated_at": None},
        {"projection_date": float("nan"), "generated_at": float("nan")},
        {"projection_date": "invalid", "generated_at": "invalid"},
        {"projection_date": "2026-02-30", "generated_at": "2026-07-04T12:00:00"},
    ],
)
def test_legacy_projection_target_handles_missing_or_invalid_metadata(projection):
    assert projection_target_date(projection, "2026-07-04") == date(2026, 7, 10)

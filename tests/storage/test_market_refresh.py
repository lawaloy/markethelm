"""Scheduled collectors coordinate through the real hosted database."""

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

import pytest

import src.storage.database as database
from src.analysis.market_calendar import get_market_calendar
from src.core.market_refresh import MarketRefreshSettings
from src.storage.database import get_connection, init_database
from src.storage.market_refresh import (
    claim_run,
    expected_session,
    finish_run,
    load_state,
    market_data_health,
    verified_quote_count,
)

NOW = datetime(2026, 7, 6, 21, tzinfo=timezone.utc)
SETTINGS = MarketRefreshSettings(min_symbols=2, timeout_seconds=10, retry_seconds=5)


@pytest.fixture
def refresh_db(tmp_path, monkeypatch):
    monkeypatch.setenv(
        "MARKETHELM_DATABASE_URL", f"sqlite:///{(tmp_path / 'refresh.db').as_posix()}"
    )
    monkeypatch.setenv("MARKETHELM_MARKET_REFRESH_ENABLED", "true")
    init_database()


def seed_snapshot(session, *, count=2, since=NOW, quote_at=None, projections=True, summary=True):
    quote_at = quote_at or get_market_calendar().session_close(session).to_pydatetime()
    stamp = since.isoformat()
    with get_connection() as conn:
        for index in range(count):
            symbol = f"TEST{index}"
            conn.execute(
                "INSERT INTO market_bars (trade_date, symbol, close, quote_timestamp, written_at) "
                "VALUES (?, ?, 100, ?, ?)",
                (session, symbol, quote_at.isoformat(), stamp),
            )
            if projections:
                conn.execute(
                    "INSERT INTO projections (run_date, symbol, written_at) VALUES (?, ?, ?)",
                    (session, symbol, stamp),
                )
        if summary:
            conn.execute(
                "INSERT INTO daily_summaries (summary_date, written_at) VALUES (?, ?)",
                (session, stamp),
            )


@pytest.mark.parametrize(
    "at,session",
    [
        ("2026-11-27T18:14:59+00:00", "2026-11-25"),
        ("2026-11-27T18:15:00+00:00", "2026-11-27"),
        ("2026-11-28T21:00:00+00:00", "2026-11-27"),
        ("2026-03-09T20:14:59+00:00", "2026-03-06"),
        ("2026-03-09T20:15:00+00:00", "2026-03-09"),
        ("2026-11-02T21:14:59+00:00", "2026-10-30"),
        ("2026-11-02T21:15:00+00:00", "2026-11-02"),
        ("2026-07-03T22:30:00+00:00", "2026-07-02"),
    ],
)
def test_close_delay_handles_dst_holidays_weekends_and_early_closes(at, session):
    assert expected_session(datetime.fromisoformat(at), SETTINGS) == session


def test_only_one_concurrent_scheduler_claims_a_session(refresh_db):
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(lambda _: claim_run("2026-07-06", NOW, SETTINGS), range(8)))
    assert sum(owner is not None for owner, _ in results) == 1
    assert load_state()["attempts"] == 1


def test_success_survives_restart_and_prevents_repeated_collection(refresh_db):
    owner, _ = claim_run("2026-07-06", NOW, SETTINGS)
    assert finish_run(owner, NOW, SETTINGS, quote_count=2)
    init_database()
    assert claim_run("2026-07-06", NOW + timedelta(seconds=1), SETTINGS) == (
        None,
        "already_collected",
    )
    assert load_state()["last_success_session"] == "2026-07-06"


def test_retry_cooldown_budget_and_operator_recovery_are_persistent(refresh_db):
    now = NOW
    for attempt in range(SETTINGS.max_attempts):
        owner, _ = claim_run("2026-07-06", now, SETTINGS)
        assert owner
        finish_run(owner, now, SETTINGS, error_code="collector_failed")
        assert claim_run("2026-07-06", now + timedelta(seconds=1), SETTINGS)[0] is None
        now += timedelta(seconds=6)
    assert claim_run("2026-07-06", now, SETTINGS) == (None, "attempts_exhausted")
    assert claim_run("2026-07-06", now, SETTINGS, retry_failed=True)[0]
    assert load_state()["attempts"] == 1


def test_expired_owner_cannot_overwrite_a_replacement_run(refresh_db):
    old, _ = claim_run("2026-07-06", NOW, SETTINGS)
    later = NOW + timedelta(seconds=SETTINGS.timeout_seconds + 121)
    replacement, _ = claim_run("2026-07-06", later, SETTINGS)
    assert replacement and replacement != old
    assert finish_run(old, later, SETTINGS, error_code="collector_failed") is False
    assert load_state()["owner"] == replacement
    assert finish_run(replacement, later, SETTINGS, quote_count=2)


def test_new_session_has_a_new_retry_budget(refresh_db):
    owner, _ = claim_run("2026-07-06", NOW, SETTINGS)
    finish_run(owner, NOW, SETTINGS, error_code="collector_failed")
    assert claim_run("2026-07-07", NOW + timedelta(days=1), SETTINGS)[0]
    assert load_state()["attempts"] == 1


@pytest.mark.parametrize("projections,summary", [(False, True), (True, False), (False, False)])
def test_bars_without_complete_persisted_outputs_do_not_count(refresh_db, projections, summary):
    seed_snapshot("2026-07-06", projections=projections, summary=summary)
    assert verified_quote_count("2026-07-06", NOW.isoformat(), NOW) == 0


@pytest.mark.parametrize(
    "quote_at",
    [
        datetime(2026, 7, 2, 20, tzinfo=timezone.utc),
        datetime(2026, 7, 6, 15, tzinfo=timezone.utc),
        datetime(2026, 7, 6, 22, tzinfo=timezone.utc),
        datetime(2026, 7, 6, 20),
    ],
)
def test_relabelled_old_intraday_future_or_naive_quotes_are_not_fresh(refresh_db, quote_at):
    seed_snapshot("2026-07-06", quote_at=quote_at)
    assert verified_quote_count("2026-07-06", NOW.isoformat(), NOW) == 0


def test_old_outputs_do_not_prove_a_new_collection(refresh_db):
    seed_snapshot("2026-07-06", since=NOW - timedelta(hours=1))
    assert verified_quote_count("2026-07-06", NOW.isoformat(), NOW) == 0


def test_freshness_rechecks_actual_data_not_just_success_flag(refresh_db):
    owner, _ = claim_run("2026-07-06", NOW, SETTINGS)
    seed_snapshot("2026-07-06")
    finish_run(owner, NOW, SETTINGS, quote_count=2)
    assert market_data_health(NOW, SETTINGS)["ok"] is True
    with get_connection() as conn:
        conn.execute("DELETE FROM projections")
    assert market_data_health(NOW, SETTINGS)["ok"] is False


def test_missing_and_partial_data_are_unhealthy(refresh_db):
    assert market_data_health(NOW, SETTINGS)["reason"] == "never_collected"
    owner, _ = claim_run("2026-07-06", NOW, SETTINGS)
    seed_snapshot("2026-07-06", count=1)
    finish_run(owner, NOW, SETTINGS, quote_count=1)
    assert market_data_health(NOW, SETTINGS)["ok"] is False


def test_weekend_freshness_does_not_require_a_weekend_quote(refresh_db):
    friday = datetime(2026, 11, 27, 19, tzinfo=timezone.utc)
    owner, _ = claim_run("2026-11-27", friday, SETTINGS)
    seed_snapshot("2026-11-27", since=friday)
    finish_run(owner, friday, SETTINGS, quote_count=2)
    assert market_data_health(friday + timedelta(days=2), SETTINGS)["ok"] is True
    assert (
        market_data_health(datetime(2026, 11, 30, 22, tzinfo=timezone.utc), SETTINGS)["ok"] is False
    )


def test_running_collection_has_a_bounded_grace_period(refresh_db):
    friday = datetime(2026, 11, 27, 19, tzinfo=timezone.utc)
    owner, _ = claim_run("2026-11-27", friday, SETTINGS)
    seed_snapshot("2026-11-27", since=friday)
    finish_run(owner, friday, SETTINGS, quote_count=2)
    monday = datetime(2026, 11, 30, 21, 15, tzinfo=timezone.utc)
    assert market_data_health(monday, SETTINGS)["reason"] == "update_pending"
    assert market_data_health(monday + timedelta(seconds=191), SETTINGS)["ok"] is False


def test_offset_timestamps_are_normalized_for_lease_comparisons(refresh_db):
    offset_now = NOW.astimezone(timezone(timedelta(hours=5)))
    owner, _ = claim_run("2026-07-06", offset_now, SETTINGS)
    assert owner
    assert load_state()["started_at"] == NOW.isoformat()
    assert claim_run("2026-07-06", NOW, SETTINGS) == (None, "running")


def test_abandoned_last_attempt_is_reported_as_failed(refresh_db):
    config = MarketRefreshSettings(max_attempts=1, timeout_seconds=1)
    claim_run("2026-07-06", NOW, config)
    assert claim_run("2026-07-06", NOW + timedelta(seconds=122), config) == (
        None,
        "attempts_exhausted",
    )
    state = load_state()
    assert state["status"] == "error"
    assert state["error_code"] == "lease_expired"


def test_schema_seven_upgrades_without_losing_existing_market_data(tmp_path, monkeypatch):
    monkeypatch.setenv("MARKETHELM_DATABASE_URL", f"sqlite:///{(tmp_path/'upgrade.db').as_posix()}")
    original = database._MIGRATIONS
    with monkeypatch.context() as legacy:
        legacy.setattr(database, "_MIGRATIONS", original[:7])
        legacy.setattr(database, "LATEST_SCHEMA_VERSION", 7)
        init_database()
        with get_connection() as conn:
            conn.execute(
                "INSERT INTO market_bars (trade_date, symbol, close, written_at) "
                "VALUES ('2026-07-06', 'AAPL', 100, ?)",
                (NOW.isoformat(),),
            )
    init_database()
    init_database()
    with get_connection() as conn:
        assert (
            conn.execute("SELECT close FROM market_bars WHERE symbol = 'AAPL'").fetchone()["close"]
            == 100
        )
        assert (
            conn.execute("SELECT name FROM schema_migrations WHERE version = 8").fetchone()["name"]
            == "scheduled_market_collection"
        )
        assert conn.execute("SELECT * FROM market_refresh_state").fetchall() == []

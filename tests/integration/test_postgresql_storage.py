"""End-to-end storage checks against a real PostgreSQL server."""

from __future__ import annotations

import os
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from urllib.parse import quote

import psycopg
import pytest

import src.storage.database as database
from src.analysis.forward_evaluation import forward_report
from src.analysis.market_calendar import get_market_calendar
from src.analysis.return_candidate import MODEL_VERSION, bundled_candidate
from src.core.market_refresh import MarketRefreshSettings
from src.storage.account_tokens import RESET_PASSWORD, consume_token, issue_token
from src.storage.alert_jobs import (
    JOB_EVALUATE_SYMBOL,
    STATUS_COMPLETED,
    claim_jobs,
    complete_job,
    enqueue_job,
)
from src.storage.alert_watches import get_watch
from src.storage.database import LATEST_SCHEMA_VERSION, get_connection, init_database
from src.storage.forecast_ledger import ledger_connection, ledger_snapshot, record_forecast_run
from src.storage.health import latest_worker_heartbeat, record_worker_heartbeat
from src.storage.market_bars import list_market_bar_dates, upsert_market_bars
from src.storage.market_refresh import claim_run, finish_run, load_state
from src.storage.projections_store import (
    list_projection_dates,
    load_daily_summary,
    upsert_daily_summary,
    upsert_projections,
)
from src.storage.rate_limits import consume_rate_limit
from src.storage.user_alerts import load_user_alerts_config, save_user_alerts_config
from src.storage.users import (
    authenticate_user,
    change_password,
    create_user,
    delete_user_account,
    get_user_by_id,
)
from tests.helpers.forecasts import LATER, NOW, outcome, projection, stock

pytestmark = pytest.mark.integration


@pytest.fixture()
def postgresql_database(monkeypatch):
    base_url = os.environ.get("MARKETHELM_POSTGRES_TEST_URL", "").strip()
    if not base_url:
        raise RuntimeError("MARKETHELM_POSTGRES_TEST_URL must be configured")

    schema = f"markethelm_test_{uuid.uuid4().hex}"
    with psycopg.connect(base_url, autocommit=True) as admin:
        admin.execute(psycopg.sql.SQL("CREATE SCHEMA {}").format(psycopg.sql.Identifier(schema)))

    separator = "&" if "?" in base_url else "?"
    test_url = f"{base_url}{separator}options={quote(f'-csearch_path={schema}')}"
    monkeypatch.setenv("MARKETHELM_DATABASE_URL", test_url)
    try:
        yield
    finally:
        with psycopg.connect(base_url, autocommit=True) as admin:
            admin.execute(
                psycopg.sql.SQL("DROP SCHEMA {} CASCADE").format(psycopg.sql.Identifier(schema))
            )


def test_postgresql_migrations_and_storage_workflow(postgresql_database):
    init_database()
    user = create_user("postgres@example.com", "correct-horse-battery-staple")
    config = {
        "defaults": {"cooldown_minutes": 15},
        "alerts": [
            {
                "id": "aapl-breakout",
                "enabled": True,
                "condition": {
                    "type": "price_threshold",
                    "symbol": "AAPL",
                    "operator": ">=",
                    "value": 200,
                },
            }
        ],
    }
    save_user_alerts_config(user["id"], config)

    exists, stored = load_user_alerts_config(user["id"])
    watch = get_watch(user["id"], "aapl-breakout")
    assert exists is True
    assert stored is not None
    assert stored["alerts"][0]["id"] == "aapl-breakout"
    assert watch is not None
    assert watch["alert"]["condition"]["symbol"] == "AAPL"

    job_id = enqueue_job(
        JOB_EVALUATE_SYMBOL,
        {"symbol": "AAPL", "price": 201.0},
    )
    claimed = claim_jobs([JOB_EVALUATE_SYMBOL], "postgres-worker", limit=1)
    assert [job["id"] for job in claimed] == [job_id]
    assert complete_job(job_id, worker_id="postgres-worker") is True

    with get_connection() as conn:
        job = conn.execute("SELECT status FROM alert_jobs WHERE id = ?", (job_id,)).fetchone()
        versions = conn.execute("SELECT version FROM schema_migrations ORDER BY version").fetchall()
    assert job["status"] == STATUS_COMPLETED
    assert [row["version"] for row in versions] == list(range(1, LATEST_SCHEMA_VERSION + 1))

    upsert_market_bars(
        [{"symbol": "AAPL", "close": 201.0, "name": "Apple"}],
        "2026-07-10",
        source="integration",
    )
    upsert_projections(
        [
            {
                "symbol": "AAPL",
                "target_mid": 210.0,
                "recommendation": "BUY",
                "confidence": 80,
            }
        ],
        "2026-07-10",
        source="integration",
    )
    upsert_daily_summary(
        {"date": "2026-07-10", "analysis": {"summary": {"total_stocks": 1}}},
        "2026-07-10",
        source="integration",
    )
    assert list_market_bar_dates(limit=5)[0] == "2026-07-10"
    assert list_projection_dates(limit=5)[0] == "2026-07-10"
    summary = load_daily_summary("2026-07-10")
    assert summary is not None
    assert summary["analysis"]["summary"]["total_stocks"] == 1

    reset_token = issue_token(user["id"], RESET_PASSWORD)
    assert consume_token(reset_token, RESET_PASSWORD) == user["id"]
    assert consume_token(reset_token, RESET_PASSWORD) is None

    record_worker_heartbeat("postgres-worker", "healthy", {"delivered": 1})
    heartbeat = latest_worker_heartbeat()
    assert heartbeat is not None
    assert heartbeat["worker_id"] == "postgres-worker"
    assert heartbeat["details"] == {"delivered": 1}

    first_limit = consume_rate_limit("integration:client", now=121, window_seconds=60)
    second_limit = consume_rate_limit("integration:client", now=122, window_seconds=60)
    assert (first_limit.count, second_limit.count, second_limit.reset_at) == (1, 2, 180)

    managed = create_user("managed-postgres@example.com", "password123")
    change_password(managed["id"], "password123", "new-password-123")
    changed = authenticate_user("managed-postgres@example.com", "new-password-123")
    assert changed is not None
    assert changed["session_version"] == 2
    delete_user_account(managed["id"], "new-password-123")
    assert get_user_by_id(managed["id"]) is None


def test_postgresql_scheduled_collection_claims_are_atomic_and_fenced(postgresql_database):
    init_database()
    now = datetime(2026, 7, 6, 21, tzinfo=timezone.utc)
    settings = MarketRefreshSettings(timeout_seconds=10)
    with ThreadPoolExecutor(max_workers=6) as pool:
        claims = list(pool.map(lambda _: claim_run("2026-07-06", now, settings), range(6)))
    owners = [owner for owner, _ in claims if owner]
    assert len(owners) == 1
    assert load_state()["attempts"] == 1
    later = now + timedelta(seconds=131)
    replacement, _ = claim_run("2026-07-06", later, settings)
    assert replacement and replacement != owners[0]
    assert finish_run(owners[0], later, settings, error_code="collector_failed") is False
    assert finish_run(replacement, later, settings, quote_count=10) is True
    assert claim_run("2026-07-06", later, settings) == (None, "already_collected")


def test_postgresql_forward_forecasts_are_atomic_append_only_and_scored_once(postgresql_database):
    init_database()

    def record(_):
        return record_forecast_run({"AAPL": projection()}, [stock()], data_dir=None, now=NOW)

    with ThreadPoolExecutor(max_workers=6) as pool:
        results = list(pool.map(record, range(6)))
    assert sum(result["new_cases"] for result in results) == 1
    before = ledger_snapshot(data_dir=None)
    assert len(before["forecast_cases"]) == 1
    assert len(before["forecast_predictions"]) == 4
    result = record_forecast_run({}, [outcome()], data_dir=None, now=LATER)
    assert result["new_scores"] == 4
    scored = ledger_snapshot(data_dir=None)
    with pytest.raises(psycopg.errors.RaiseException, match="append-only"):
        with ledger_connection(None, write=True) as conn:
            conn.execute("UPDATE forecast_predictions SET target_mid = 999")
    with pytest.raises(psycopg.errors.RaiseException, match="append-only"):
        with ledger_connection(None, write=True) as conn:
            conn.execute("DELETE FROM forecast_scores")
    record_forecast_run(
        {}, [outcome(outcome_close=120)], data_dir=None, now=LATER + timedelta(seconds=1)
    )
    assert ledger_snapshot(data_dir=None)["forecast_scores"] == scored["forecast_scores"]


def test_postgresql_schema_eight_upgrades_without_reclassifying_history(
    postgresql_database, monkeypatch
):
    migrations = database._MIGRATIONS
    with monkeypatch.context() as legacy:
        legacy.setattr(database, "_MIGRATIONS", migrations[:8])
        legacy.setattr(database, "LATEST_SCHEMA_VERSION", 8)
        init_database()
        upsert_market_bars([stock()], "2026-07-06")
        upsert_projections([projection()], "2026-07-06")
    init_database()
    with get_connection() as conn:
        assert conn.execute("SELECT close FROM market_bars").fetchone()["close"] == 100
        assert conn.execute("SELECT target_mid FROM projections").fetchone()["target_mid"] == 102
    assert ledger_snapshot(data_dir=None)["forecast_cases"] == []


def test_postgresql_scores_shadow_candidate_on_paired_cases(postgresql_database):
    candidate = bundled_candidate()
    calendar = get_market_calendar()
    anchor = calendar.date_to_session(candidate.frozen_at.date().isoformat(), direction="next")
    origin = calendar.next_session(anchor)
    now = calendar.session_close(origin).to_pydatetime() + timedelta(hours=1)
    p = projection(now)
    quote = stock(quote_timestamp=calendar.session_close(origin).isoformat())
    with ThreadPoolExecutor(max_workers=6) as pool:
        results = list(
            pool.map(
                lambda _: record_forecast_run({"AAPL": p}, [quote], data_dir=None, now=now),
                range(6),
            )
        )
    assert sum(result["new_cases"] for result in results) == 1
    assert len(ledger_snapshot(data_dir=None)["forecast_predictions"]) == 5
    observed = calendar.next_session(p["projection_date"])
    later = calendar.session_close(observed).to_pydatetime() + timedelta(hours=1)
    actual = stock(
        quote_timestamp=calendar.session_close(observed).isoformat(),
        outcome_session=p["projection_date"],
        outcome_close=104,
    )
    assert record_forecast_run({}, [actual], data_dir=None, now=later)["new_scores"] == 5
    assert record_forecast_run({}, [actual], data_dir=None, now=later)["new_scores"] == 0
    report = forward_report(data_dir=None, now=later)
    shadow = report["models"][MODEL_VERSION]
    assert shadow["comparisons"]["heuristic-v1"]["scoredPairCount"] == 1
    assert shadow["comparisons"]["unchanged-price-v1"]["scoredPairCount"] == 1
    assert shadow["readiness"]["decision"] == "not_ready"

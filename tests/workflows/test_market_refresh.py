"""Collector failures must not become successful or unbounded scheduled runs."""

import os
import subprocess
import sys
from datetime import date, datetime, timedelta, timezone
from unittest.mock import Mock

import pytest
from fastapi.testclient import TestClient

import src.storage.market_refresh as store
import src.workflows.market_refresh as worker
from dashboard.backend.main import app
from src.core.market_refresh import MarketRefreshSettings
from src.storage.database import init_database

NOW = datetime(2026, 7, 6, 21, tzinfo=timezone.utc)
SETTINGS = MarketRefreshSettings(min_symbols=1, timeout_seconds=2, retry_seconds=5)


@pytest.fixture
def db(tmp_path, monkeypatch):
    monkeypatch.setenv("MARKETHELM_DATABASE_URL", f"sqlite:///{(tmp_path/'worker.db').as_posix()}")
    monkeypatch.setenv("MARKETHELM_MARKET_REFRESH_ENABLED", "true")
    monkeypatch.setenv("FINNHUB_API_KEY", "")
    monkeypatch.setenv("OPENAI_API_KEY", "")
    init_database()
    monkeypatch.setattr(store, "utc_now", lambda: NOW)


def test_success_exit_without_persisted_fresh_data_is_a_failure(db, monkeypatch):
    monkeypatch.setattr(worker, "_run_collector", lambda *args, **kwargs: None)
    result = worker.run_once(SETTINGS)
    assert result["status"] == "error"
    assert result["error_code"] == "insufficient_fresh_data"
    assert store.load_state()["last_success_session"] is None


def test_provider_exception_is_redacted_and_the_lease_is_released(db, monkeypatch, caplog):
    def fail(*args, **kwargs):
        raise RuntimeError("token=DO-NOT-LOG")

    monkeypatch.setattr(worker, "_run_collector", fail)
    result = worker.run_once(SETTINGS)
    assert result["error_code"] == "collector_error"
    state = store.load_state()
    assert state["lease_until"] is None
    assert state["error_code"] == "collector_error"
    assert "DO-NOT-LOG" not in caplog.text


@pytest.mark.parametrize("stage", ["collection", "verification"])
def test_interrupted_run_remains_failed_and_can_be_retried(db, monkeypatch, stage):
    def interrupt(*args):
        raise KeyboardInterrupt

    monkeypatch.setattr(
        worker, "_run_collector", interrupt if stage == "collection" else lambda *a, **k: None
    )
    monkeypatch.setattr(store, "verified_quote_count", interrupt)
    with pytest.raises(KeyboardInterrupt):
        worker.run_once(SETTINGS)
    state = store.load_state()
    assert state["status"] == "error"
    assert state["error_code"] == "interrupted"
    assert state["last_success_session"] is None
    assert state["lease_until"] is None
    owner, status = store.claim_run(
        state["target_session"], NOW + timedelta(seconds=SETTINGS.retry_seconds), SETTINGS
    )
    assert status == "claimed"
    assert owner


def test_timeout_terminates_and_reaps_child(monkeypatch):
    child = Mock()
    child.wait.side_effect = [subprocess.TimeoutExpired("collector", 2), 0]
    child.poll.return_value = None
    monkeypatch.setattr(worker.subprocess, "Popen", lambda *a, **kw: child)
    assert worker._run_collector(SETTINGS, "2026-07-06") == "timeout"
    child.kill.assert_called_once()
    assert child.wait.call_count == 2


def test_process_exit_and_watchdog_timeout_have_distinct_results(monkeypatch):
    for code, expected in [(0, None), (1, "collector_failed"), (124, "timeout")]:
        child = Mock()
        child.wait.return_value = code
        child.poll.return_value = code
        monkeypatch.setattr(worker.subprocess, "Popen", lambda *a, **kw: child)
        assert worker._run_collector(SETTINGS, "2026-07-06") == expected
        child.kill.assert_not_called()


def test_orphan_watchdog_really_stops_a_hung_collector(tmp_path):
    program = """
import time
import src.workflows.market_refresh as worker
from src.core.market_refresh import MarketRefreshSettings
class HungWorkflow:
    def __init__(self, **kwargs):
        pass
    def run(self, **kwargs):
        time.sleep(60)
worker.StockTrackerWorkflow = HungWorkflow
raise SystemExit(worker._collect(MarketRefreshSettings(timeout_seconds=1)))
"""
    env = {**os.environ, "FINNHUB_API_KEY": "", "OPENAI_API_KEY": ""}
    result = subprocess.run(
        [sys.executable, "-c", program], env=env, capture_output=True, timeout=30
    )
    assert result.returncode == 124


def test_freshness_probe_is_disabled_by_default(monkeypatch):
    monkeypatch.delenv("MARKETHELM_MARKET_REFRESH_ENABLED", raising=False)
    assert TestClient(app).get("/health/market-data").json() == {"status": "disabled"}


def test_freshness_probe_returns_503_for_missing_data(db):
    response = TestClient(app).get("/health/market-data")
    assert response.status_code == 503
    assert response.json()["reason"] == "never_collected"


def test_freshness_probe_redacts_database_failures(db, monkeypatch):
    def fail(*args):
        raise RuntimeError("postgresql://secret@private")

    monkeypatch.setattr(store, "market_data_health", fail)
    response = TestClient(app).get("/health/market-data")
    assert response.status_code == 503
    assert response.json()["reason"] == "collector_health_unavailable"
    assert "secret" not in response.text


def test_cli_requires_explicit_hosted_enablement(monkeypatch):
    monkeypatch.delenv("MARKETHELM_MARKET_REFRESH_ENABLED", raising=False)
    assert worker.main([]) == 2


@pytest.mark.parametrize(
    "key,value",
    [
        ("TIMEOUT_SECONDS", "0"),
        ("TIMEOUT_SECONDS", "3601"),
        ("TOP_N", "-1"),
        ("MIN_SYMBOLS", "501"),
        ("POLL_SECONDS", "NaN"),
        ("MAX_ATTEMPTS", "99999"),
    ],
)
def test_invalid_configuration_is_rejected(key, value, monkeypatch):
    monkeypatch.setenv("MARKET_REFRESH_" + key, value)
    with pytest.raises(ValueError):
        MarketRefreshSettings.from_env()


def test_cli_probe_reports_freshness_and_redacts_unavailable_database(db, monkeypatch, capsys):
    assert worker.main(["--check"]) == 1
    assert "never_collected" in capsys.readouterr().out

    def fail(*args):
        raise RuntimeError("database-password-DO-NOT-LOG")

    monkeypatch.setattr(store, "market_data_health", fail)
    assert worker.main(["--check"]) == 1
    output = capsys.readouterr().out
    assert "collector_health_unavailable" in output
    assert "DO-NOT-LOG" not in output


def test_packaged_cli_health_probe_runs_without_a_provider_call(db):
    result = subprocess.run(
        [sys.executable, "-m", "src.cli.commands", "collect-market-data", "--check"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=30,
    )
    assert result.returncode == 1
    assert "never_collected" in result.stdout
    assert "Traceback" not in result.stderr


def test_collector_persists_claimed_session_instead_of_local_calendar_date(monkeypatch):
    captured = {}

    class CaptureWorkflow:
        def __init__(self, **kwargs):
            pass

        def run(self, **kwargs):
            captured.update(kwargs)
            return {"success": True}

    monkeypatch.setattr(worker, "StockTrackerWorkflow", CaptureWorkflow)
    monkeypatch.delenv("MARKET_REFRESH_TARGET_SESSION", raising=False)
    monkeypatch.setattr(store, "utc_now", lambda: datetime(2026, 7, 11, 1, tzinfo=timezone.utc))
    assert worker._collect(SETTINGS) == 0
    assert captured["trade_date"] == date(2026, 7, 10)
    assert captured["evaluate_alerts"] is False


def test_collector_child_uses_parent_session_even_after_local_date_rolls(monkeypatch):
    captured = {}

    class CaptureWorkflow:
        def __init__(self, **kwargs):
            pass

        def run(self, **kwargs):
            captured.update(kwargs)
            return {"success": True}

    monkeypatch.setattr(worker, "StockTrackerWorkflow", CaptureWorkflow)
    monkeypatch.setenv("MARKET_REFRESH_TARGET_SESSION", "2026-07-10")
    monkeypatch.setattr(store, "utc_now", lambda: datetime(2026, 7, 13, 16, tzinfo=timezone.utc))
    assert worker._collect(SETTINGS) == 0
    assert captured["trade_date"] == date(2026, 7, 10)


def test_parent_passes_claimed_session_to_collector_child(monkeypatch):
    seen = {}

    class Child:
        def wait(self, timeout=None):
            return 0

        def poll(self):
            return 0

        def kill(self):
            raise AssertionError("successful collector must not be killed")

    def fake_popen(command, **kwargs):
        seen["command"] = command
        seen["env"] = kwargs["env"]
        return Child()

    monkeypatch.setattr(worker.subprocess, "Popen", fake_popen)
    assert worker._run_collector(SETTINGS, "2026-07-10") is None
    assert seen["env"]["MARKET_REFRESH_TARGET_SESSION"] == "2026-07-10"

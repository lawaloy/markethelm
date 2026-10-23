"""Tests for scheduled alert worker."""

from unittest.mock import patch

import pytest

from src.alerts import alert_worker
from src.storage.alert_jobs import JOB_DELIVER, JOB_EVALUATE_SYMBOL, pending_job_count
from src.storage.alert_watches import InvalidAlertWatchConfig, sync_watches_from_config
from src.storage.database import get_connection, init_database
from src.storage.user_alerts import save_user_alerts_config
from src.storage.users import create_user


@pytest.fixture
def db_users(tmp_path, monkeypatch):
    db_path = tmp_path / "worker.db"
    monkeypatch.setenv("MARKETHELM_DATABASE_URL", f"sqlite:///{db_path.as_posix()}")
    init_database()
    return (
        create_user("worker-a@example.com", "password123")["id"],
        create_user("worker-b@example.com", "password123")["id"],
    )


def _price_watch(alert_id: str, threshold: float = 200.0, symbol: str = "AAPL"):
    return {
        "defaults": {},
        "alerts": [
            {
                "id": alert_id,
                "name": f"{alert_id} watch",
                "enabled": True,
                "cooldown_minutes": 0,
                "condition": {
                    "type": "price_threshold",
                    "symbol": symbol,
                    "operator": "less_than",
                    "value": threshold,
                },
                "notifications": ["log"],
            }
        ],
    }


def test_resolve_interval_seconds_explicit_minimum() -> None:
    assert alert_worker.resolve_interval_seconds(30) == 60
    assert alert_worker.resolve_interval_seconds(120) == 120


def test_resolve_interval_seconds_from_env(monkeypatch) -> None:
    monkeypatch.setenv("ALERT_CHECK_INTERVAL_SECONDS", "180")
    assert alert_worker.resolve_interval_seconds() == 180


def test_resolve_interval_seconds_invalid_env_uses_default(monkeypatch) -> None:
    monkeypatch.setenv("ALERT_CHECK_INTERVAL_SECONDS", "not-a-number")
    assert alert_worker.resolve_interval_seconds() == alert_worker.DEFAULT_INTERVAL_SECONDS


def test_resolve_interval_seconds_default_without_env(monkeypatch) -> None:
    monkeypatch.delenv("ALERT_CHECK_INTERVAL_SECONDS", raising=False)
    assert alert_worker.resolve_interval_seconds() == alert_worker.DEFAULT_INTERVAL_SECONDS


@patch("src.alerts.alert_worker.run_db_worker_cycle")
def test_run_check_once_uses_db_cycle(mock_db_cycle, monkeypatch) -> None:
    monkeypatch.setenv("MARKETHELM_DATABASE_URL", "sqlite:///tmp/test.db")
    mock_db_cycle.return_value = {
        "triggered": 2,
        "enqueued": 3,
        "jobs": {"evaluated": 3, "delivered": 2, "failed": 0},
        "last_data_date": "2026-06-09",
    }
    result = alert_worker.run_check_once()
    assert result["triggered"] == 2
    mock_db_cycle.assert_called_once()


def test_run_user_check_requires_database(monkeypatch) -> None:
    monkeypatch.delenv("MARKETHELM_DATABASE_URL", raising=False)
    with pytest.raises(RuntimeError, match="MARKETHELM_DATABASE_URL"):
        alert_worker.run_user_check("user-1")


def test_run_user_check_returns_no_watches_when_config_missing(db_users) -> None:
    user_a, _user_b = db_users
    result = alert_worker.run_user_check(user_a)
    assert result == {
        "triggered": 0,
        "events": [],
        "last_data_date": None,
        "message": "No active watches configured.",
    }


def test_run_user_check_returns_no_watches_when_alerts_disabled(db_users) -> None:
    user_a, _user_b = db_users
    save_user_alerts_config(
        user_a,
        {
            "defaults": {},
            "alerts": [
                {
                    "id": "disabled",
                    "enabled": False,
                    "condition": {
                        "type": "price_threshold",
                        "symbol": "AAPL",
                        "operator": "less_than",
                        "value": 200,
                    },
                    "notifications": ["log"],
                }
            ],
        },
    )
    result = alert_worker.run_user_check(user_a)
    assert result["triggered"] == 0
    assert result["message"] == "No active watches configured."


def test_run_user_check_reports_missing_market_data(db_users) -> None:
    user_a, _user_b = db_users
    save_user_alerts_config(user_a, _price_watch("aapl-low"))

    with patch(
        "src.alerts.market_snapshot.load_market_snapshot",
        return_value=("2026-06-09", {}, []),
    ) as load_snapshot:
        result = alert_worker.run_user_check(user_a)

    assert result == {
        "triggered": 0,
        "events": [],
        "last_data_date": "2026-06-09",
        "message": "No market data available.",
    }
    load_snapshot.assert_called_once_with(["AAPL"], fetch_missing_quotes=True)


def test_run_user_check_only_evaluates_requested_user(db_users) -> None:
    """Manual hosted checks must never fan out into another tenant's watches."""
    user_a, user_b = db_users
    save_user_alerts_config(user_a, _price_watch("aapl-low-a"))
    save_user_alerts_config(user_b, _price_watch("aapl-low-b"))

    with patch(
        "src.alerts.market_snapshot.load_market_snapshot",
        return_value=(
            "2026-06-09",
            {"AAPL": 150.0},
            [{"symbol": "AAPL", "close": 150.0}],
        ),
    ) as load_snapshot:
        with patch("src.alerts.alert_engine.LogNotifier.send", return_value=True) as send:
            with patch("src.alerts.alert_worker.run_db_worker_cycle") as mock_cycle:
                result = alert_worker.run_user_check(user_a)

    assert result["triggered"] == 1
    assert result["last_data_date"] == "2026-06-09"
    assert result["message"] is None
    assert send.call_count == 1
    mock_cycle.assert_not_called()
    load_snapshot.assert_called_once_with(["AAPL"], fetch_missing_quotes=True)

    with get_connection() as conn:
        rows = conn.execute(
            "SELECT user_id, alert_id FROM alert_trigger_state ORDER BY alert_id"
        ).fetchall()
    assert [(row["user_id"], row["alert_id"]) for row in rows] == [
        (user_a, "aapl-low-a"),
    ]


def test_run_user_check_dedupes_and_uppercases_watch_symbols(db_users) -> None:
    user_a, _user_b = db_users
    save_user_alerts_config(
        user_a,
        {
            "defaults": {},
            "alerts": [
                {
                    "id": "aapl-a",
                    "enabled": True,
                    "condition": {
                        "type": "price_threshold",
                        "symbol": "aapl",
                        "operator": "less_than",
                        "value": 200,
                    },
                    "notifications": ["log"],
                },
                {
                    "id": "aapl-b",
                    "enabled": True,
                    "condition": {
                        "type": "price_threshold",
                        "symbol": "AAPL",
                        "operator": "greater_than",
                        "value": 100,
                    },
                    "notifications": ["log"],
                },
                {
                    "id": "screen",
                    "enabled": True,
                    "condition": {
                        "type": "screening_match",
                        "filters": {"volume_threshold": 1_000_000},
                    },
                    "notifications": ["log"],
                },
            ],
        },
    )

    with patch(
        "src.alerts.market_snapshot.load_market_snapshot",
        return_value=("2026-06-09", {}, []),
    ) as load_snapshot:
        alert_worker.run_user_check(user_a)

    load_snapshot.assert_called_once_with(["AAPL"], fetch_missing_quotes=True)


def test_run_user_check_skips_non_dict_conditions(db_users) -> None:
    """Truthy non-dict conditions previously AttributeError'd before evaluate()."""
    user_a, _user_b = db_users
    save_user_alerts_config(
        user_a,
        {
            "defaults": {},
            "alerts": [
                {
                    "id": "poison-str",
                    "enabled": True,
                    "condition": "price_threshold",
                    "notifications": ["log"],
                },
                {
                    "id": "poison-list",
                    "enabled": True,
                    "condition": [{"type": "price_threshold", "symbol": "MSFT"}],
                    "notifications": ["log"],
                },
                {
                    "id": "null-condition",
                    "enabled": True,
                    "condition": None,
                    "notifications": ["log"],
                },
                {
                    "id": "good",
                    "enabled": True,
                    "condition": {
                        "type": "price_threshold",
                        "symbol": "AAPL",
                        "operator": "less_than",
                        "value": 200,
                    },
                    "notifications": ["log"],
                },
            ],
        },
    )

    with patch(
        "src.alerts.market_snapshot.load_market_snapshot",
        return_value=(
            "2026-06-09",
            {"AAPL": 150.0},
            [{"symbol": "AAPL", "close": 150.0}],
        ),
    ) as load_snapshot:
        with patch("src.alerts.alert_engine.LogNotifier.send", return_value=True):
            result = alert_worker.run_user_check(user_a)

    assert result["triggered"] == 1
    load_snapshot.assert_called_once_with(["AAPL"], fetch_missing_quotes=True)


def test_run_user_check_strips_padded_symbols(db_users) -> None:
    """User-scoped checks must strip padded watch symbols before quote fetch."""
    user_a, _user_b = db_users
    save_user_alerts_config(
        user_a,
        {
            "defaults": {},
            "alerts": [
                {
                    "id": "padded",
                    "enabled": True,
                    "condition": {
                        "type": "price_threshold",
                        "symbol": "  msft  ",
                        "operator": "less_than",
                        "value": 400,
                    },
                    "notifications": ["log"],
                },
            ],
        },
    )

    with patch(
        "src.alerts.market_snapshot.load_market_snapshot",
        return_value=("2026-06-09", {}, []),
    ) as load_snapshot:
        alert_worker.run_user_check(user_a)

    load_snapshot.assert_called_once_with(["MSFT"], fetch_missing_quotes=True)


def test_save_user_alerts_rejects_sentinel_symbols(db_users) -> None:
    """Blank/sentinel symbols must fail closed at save, not become zombie watches."""

    user_a, _user_b = db_users
    with pytest.raises(InvalidAlertWatchConfig, match="valid symbol"):
        save_user_alerts_config(
            user_a,
            {
                "defaults": {},
                "alerts": [
                    {
                        "id": "sentinel-nan",
                        "enabled": True,
                        "condition": {
                            "type": "price_threshold",
                            "symbol": "nan",
                            "operator": "less_than",
                            "value": 10,
                        },
                        "notifications": ["log"],
                    },
                ],
            },
        )


def test_run_db_worker_cycle_evaluates_snapshot_and_delivers_per_user(db_users) -> None:
    """Hosted worker cycle should wire orchestrator, queue processing, and user storage."""
    user_a, user_b = db_users
    sync_watches_from_config(user_a, _price_watch("aapl-low-a"))
    sync_watches_from_config(user_b, _price_watch("aapl-low-b"))

    with patch(
        "src.alerts.alert_orchestrator.load_market_snapshot",
        return_value=("2026-06-09", {"AAPL": 150.0}, []),
    ) as load_snapshot:
        with patch("src.alerts.alert_engine.LogNotifier.send", return_value=True) as send:
            result = alert_worker.run_db_worker_cycle("test-worker")

    assert result["enqueued"] == 1
    assert result["triggered"] == 2
    assert result["last_data_date"] == "2026-06-09"
    assert result["jobs"] == {"evaluated": 1, "delivered": 2, "failed": 0}
    assert pending_job_count([JOB_EVALUATE_SYMBOL, JOB_DELIVER]) == 0
    load_snapshot.assert_called_once_with(["AAPL"], fetch_missing_quotes=True)
    assert send.call_count == 2

    with get_connection() as conn:
        rows = conn.execute("""
            SELECT user_id, alert_id FROM alert_trigger_state
            ORDER BY alert_id
            """).fetchall()

    assert [(row["user_id"], row["alert_id"]) for row in rows] == [
        (user_a, "aapl-low-a"),
        (user_b, "aapl-low-b"),
    ]


@patch("src.alerts.alert_worker.run_check_once")
def test_run_worker_once_logs_trigger(mock_run, caplog) -> None:
    mock_run.return_value = {
        "triggered": 1,
        "last_data_date": "2026-05-20",
        "events": [{"alert_name": "AAPL drop", "symbols": ["AAPL"]}],
    }
    with caplog.at_level("INFO"):
        result = alert_worker.run_worker_once()
    assert result["triggered"] == 1
    assert "triggered 1 watch" in caplog.text


@patch("src.alerts.alert_worker.run_check_once")
def test_run_worker_once_logs_no_data_message(mock_run, caplog) -> None:
    mock_run.return_value = {
        "triggered": 0,
        "last_data_date": None,
        "message": "No market data available.",
    }
    with caplog.at_level("INFO"):
        alert_worker.run_worker_once()
    assert "No market data available." in caplog.text


@patch("src.alerts.alert_worker.time.sleep")
@patch("src.alerts.alert_worker.run_worker_once")
def test_run_worker_loop_stops_via_callback(mock_once, _mock_sleep, caplog) -> None:
    with caplog.at_level("INFO"):
        alert_worker.run_worker_loop(60, should_stop=lambda: True)
    assert mock_once.call_count == 1
    assert "Alert worker stopped" in caplog.text


@patch("src.alerts.alert_worker.signal.signal")
@patch("src.alerts.alert_worker.time.sleep")
@patch("src.alerts.alert_worker.time.monotonic")
@patch("src.alerts.alert_worker.run_worker_once")
def test_run_worker_loop_repeats_until_stop_callback(
    mock_once, mock_monotonic, _mock_sleep, mock_signal
) -> None:
    """The daemon path should continue evaluating after an interval until asked to stop."""
    mock_monotonic.side_effect = [0, 0, 61, 61, 61]
    stop_checks = iter([False, True])

    alert_worker.run_worker_loop(60, should_stop=lambda: next(stop_checks))

    assert mock_once.call_count == 2
    assert mock_signal.call_count == 2

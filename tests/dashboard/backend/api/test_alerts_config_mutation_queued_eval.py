"""Hosted Settings mutations must take effect for already-queued evaluate jobs.

Disable/retarget (#453–#455) cover pause and same-id symbol swaps. These tests
cover the remaining Settings writes that rewrite watch rows: deleting a rule,
tightening its threshold, and replacing it with a different alert id. A queued
``evaluate_symbol`` job must use the post-PUT index, and a sibling tenant must
keep delivering.
"""

from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from dashboard.backend.main import app
from src.alerts.job_processor import process_job_queue
from src.storage.alert_jobs import JOB_DELIVER, JOB_EVALUATE_SYMBOL, enqueue_job, pending_job_count
from src.storage.alert_watches import get_watch, list_enabled_symbols, list_watches_for_symbol
from src.storage.database import get_connection, init_database


@pytest.fixture
def multi_user_env(tmp_path, monkeypatch):
    db_path = tmp_path / "mutation-queued-eval.db"
    monkeypatch.setenv("MARKETHELM_DATABASE_URL", f"sqlite:///{db_path.as_posix()}")
    monkeypatch.setenv("MARKETHELM_AUTH_SECRET", "test-secret-min-16-chars")

    init_database()


@pytest.fixture
def client(multi_user_env):
    return TestClient(app)


def _register(client, email: str) -> tuple[str, str]:
    response = client.post(
        "/api/auth/register",
        json={"email": email, "password": "password123"},
    )
    assert response.status_code == 200
    body = response.json()
    return body["access_token"], body["user"]["id"]


def _price_payload(
    alert_id: str,
    symbol: str,
    *,
    operator: str = "less_than",
    value: float = 150,
) -> dict:
    return {
        "defaults": {},
        "alerts": [
            {
                "id": alert_id,
                "name": alert_id,
                "enabled": True,
                "condition": {
                    "type": "price_threshold",
                    "symbol": symbol,
                    "operator": operator,
                    "value": value,
                },
                "notifications": ["log"],
            }
        ],
    }


def _watch_ids(symbol: str) -> set[tuple[str, str]]:
    return {(w["user_id"], w["alert_id"]) for w in list_watches_for_symbol(symbol)}


def _watch_row_exists(user_id: str, alert_id: str) -> bool:
    with get_connection() as conn:
        row = conn.execute(
            "SELECT 1 FROM alert_watches WHERE user_id = ? AND alert_id = ?",
            (user_id, alert_id),
        ).fetchone()
    return row is not None


def test_put_empty_config_skips_queued_evaluate_without_touching_sibling(client) -> None:
    token_a, user_a = _register(client, "delete-eval-a@example.com")
    token_b, user_b = _register(client, "delete-eval-b@example.com")
    headers_a = {"Authorization": f"Bearer {token_a}"}
    headers_b = {"Authorization": f"Bearer {token_b}"}

    saved_a = client.put(
        "/api/alerts/config",
        headers=headers_a,
        json=_price_payload("aapl_drop", "AAPL"),
    )
    saved_b = client.put(
        "/api/alerts/config",
        headers=headers_b,
        json=_price_payload("sibling-msft", "MSFT"),
    )
    assert saved_a.status_code == 200
    assert saved_b.status_code == 200
    assert _watch_row_exists(user_a, "aapl_drop")

    enqueue_job(
        JOB_EVALUATE_SYMBOL,
        {"symbol": "AAPL", "price": 100.0, "tick_id": "t-aapl"},
    )
    enqueue_job(
        JOB_EVALUATE_SYMBOL,
        {"symbol": "MSFT", "price": 100.0, "tick_id": "t-msft"},
    )

    cleared = client.put(
        "/api/alerts/config",
        headers=headers_a,
        json={"defaults": {}, "alerts": []},
    )
    assert cleared.status_code == 200
    assert cleared.json()["config"]["alerts"] == []
    assert not _watch_row_exists(user_a, "aapl_drop")
    assert get_watch(user_a, "aapl_drop") is None
    assert _watch_ids("AAPL") == set()
    assert "AAPL" not in list_enabled_symbols()
    assert _watch_ids("MSFT") == {(user_b, "sibling-msft")}
    assert list_enabled_symbols() == ["MSFT"]

    with patch("src.alerts.alert_engine.LogNotifier.send", return_value=True) as send:
        stats = process_job_queue("delete-eval-worker")

    assert stats["evaluated"] == 2
    assert stats["delivered"] == 1
    assert stats["failed"] == 0
    assert pending_job_count([JOB_DELIVER]) == 0
    send.assert_called_once()
    event = send.call_args.args[0]
    assert event["alert_id"] == "sibling-msft"
    assert event["user_id"] == user_b
    assert event["symbols"] == ["MSFT"]


def test_put_tighter_threshold_skips_queued_evaluate_without_touching_sibling(
    client,
) -> None:
    token_a, user_a = _register(client, "threshold-eval-a@example.com")
    token_b, user_b = _register(client, "threshold-eval-b@example.com")
    headers_a = {"Authorization": f"Bearer {token_a}"}
    headers_b = {"Authorization": f"Bearer {token_b}"}

    saved_a = client.put(
        "/api/alerts/config",
        headers=headers_a,
        json=_price_payload("aapl_drop", "AAPL", value=150),
    )
    saved_b = client.put(
        "/api/alerts/config",
        headers=headers_b,
        json=_price_payload("sibling-msft", "MSFT", value=150),
    )
    assert saved_a.status_code == 200
    assert saved_b.status_code == 200

    enqueue_job(
        JOB_EVALUATE_SYMBOL,
        {"symbol": "AAPL", "price": 100.0, "tick_id": "t-aapl"},
    )
    enqueue_job(
        JOB_EVALUATE_SYMBOL,
        {"symbol": "MSFT", "price": 100.0, "tick_id": "t-msft"},
    )

    tightened = client.put(
        "/api/alerts/config",
        headers=headers_a,
        json=_price_payload("aapl_drop", "AAPL", value=50),
    )
    assert tightened.status_code == 200
    assert tightened.json()["config"]["alerts"][0]["condition"]["value"] == 50
    assert _watch_ids("AAPL") == {(user_a, "aapl_drop")}
    assert _watch_ids("MSFT") == {(user_b, "sibling-msft")}

    with patch("src.alerts.alert_engine.LogNotifier.send", return_value=True) as send:
        stats = process_job_queue("threshold-eval-worker")

    assert stats["evaluated"] == 2
    assert stats["delivered"] == 1
    assert stats["failed"] == 0
    assert pending_job_count([JOB_DELIVER]) == 0
    send.assert_called_once()
    event = send.call_args.args[0]
    assert event["alert_id"] == "sibling-msft"
    assert event["user_id"] == user_b
    assert event["symbols"] == ["MSFT"]


def test_put_new_alert_id_skips_queued_old_symbol_and_delivers_new(client) -> None:
    token_a, user_a = _register(client, "replace-id-a@example.com")
    token_b, user_b = _register(client, "replace-id-b@example.com")
    headers_a = {"Authorization": f"Bearer {token_a}"}
    headers_b = {"Authorization": f"Bearer {token_b}"}

    saved_a = client.put(
        "/api/alerts/config",
        headers=headers_a,
        json=_price_payload("aapl_drop", "AAPL"),
    )
    saved_b = client.put(
        "/api/alerts/config",
        headers=headers_b,
        json=_price_payload("sibling-msft", "MSFT"),
    )
    assert saved_a.status_code == 200
    assert saved_b.status_code == 200

    enqueue_job(
        JOB_EVALUATE_SYMBOL,
        {"symbol": "AAPL", "price": 100.0, "tick_id": "t-aapl"},
    )
    enqueue_job(
        JOB_EVALUATE_SYMBOL,
        {"symbol": "GOOG", "price": 100.0, "tick_id": "t-goog"},
    )
    enqueue_job(
        JOB_EVALUATE_SYMBOL,
        {"symbol": "MSFT", "price": 100.0, "tick_id": "t-msft"},
    )

    replaced = client.put(
        "/api/alerts/config",
        headers=headers_a,
        json=_price_payload("goog_drop", "GOOG"),
    )
    assert replaced.status_code == 200
    assert [alert["id"] for alert in replaced.json()["config"]["alerts"]] == ["goog_drop"]
    assert not _watch_row_exists(user_a, "aapl_drop")
    assert get_watch(user_a, "aapl_drop") is None
    assert _watch_ids("AAPL") == set()
    assert _watch_ids("GOOG") == {(user_a, "goog_drop")}
    assert _watch_ids("MSFT") == {(user_b, "sibling-msft")}
    assert list_enabled_symbols() == ["GOOG", "MSFT"]

    with patch("src.alerts.alert_engine.LogNotifier.send", return_value=True) as send:
        stats = process_job_queue("replace-id-worker")

    assert stats["evaluated"] == 3
    assert stats["delivered"] == 2
    assert stats["failed"] == 0
    assert pending_job_count([JOB_DELIVER]) == 0
    deliveries = {
        (call.args[0]["user_id"], call.args[0]["alert_id"], tuple(call.args[0]["symbols"]))
        for call in send.call_args_list
    }
    assert deliveries == {
        (user_a, "goog_drop", ("GOOG",)),
        (user_b, "sibling-msft", ("MSFT",)),
    }

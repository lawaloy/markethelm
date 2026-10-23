"""Hosted Settings disable/retarget must drop the old watch index key.

Pause/resume and same-id symbol swaps also have to be visible to in-flight
``evaluate_symbol`` jobs: a paused or retargeted rule must not notify, and a
resumed rule must start firing again without touching a sibling tenant.
"""

from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from dashboard.backend.main import app
from src.alerts.job_processor import process_job_queue
from src.storage.alert_jobs import JOB_DELIVER, JOB_EVALUATE_SYMBOL, enqueue_job, pending_job_count
from src.storage.alert_watches import list_enabled_symbols, list_watches_for_symbol
from src.storage.database import get_connection, init_database


@pytest.fixture
def multi_user_env(tmp_path, monkeypatch):
    db_path = tmp_path / "disable-retarget.db"
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


def _price_payload(alert_id: str, symbol: str, *, enabled: bool = True) -> dict:
    return {
        "defaults": {},
        "alerts": [
            {
                "id": alert_id,
                "name": alert_id,
                "enabled": enabled,
                "condition": {
                    "type": "price_threshold",
                    "symbol": symbol,
                    "operator": "less_than",
                    "value": 150,
                },
                "notifications": ["log"],
            }
        ],
    }


def _watch_ids(symbol: str) -> set[tuple[str, str]]:
    return {(w["user_id"], w["alert_id"]) for w in list_watches_for_symbol(symbol)}


def _enabled_flag(user_id: str, alert_id: str) -> int:
    with get_connection() as conn:
        row = conn.execute(
            "SELECT enabled FROM alert_watches WHERE user_id = ? AND alert_id = ?",
            (user_id, alert_id),
        ).fetchone()
    assert row is not None
    return int(row["enabled"])


def test_put_disable_drops_watch_from_index_without_touching_sibling(client) -> None:
    token_a, user_a = _register(client, "disable-a@example.com")
    token_b, user_b = _register(client, "disable-b@example.com")
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
    assert _watch_ids("AAPL") == {(user_a, "aapl_drop")}
    assert _watch_ids("MSFT") == {(user_b, "sibling-msft")}
    assert _enabled_flag(user_a, "aapl_drop") == 1

    disabled = client.put(
        "/api/alerts/config",
        headers=headers_a,
        json=_price_payload("aapl_drop", "AAPL", enabled=False),
    )
    assert disabled.status_code == 200
    assert disabled.json()["config"]["alerts"][0]["enabled"] is False
    assert disabled.json()["config"]["alerts"][0]["id"] == "aapl_drop"

    status = client.get("/api/alerts/status", headers=headers_a)
    assert status.status_code == 200
    assert status.json()["active_watches"] == 0

    assert _watch_ids("AAPL") == set()
    assert "AAPL" not in list_enabled_symbols()
    assert _enabled_flag(user_a, "aapl_drop") == 0
    assert _watch_ids("MSFT") == {(user_b, "sibling-msft")}
    assert list_enabled_symbols() == ["MSFT"]

    sibling = client.get("/api/alerts/config", headers=headers_b)
    assert sibling.status_code == 200
    assert [alert["id"] for alert in sibling.json()["config"]["alerts"]] == ["sibling-msft"]
    assert sibling.json()["config"]["alerts"][0]["enabled"] is True


def test_put_reenable_restores_watch_index_without_touching_sibling(client) -> None:
    token_a, user_a = _register(client, "reenable-a@example.com")
    token_b, user_b = _register(client, "reenable-b@example.com")
    headers_a = {"Authorization": f"Bearer {token_a}"}
    headers_b = {"Authorization": f"Bearer {token_b}"}

    saved_a = client.put(
        "/api/alerts/config",
        headers=headers_a,
        json=_price_payload("aapl_pause", "AAPL"),
    )
    saved_b = client.put(
        "/api/alerts/config",
        headers=headers_b,
        json=_price_payload("sibling-msft", "MSFT"),
    )
    assert saved_a.status_code == 200
    assert saved_b.status_code == 200

    paused = client.put(
        "/api/alerts/config",
        headers=headers_a,
        json=_price_payload("aapl_pause", "AAPL", enabled=False),
    )
    assert paused.status_code == 200
    assert _watch_ids("AAPL") == set()
    assert _enabled_flag(user_a, "aapl_pause") == 0

    resumed = client.put(
        "/api/alerts/config",
        headers=headers_a,
        json=_price_payload("aapl_pause", "AAPL", enabled=True),
    )
    assert resumed.status_code == 200
    assert resumed.json()["config"]["alerts"][0]["enabled"] is True

    status = client.get("/api/alerts/status", headers=headers_a)
    assert status.status_code == 200
    assert status.json()["active_watches"] == 1

    assert _watch_ids("AAPL") == {(user_a, "aapl_pause")}
    assert _enabled_flag(user_a, "aapl_pause") == 1
    assert _watch_ids("MSFT") == {(user_b, "sibling-msft")}
    assert list_enabled_symbols() == ["AAPL", "MSFT"]


def test_put_reenable_queued_evaluate_delivers_without_touching_sibling(client) -> None:
    token_a, user_a = _register(client, "resume-eval-a@example.com")
    token_b, user_b = _register(client, "resume-eval-b@example.com")
    headers_a = {"Authorization": f"Bearer {token_a}"}
    headers_b = {"Authorization": f"Bearer {token_b}"}

    saved_a = client.put(
        "/api/alerts/config",
        headers=headers_a,
        json=_price_payload("aapl_pause", "AAPL"),
    )
    saved_b = client.put(
        "/api/alerts/config",
        headers=headers_b,
        json=_price_payload("sibling-msft", "MSFT"),
    )
    assert saved_a.status_code == 200
    assert saved_b.status_code == 200

    paused = client.put(
        "/api/alerts/config",
        headers=headers_a,
        json=_price_payload("aapl_pause", "AAPL", enabled=False),
    )
    assert paused.status_code == 200
    assert _watch_ids("AAPL") == set()

    resumed = client.put(
        "/api/alerts/config",
        headers=headers_a,
        json=_price_payload("aapl_pause", "AAPL", enabled=True),
    )
    assert resumed.status_code == 200
    assert _watch_ids("AAPL") == {(user_a, "aapl_pause")}

    enqueue_job(
        JOB_EVALUATE_SYMBOL,
        {"symbol": "AAPL", "price": 100.0, "tick_id": "t-aapl"},
    )
    enqueue_job(
        JOB_EVALUATE_SYMBOL,
        {"symbol": "MSFT", "price": 100.0, "tick_id": "t-msft"},
    )

    with patch("src.alerts.alert_engine.LogNotifier.send", return_value=True) as send:
        stats = process_job_queue("resume-worker")

    assert stats["evaluated"] == 2
    assert stats["delivered"] == 2
    assert stats["failed"] == 0
    assert pending_job_count([JOB_DELIVER]) == 0
    deliveries = {
        (call.args[0]["user_id"], call.args[0]["alert_id"], tuple(call.args[0]["symbols"]))
        for call in send.call_args_list
    }
    assert deliveries == {
        (user_a, "aapl_pause", ("AAPL",)),
        (user_b, "sibling-msft", ("MSFT",)),
    }


def test_put_disable_skips_queued_evaluate_without_touching_sibling(client) -> None:
    token_a, user_a = _register(client, "queued-a@example.com")
    token_b, user_b = _register(client, "queued-b@example.com")
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
        {"symbol": "MSFT", "price": 100.0, "tick_id": "t-msft"},
    )

    disabled = client.put(
        "/api/alerts/config",
        headers=headers_a,
        json=_price_payload("aapl_drop", "AAPL", enabled=False),
    )
    assert disabled.status_code == 200
    assert _watch_ids("AAPL") == set()

    with patch("src.alerts.alert_engine.LogNotifier.send", return_value=True) as send:
        stats = process_job_queue("disable-worker")

    assert stats["evaluated"] == 2
    assert stats["delivered"] == 1
    assert stats["failed"] == 0
    assert pending_job_count([JOB_DELIVER]) == 0
    send.assert_called_once()
    event = send.call_args.args[0]
    assert event["alert_id"] == "sibling-msft"
    assert event["user_id"] == user_b
    assert event["symbols"] == ["MSFT"]


def test_put_same_id_symbol_swap_drops_old_symbol_without_touching_sibling(
    client,
) -> None:
    token_a, user_a = _register(client, "retarget-a@example.com")
    token_b, user_b = _register(client, "retarget-b@example.com")
    headers_a = {"Authorization": f"Bearer {token_a}"}
    headers_b = {"Authorization": f"Bearer {token_b}"}

    saved_a = client.put(
        "/api/alerts/config",
        headers=headers_a,
        json=_price_payload("price_watch", "AAPL"),
    )
    saved_b = client.put(
        "/api/alerts/config",
        headers=headers_b,
        json=_price_payload("sibling-msft", "MSFT"),
    )
    assert saved_a.status_code == 200
    assert saved_b.status_code == 200
    assert _watch_ids("AAPL") == {(user_a, "price_watch")}
    assert _watch_ids("MSFT") == {(user_b, "sibling-msft")}

    swapped = client.put(
        "/api/alerts/config",
        headers=headers_a,
        json=_price_payload("price_watch", "GOOG"),
    )
    assert swapped.status_code == 200
    assert [alert["id"] for alert in swapped.json()["config"]["alerts"]] == ["price_watch"]
    assert swapped.json()["config"]["alerts"][0]["condition"]["symbol"] == "GOOG"

    assert _watch_ids("AAPL") == set()
    assert "AAPL" not in list_enabled_symbols()
    assert _watch_ids("GOOG") == {(user_a, "price_watch")}
    assert _watch_ids("MSFT") == {(user_b, "sibling-msft")}
    assert list_enabled_symbols() == ["GOOG", "MSFT"]

    sibling = client.get("/api/alerts/config", headers=headers_b)
    assert sibling.status_code == 200
    assert [alert["id"] for alert in sibling.json()["config"]["alerts"]] == ["sibling-msft"]


def test_put_retarget_skips_queued_old_symbol_and_delivers_new(client) -> None:
    token_a, user_a = _register(client, "retarget-eval-a@example.com")
    token_b, user_b = _register(client, "retarget-eval-b@example.com")
    headers_a = {"Authorization": f"Bearer {token_a}"}
    headers_b = {"Authorization": f"Bearer {token_b}"}

    saved_a = client.put(
        "/api/alerts/config",
        headers=headers_a,
        json=_price_payload("price_watch", "AAPL"),
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

    swapped = client.put(
        "/api/alerts/config",
        headers=headers_a,
        json=_price_payload("price_watch", "GOOG"),
    )
    assert swapped.status_code == 200
    assert _watch_ids("AAPL") == set()
    assert _watch_ids("GOOG") == {(user_a, "price_watch")}

    with patch("src.alerts.alert_engine.LogNotifier.send", return_value=True) as send:
        stats = process_job_queue("retarget-worker")

    assert stats["evaluated"] == 3
    assert stats["delivered"] == 2
    assert stats["failed"] == 0
    assert pending_job_count([JOB_DELIVER]) == 0
    deliveries = {
        (call.args[0]["user_id"], call.args[0]["alert_id"], tuple(call.args[0]["symbols"]))
        for call in send.call_args_list
    }
    assert deliveries == {
        (user_a, "price_watch", ("GOOG",)),
        (user_b, "sibling-msft", ("MSFT",)),
    }

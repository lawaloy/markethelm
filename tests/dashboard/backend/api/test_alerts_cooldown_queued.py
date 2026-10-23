"""Hosted cooldown PUTs must take effect for already-queued evaluate and deliver jobs.

Threshold tighten (#456) reloads ``alert_json``. Cooldown is a dedicated watch
column read by ``_within_cooldown`` at both evaluate and deliver time. A Settings
write that lengthens cooldown after a recent trigger must suppress that tenant
without touching a sibling.
"""

from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from dashboard.backend.main import app
from src.alerts.job_processor import process_job_queue
from src.storage.alert_jobs import JOB_DELIVER, JOB_EVALUATE_SYMBOL, enqueue_job, pending_job_count
from src.storage.alert_watches import get_watch, record_trigger
from src.storage.database import init_database


@pytest.fixture
def multi_user_env(tmp_path, monkeypatch):
    db_path = tmp_path / "cooldown-queued.db"
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


def _price_payload(alert_id: str, symbol: str, *, cooldown_minutes: int = 0) -> dict:
    return {
        "defaults": {},
        "alerts": [
            {
                "id": alert_id,
                "name": alert_id,
                "enabled": True,
                "cooldown_minutes": cooldown_minutes,
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


def _recent_trigger_ts() -> str:
    return (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat()


def test_put_longer_cooldown_skips_queued_evaluate_without_touching_sibling(client) -> None:
    token_a, user_a = _register(client, "cooldown-eval-a@example.com")
    token_b, user_b = _register(client, "cooldown-eval-b@example.com")
    headers_a = {"Authorization": f"Bearer {token_a}"}
    headers_b = {"Authorization": f"Bearer {token_b}"}

    saved_a = client.put(
        "/api/alerts/config",
        headers=headers_a,
        json=_price_payload("aapl_drop", "AAPL", cooldown_minutes=0),
    )
    saved_b = client.put(
        "/api/alerts/config",
        headers=headers_b,
        json=_price_payload("sibling-msft", "MSFT", cooldown_minutes=0),
    )
    assert saved_a.status_code == 200
    assert saved_b.status_code == 200
    assert get_watch(user_a, "aapl_drop")["cooldown_minutes"] == 0

    record_trigger(user_a, "aapl_drop", timestamp=_recent_trigger_ts())
    enqueue_job(
        JOB_EVALUATE_SYMBOL,
        {"symbol": "AAPL", "price": 100.0, "tick_id": "t-aapl"},
    )
    enqueue_job(
        JOB_EVALUATE_SYMBOL,
        {"symbol": "MSFT", "price": 100.0, "tick_id": "t-msft"},
    )

    lengthened = client.put(
        "/api/alerts/config",
        headers=headers_a,
        json=_price_payload("aapl_drop", "AAPL", cooldown_minutes=60),
    )
    assert lengthened.status_code == 200
    assert lengthened.json()["config"]["alerts"][0]["cooldown_minutes"] == 60
    assert get_watch(user_a, "aapl_drop")["cooldown_minutes"] == 60
    assert get_watch(user_b, "sibling-msft")["cooldown_minutes"] == 0

    with patch("src.alerts.alert_engine.LogNotifier.send", return_value=True) as send:
        stats = process_job_queue("cooldown-eval-worker")

    assert stats["evaluated"] == 2
    assert stats["delivered"] == 1
    assert stats["failed"] == 0
    assert pending_job_count([JOB_DELIVER]) == 0
    send.assert_called_once()
    event = send.call_args.args[0]
    assert event["alert_id"] == "sibling-msft"
    assert event["user_id"] == user_b
    assert event["symbols"] == ["MSFT"]


def test_put_longer_cooldown_skips_queued_deliver_without_touching_sibling(client) -> None:
    token_a, user_a = _register(client, "cooldown-deliver-a@example.com")
    token_b, user_b = _register(client, "cooldown-deliver-b@example.com")
    headers_a = {"Authorization": f"Bearer {token_a}"}
    headers_b = {"Authorization": f"Bearer {token_b}"}

    saved_a = client.put(
        "/api/alerts/config",
        headers=headers_a,
        json=_price_payload("aapl_drop", "AAPL", cooldown_minutes=0),
    )
    saved_b = client.put(
        "/api/alerts/config",
        headers=headers_b,
        json=_price_payload("sibling-msft", "MSFT", cooldown_minutes=0),
    )
    assert saved_a.status_code == 200
    assert saved_b.status_code == 200

    record_trigger(user_a, "aapl_drop", timestamp=_recent_trigger_ts())
    event_ts = datetime.now(timezone.utc).isoformat()
    enqueue_job(
        JOB_DELIVER,
        {
            "user_id": user_a,
            "alert_id": "aapl_drop",
            "event": {
                "alert_id": "aapl_drop",
                "alert_name": "aapl_drop",
                "symbols": ["AAPL"],
                "timestamp": event_ts,
                "condition_type": "price_threshold",
                "user_id": user_a,
            },
        },
    )
    enqueue_job(
        JOB_DELIVER,
        {
            "user_id": user_b,
            "alert_id": "sibling-msft",
            "event": {
                "alert_id": "sibling-msft",
                "alert_name": "sibling-msft",
                "symbols": ["MSFT"],
                "timestamp": event_ts,
                "condition_type": "price_threshold",
                "user_id": user_b,
            },
        },
    )

    lengthened = client.put(
        "/api/alerts/config",
        headers=headers_a,
        json=_price_payload("aapl_drop", "AAPL", cooldown_minutes=60),
    )
    assert lengthened.status_code == 200
    assert get_watch(user_a, "aapl_drop")["cooldown_minutes"] == 60

    with patch("src.alerts.alert_engine.LogNotifier.send", return_value=True) as send:
        stats = process_job_queue("cooldown-deliver-worker")

    assert stats["evaluated"] == 0
    assert stats["delivered"] == 1
    assert stats["failed"] == 0
    assert pending_job_count([JOB_DELIVER]) == 0
    send.assert_called_once()
    event = send.call_args.args[0]
    assert event["alert_id"] == "sibling-msft"
    assert event["user_id"] == user_b
    assert event["symbols"] == ["MSFT"]

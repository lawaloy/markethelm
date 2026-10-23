"""Hosted email PUTs must take effect for already-queued deliver jobs.

Webhook channel drop and URL rotation (#458–#459) re-read ``alert_json`` at
deliver time. Email recipients are the same path through
``apply_alert_defaults`` / ``EmailNotifier.from_alert``. Turning email off
after a job is already queued must not SendGrid-POST. Rotating ``email_to``
must POST only to the new address. A sibling tenant must still notify its
own mailbox, never a process-wide ``ALERT_EMAIL_TO``.
"""

from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from dashboard.backend.main import app
from src.alerts.job_processor import process_job_queue
from src.storage.alert_jobs import JOB_DELIVER, enqueue_job, pending_job_count
from src.storage.alert_watches import get_watch
from src.storage.database import init_database

GLOBAL_MAILBOX = "global-shared@example.com"


@pytest.fixture
def multi_user_env(tmp_path, monkeypatch):
    db_path = tmp_path / "email-notifications-queued.db"
    monkeypatch.setenv("MARKETHELM_DATABASE_URL", f"sqlite:///{db_path.as_posix()}")
    monkeypatch.setenv("MARKETHELM_AUTH_SECRET", "test-secret-min-16-chars")
    monkeypatch.setenv("ALERT_EMAIL_PROVIDER", "sendgrid")
    monkeypatch.setenv("SENDGRID_API_KEY", "sg-test-key")
    monkeypatch.setenv("ALERT_EMAIL_FROM", "alerts@markethelm.example")
    monkeypatch.setenv("ALERT_EMAIL_TO", GLOBAL_MAILBOX)

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


def _email_payload(
    alert_id: str,
    symbol: str,
    email_to: str,
    *,
    notifications: list[str],
) -> dict:
    return {
        "defaults": {
            "email_to": email_to,
            "notify_email": "email" in notifications,
        },
        "alerts": [
            {
                "id": alert_id,
                "name": alert_id,
                "enabled": True,
                "condition": {
                    "type": "price_threshold",
                    "symbol": symbol,
                    "operator": "less_than",
                    "value": 150,
                },
                "notifications": notifications,
            }
        ],
    }


def _deliver_job(user_id: str, alert_id: str, symbol: str, event_ts: str) -> dict:
    return {
        "user_id": user_id,
        "alert_id": alert_id,
        "event": {
            "alert_id": alert_id,
            "alert_name": alert_id,
            "symbols": [symbol],
            "timestamp": event_ts,
            "condition_type": "price_threshold",
            "user_id": user_id,
        },
    }


def _sendgrid_recipients(mock_post: MagicMock) -> list[str]:
    emails: list[str] = []
    for call in mock_post.call_args_list:
        payload = call.kwargs["json"]
        emails.extend(item["email"] for item in payload["personalizations"][0]["to"])
    return emails


def test_put_drops_email_skips_queued_deliver_without_touching_sibling(client) -> None:
    token_a, user_a = _register(client, "email-deliver-a@example.com")
    token_b, user_b = _register(client, "email-deliver-b@example.com")
    headers_a = {"Authorization": f"Bearer {token_a}"}
    headers_b = {"Authorization": f"Bearer {token_b}"}
    addr_a = "tenant-a@example.com"
    addr_b = "tenant-b@example.com"

    saved_a = client.put(
        "/api/alerts/config",
        headers=headers_a,
        json=_email_payload("aapl_drop", "AAPL", addr_a, notifications=["log", "email"]),
    )
    saved_b = client.put(
        "/api/alerts/config",
        headers=headers_b,
        json=_email_payload("sibling-msft", "MSFT", addr_b, notifications=["email"]),
    )
    assert saved_a.status_code == 200
    assert saved_b.status_code == 200
    assert get_watch(user_a, "aapl_drop")["alert"]["notifications"] == ["log", "email"]
    assert get_watch(user_a, "aapl_drop")["defaults"]["email_to"] == addr_a

    event_ts = datetime.now(timezone.utc).isoformat()
    enqueue_job(JOB_DELIVER, _deliver_job(user_a, "aapl_drop", "AAPL", event_ts))
    enqueue_job(JOB_DELIVER, _deliver_job(user_b, "sibling-msft", "MSFT", event_ts))

    silenced = client.put(
        "/api/alerts/config",
        headers=headers_a,
        json=_email_payload("aapl_drop", "AAPL", addr_a, notifications=["log"]),
    )
    assert silenced.status_code == 200
    assert get_watch(user_a, "aapl_drop")["alert"]["notifications"] == ["log"]
    assert get_watch(user_a, "aapl_drop")["defaults"]["email_to"] == addr_a
    assert get_watch(user_b, "sibling-msft")["alert"]["notifications"] == ["email"]

    with patch("src.alerts.alert_engine.LogNotifier.send", return_value=True) as send_log:
        with patch("src.alerts.notifiers.email_delivery.requests.post") as mock_post:
            mock_post.return_value.status_code = 202
            stats = process_job_queue("email-deliver-worker")

    assert stats["evaluated"] == 0
    assert stats["delivered"] == 2
    assert stats["failed"] == 0
    assert pending_job_count([JOB_DELIVER]) == 0
    send_log.assert_called_once()
    log_event = send_log.call_args.args[0]
    assert log_event["alert_id"] == "aapl_drop"
    assert log_event["user_id"] == user_a
    assert _sendgrid_recipients(mock_post) == [addr_b]
    assert addr_a not in _sendgrid_recipients(mock_post)
    assert GLOBAL_MAILBOX not in _sendgrid_recipients(mock_post)


def test_put_rotates_email_to_retargets_queued_deliver_without_touching_sibling(
    client,
) -> None:
    token_a, user_a = _register(client, "email-rotate-a@example.com")
    token_b, user_b = _register(client, "email-rotate-b@example.com")
    headers_a = {"Authorization": f"Bearer {token_a}"}
    headers_b = {"Authorization": f"Bearer {token_b}"}
    addr_a_old = "leaked-a@example.com"
    addr_a_new = "rotated-a@example.com"
    addr_b = "tenant-b@example.com"

    saved_a = client.put(
        "/api/alerts/config",
        headers=headers_a,
        json=_email_payload("aapl_drop", "AAPL", addr_a_old, notifications=["email"]),
    )
    saved_b = client.put(
        "/api/alerts/config",
        headers=headers_b,
        json=_email_payload("sibling-msft", "MSFT", addr_b, notifications=["email"]),
    )
    assert saved_a.status_code == 200
    assert saved_b.status_code == 200
    assert get_watch(user_a, "aapl_drop")["defaults"]["email_to"] == addr_a_old

    event_ts = datetime.now(timezone.utc).isoformat()
    enqueue_job(JOB_DELIVER, _deliver_job(user_a, "aapl_drop", "AAPL", event_ts))
    enqueue_job(JOB_DELIVER, _deliver_job(user_b, "sibling-msft", "MSFT", event_ts))

    rotated = client.put(
        "/api/alerts/config",
        headers=headers_a,
        json=_email_payload("aapl_drop", "AAPL", addr_a_new, notifications=["email"]),
    )
    assert rotated.status_code == 200
    assert get_watch(user_a, "aapl_drop")["defaults"]["email_to"] == addr_a_new
    assert get_watch(user_b, "sibling-msft")["defaults"]["email_to"] == addr_b

    with patch("src.alerts.notifiers.email_delivery.requests.post") as mock_post:
        mock_post.return_value.status_code = 202
        stats = process_job_queue("email-rotate-worker")

    assert stats["evaluated"] == 0
    assert stats["delivered"] == 2
    assert stats["failed"] == 0
    assert pending_job_count([JOB_DELIVER]) == 0
    posted = _sendgrid_recipients(mock_post)
    assert mock_post.call_count == 2
    assert set(posted) == {addr_a_new, addr_b}
    assert addr_a_old not in posted
    assert GLOBAL_MAILBOX not in posted

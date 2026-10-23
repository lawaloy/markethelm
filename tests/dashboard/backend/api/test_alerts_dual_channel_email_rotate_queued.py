"""Hosted dual-channel email_to PUTs must retarget queued deliver without dropping webhook.

Single-channel mailbox rotation (#460) never walks a webhook remainder.
Unmodified dual-channel deliver (#466) never mutates ``defaults.email_to``.
Rotating the inbox on ``['log','email','webhook']`` after a job is queued must
SendGrid the new mailbox AND still POST the stored webhook. A sibling must
still notify its own URL. Process-wide ``ALERT_EMAIL_TO`` /
``ALERT_WEBHOOK_URL`` must not leak into hosted sends.
"""

from datetime import datetime, timezone
from unittest.mock import MagicMock, patch
from urllib.parse import urlparse

import pytest
from fastapi.testclient import TestClient

from dashboard.backend.main import app
from src.alerts.job_processor import process_job_queue
from src.storage.alert_jobs import JOB_DELIVER, enqueue_job, pending_job_count
from src.storage.alert_watches import get_watch
from src.storage.database import init_database

GLOBAL_MAILBOX = "global-shared@example.com"
GLOBAL_WEBHOOK = "https://hooks.example/global-shared"


@pytest.fixture
def multi_user_env(tmp_path, monkeypatch):
    db_path = tmp_path / "dual-channel-email-rotate.db"
    monkeypatch.setenv("MARKETHELM_DATABASE_URL", f"sqlite:///{db_path.as_posix()}")
    monkeypatch.setenv("MARKETHELM_AUTH_SECRET", "test-secret-min-16-chars")
    monkeypatch.setenv("ALERT_EMAIL_PROVIDER", "sendgrid")
    monkeypatch.setenv("SENDGRID_API_KEY", "sg-test-key")
    monkeypatch.setenv("ALERT_EMAIL_FROM", "alerts@markethelm.example")
    monkeypatch.setenv("ALERT_EMAIL_TO", GLOBAL_MAILBOX)
    monkeypatch.setenv("ALERT_WEBHOOK_URL", GLOBAL_WEBHOOK)
    monkeypatch.setenv("ALERT_WEBHOOK_FORMAT", "slack")

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


def _dual_payload(
    alert_id: str,
    symbol: str,
    *,
    email_to: str,
    webhook_url: str,
    notifications: list[str],
) -> dict:
    return {
        "defaults": {
            "email_to": email_to,
            "webhook_url": webhook_url,
            "webhook_format": "json",
            "notify_email": "email" in notifications,
            "notify_webhook": "webhook" in notifications,
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


def _post_side_effect(url, *args, **kwargs):
    response = MagicMock()
    response.status_code = 202 if "sendgrid" in str(url) else 200
    return response


def _webhook_urls(mock_post: MagicMock) -> list[str]:
    urls: list[str] = []
    for call in mock_post.call_args_list:
        if not call.args:
            continue
        raw = str(call.args[0])
        # Hostname equality — a URL prefix startswith matches hooks.example.evil.
        if (urlparse(raw).hostname or "").lower() == "hooks.example":
            urls.append(raw)
    return urls


def _sendgrid_recipients(mock_post: MagicMock) -> list[str]:
    emails: list[str] = []
    for call in mock_post.call_args_list:
        if not call.args or "sendgrid" not in str(call.args[0]):
            continue
        payload = call.kwargs["json"]
        emails.extend(item["email"] for item in payload["personalizations"][0]["to"])
    return emails


def test_put_rotates_email_to_keeps_webhook_queued_deliver_without_touching_sibling(
    client,
) -> None:
    """email_to rotation on a dual-channel watch must not drop webhook.

    ``apply_alert_defaults`` seeds the new mailbox at deliver time while
    ``_build_notifiers`` still walks ``email`` and ``webhook``. A PUT that
    rewrites notifications when only the inbox changes would skip Discord or
    leak process-wide mailbox/URL env (hosted ``allow_env_*`` is off).
    """
    token_a, user_a = _register(client, "dual-email-rot-a@example.com")
    token_b, user_b = _register(client, "dual-email-rot-b@example.com")
    headers_a = {"Authorization": f"Bearer {token_a}"}
    headers_b = {"Authorization": f"Bearer {token_b}"}
    addr_a_old = "leaked-a@example.com"
    addr_a_new = "rotated-a@example.com"
    addr_b = "tenant-b@example.com"
    url_a = "https://hooks.example/tenant-a"
    url_b = "https://hooks.example/tenant-b"

    saved_a = client.put(
        "/api/alerts/config",
        headers=headers_a,
        json=_dual_payload(
            "aapl_drop",
            "AAPL",
            email_to=addr_a_old,
            webhook_url=url_a,
            notifications=["log", "email", "webhook"],
        ),
    )
    saved_b = client.put(
        "/api/alerts/config",
        headers=headers_b,
        json=_dual_payload(
            "sibling-msft",
            "MSFT",
            email_to=addr_b,
            webhook_url=url_b,
            notifications=["webhook"],
        ),
    )
    assert saved_a.status_code == 200
    assert saved_b.status_code == 200
    assert get_watch(user_a, "aapl_drop")["alert"]["notifications"] == [
        "log",
        "email",
        "webhook",
    ]
    assert get_watch(user_a, "aapl_drop")["defaults"]["email_to"] == addr_a_old
    assert get_watch(user_a, "aapl_drop")["defaults"]["webhook_url"] == url_a
    assert get_watch(user_b, "sibling-msft")["alert"]["notifications"] == ["webhook"]

    event_ts = datetime.now(timezone.utc).isoformat()
    enqueue_job(JOB_DELIVER, _deliver_job(user_a, "aapl_drop", "AAPL", event_ts))
    enqueue_job(JOB_DELIVER, _deliver_job(user_b, "sibling-msft", "MSFT", event_ts))

    rotated = client.put(
        "/api/alerts/config",
        headers=headers_a,
        json=_dual_payload(
            "aapl_drop",
            "AAPL",
            email_to=addr_a_new,
            webhook_url=url_a,
            notifications=["log", "email", "webhook"],
        ),
    )
    assert rotated.status_code == 200
    assert get_watch(user_a, "aapl_drop")["alert"]["notifications"] == [
        "log",
        "email",
        "webhook",
    ]
    assert get_watch(user_a, "aapl_drop")["defaults"]["email_to"] == addr_a_new
    assert get_watch(user_a, "aapl_drop")["defaults"]["webhook_url"] == url_a
    assert get_watch(user_b, "sibling-msft")["defaults"]["email_to"] == addr_b
    assert get_watch(user_b, "sibling-msft")["defaults"]["webhook_url"] == url_b

    with patch("src.alerts.alert_engine.LogNotifier.send", return_value=True) as send_log:
        # webhook_notifier and email_delivery both `import requests`; one patch
        # covers both POSTs. Split by URL so a leftover env send cannot hide
        # behind the SendGrid mock.
        with patch(
            "src.alerts.notifiers.webhook_notifier.requests.post",
            side_effect=_post_side_effect,
        ) as mock_post:
            stats = process_job_queue("dual-channel-email-rotate-worker")

    assert stats["evaluated"] == 0
    assert stats["delivered"] == 2
    assert stats["failed"] == 0
    assert pending_job_count([JOB_DELIVER]) == 0
    send_log.assert_called_once()
    log_event = send_log.call_args.args[0]
    assert log_event["alert_id"] == "aapl_drop"
    assert log_event["user_id"] == user_a
    posted = _webhook_urls(mock_post)
    assert set(posted) == {url_a, url_b}
    assert len(posted) == 2
    assert GLOBAL_WEBHOOK not in posted
    mailed = _sendgrid_recipients(mock_post)
    assert mailed == [addr_a_new]
    assert addr_a_old not in mailed
    assert addr_b not in mailed
    assert GLOBAL_MAILBOX not in mailed

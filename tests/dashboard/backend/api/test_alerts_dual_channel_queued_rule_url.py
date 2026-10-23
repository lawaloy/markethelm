"""Queued dual-channel deliver must merge Discord format when URL is on the rule.

Queued deliver (#466) and format rotation (#469) put ``webhook_url`` on
``defaults`` with the format, so nesting the format copy inside the
missing-URL branch still POSTs Discord. Hosted Discord rule-URL send (#480)
and dry-run (#481) never walk the job queue. Hand-edited / preserved
rule-level URLs keep the secret on the alert and Discord on defaults — a
gated merge then falls through to hosted JSON (env Slack is opted out) or
skips the webhook if the rule URL is ignored. This locks that split on
queued deliver: POST Discord ``content`` to the rule URL, still SendGrid
the tenant mailbox, sibling unused.
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
GLOBAL_DISCORD = "https://discord.com/api/webhooks/global/token"


@pytest.fixture
def multi_user_env(tmp_path, monkeypatch):
    db_path = tmp_path / "dual-channel-queued-rule-url.db"
    monkeypatch.setenv("MARKETHELM_DATABASE_URL", f"sqlite:///{db_path.as_posix()}")
    monkeypatch.setenv("MARKETHELM_AUTH_SECRET", "test-secret-min-16-chars")
    monkeypatch.setenv("ALERT_EMAIL_PROVIDER", "sendgrid")
    monkeypatch.setenv("SENDGRID_API_KEY", "sg-test-key")
    monkeypatch.setenv("ALERT_EMAIL_FROM", "alerts@markethelm.example")
    monkeypatch.setenv("ALERT_EMAIL_TO", GLOBAL_MAILBOX)
    monkeypatch.setenv("ALERT_WEBHOOK_URL", GLOBAL_WEBHOOK)
    monkeypatch.setenv("DISCORD_WEBHOOK_URL", GLOBAL_DISCORD)
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
    webhook_format: str = "discord",
) -> dict:
    return {
        "defaults": {
            "email_to": email_to,
            "webhook_format": webhook_format,
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
                "webhook_url": webhook_url,
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


def _webhook_bodies(mock_post: MagicMock) -> dict[str, dict]:
    bodies: dict[str, dict] = {}
    for call in mock_post.call_args_list:
        if not call.args:
            continue
        raw = str(call.args[0])
        # Hostname equality — a URL prefix startswith matches hooks.example.evil.
        if (urlparse(raw).hostname or "").lower() == "hooks.example":
            bodies[raw] = call.kwargs["json"]
    return bodies


def _sendgrid_recipients(mock_post: MagicMock) -> list[str]:
    emails: list[str] = []
    for call in mock_post.call_args_list:
        if not call.args or "sendgrid" not in str(call.args[0]):
            continue
        payload = call.kwargs["json"]
        emails.extend(item["email"] for item in payload["personalizations"][0]["to"])
    return emails


def test_queued_deliver_merges_discord_format_when_url_is_on_the_rule(client) -> None:
    """Rule-level URL must not skip copying defaults.webhook_format on queued send.

    ``apply_alert_defaults`` copies URL and format independently. Gating format
    on a missing URL would still pass #466/#469 (both live on defaults) but this
    POST would be JSON ``alert_id`` (hosted fallback) or Slack ``text``/``blocks``
    if env leaked — or skip the webhook if the rule URL is ignored. Hosted
    Discord rule-URL send (#480) never drains ``JOB_DELIVER``. ``send`` must
    POST Discord ``content`` to the rule URL, SendGrid the tenant mailbox, and
    leave sibling/env destinations unused.
    """
    token_a, user_a = _register(client, "dual-queued-rule-url-a@example.com")
    token_b, user_b = _register(client, "dual-queued-rule-url-b@example.com")
    headers_a = {"Authorization": f"Bearer {token_a}"}
    headers_b = {"Authorization": f"Bearer {token_b}"}
    addr_a = "tenant-a@example.com"
    addr_b = "tenant-b@example.com"
    url_a = "https://hooks.example/tenant-a-rule"
    url_b = "https://hooks.example/tenant-b-rule"

    saved_a = client.put(
        "/api/alerts/config",
        headers=headers_a,
        json=_dual_payload(
            "aapl_drop",
            "AAPL",
            email_to=addr_a,
            webhook_url=url_a,
            notifications=["log", "email", "webhook"],
            webhook_format="discord",
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
            webhook_format="json",
        ),
    )
    assert saved_a.status_code == 200
    assert saved_b.status_code == 200
    watch_a = get_watch(user_a, "aapl_drop")
    watch_b = get_watch(user_b, "sibling-msft")
    assert watch_a["alert"]["notifications"] == ["log", "email", "webhook"]
    assert watch_a["defaults"]["email_to"] == addr_a
    assert watch_a["defaults"]["webhook_format"] == "discord"
    assert not str(watch_a["defaults"].get("webhook_url") or "").strip()
    assert watch_a["alert"]["webhook_url"] == url_a
    assert watch_b["alert"]["notifications"] == ["webhook"]
    assert watch_b["defaults"]["webhook_format"] == "json"
    assert not str(watch_b["defaults"].get("webhook_url") or "").strip()
    assert watch_b["alert"]["webhook_url"] == url_b

    event_ts = datetime.now(timezone.utc).isoformat()
    enqueue_job(JOB_DELIVER, _deliver_job(user_a, "aapl_drop", "AAPL", event_ts))
    enqueue_job(JOB_DELIVER, _deliver_job(user_b, "sibling-msft", "MSFT", event_ts))

    with patch("src.alerts.alert_engine.LogNotifier.send", return_value=True) as send_log:
        # webhook_notifier and email_delivery both `import requests`; one patch
        # covers both POSTs. Split by URL so a leftover env send cannot hide
        # behind the SendGrid mock.
        with patch(
            "src.alerts.notifiers.webhook_notifier.requests.post",
            side_effect=_post_side_effect,
        ) as mock_post:
            stats = process_job_queue("dual-channel-queued-rule-url-worker")

    assert stats["evaluated"] == 0
    assert stats["delivered"] == 2
    assert stats["failed"] == 0
    assert pending_job_count([JOB_DELIVER]) == 0
    send_log.assert_called_once()
    log_event = send_log.call_args.args[0]
    assert log_event["alert_id"] == "aapl_drop"
    assert log_event["user_id"] == user_a
    bodies = _webhook_bodies(mock_post)
    assert set(bodies) == {url_a, url_b}
    assert GLOBAL_WEBHOOK not in bodies
    discord_body = bodies[url_a]
    json_body = bodies[url_b]
    assert "content" in discord_body
    assert "alert_id" not in discord_body
    assert "text" not in discord_body
    assert "blocks" not in discord_body
    assert "aapl_drop" in discord_body["content"]
    assert "AAPL" in discord_body["content"]
    assert json_body["alert_id"] == "sibling-msft"
    assert json_body["symbols"] == ["MSFT"]
    assert "content" not in json_body
    assert "blocks" not in json_body
    mailed = _sendgrid_recipients(mock_post)
    assert mailed == [addr_a]
    assert addr_b not in mailed
    assert GLOBAL_MAILBOX not in mailed

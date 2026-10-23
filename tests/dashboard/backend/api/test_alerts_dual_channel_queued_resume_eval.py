"""Queued evaluate must deliver a resumed dual-channel watch over HTTP.

Log-only resume (#455) only observes ``LogNotifier``. Dual-channel queued
deliver (#466) and Slack rule-URL deliver (#483) enqueue ``JOB_DELIVER``
directly, so they never walk the evaluate index after a Settings resume.
Hosted ``/run`` (#472) never drains the worker. Pause evaluate skip (#484)
proves an in-flight tick is dropped while ``enabled=0`` — the same queued
jobs must still SendGrid the tenant mailbox and POST its webhook after
resume, without env Discord/Slack/mailbox leak, while the sibling
webhook-only tenant still POSTs its own URL.
"""

from unittest.mock import MagicMock, patch
from urllib.parse import urlparse

import pytest
from fastapi.testclient import TestClient

from dashboard.backend.main import app
from src.alerts.job_processor import process_job_queue
from src.storage.alert_jobs import JOB_DELIVER, JOB_EVALUATE_SYMBOL, enqueue_job, pending_job_count
from src.storage.alert_watches import get_watch, list_enabled_symbols, list_watches_for_symbol
from src.storage.database import get_connection, init_database

GLOBAL_MAILBOX = "global-shared@example.com"
GLOBAL_WEBHOOK = "https://hooks.example/global-shared"
GLOBAL_DISCORD = "https://discord.com/api/webhooks/global/token"


@pytest.fixture
def multi_user_env(tmp_path, monkeypatch):
    db_path = tmp_path / "dual-channel-queued-resume-eval.db"
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
    enabled: bool = True,
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
                "enabled": enabled,
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


def _enabled_flag(user_id: str, alert_id: str) -> int:
    with get_connection() as conn:
        row = conn.execute(
            "SELECT enabled FROM alert_watches WHERE user_id = ? AND alert_id = ?",
            (user_id, alert_id),
        ).fetchone()
    assert row is not None
    return int(row["enabled"])


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


def _discord_env_urls(mock_post: MagicMock) -> list[str]:
    urls: list[str] = []
    for call in mock_post.call_args_list:
        if not call.args:
            continue
        raw = str(call.args[0])
        if (urlparse(raw).hostname or "").lower() == "discord.com":
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


def test_queued_evaluate_delivers_resumed_dual_channel_without_touching_sibling(
    client,
) -> None:
    """In-flight evaluate after pause+resume must notify the dual-channel watch.

    ``list_watches_for_symbol`` filters ``enabled=1``. Log-only resume never
    observes SendGrid or webhook POSTs, so a merge that still copies defaults
    onto a resumed row would still pass them. Enqueue ticks first, then PUT
    ``enabled=False`` and ``enabled=True`` so the worker sees the restored
    index. Sibling webhook-only must still POST its URL. Do not enqueue
    ``JOB_DELIVER`` for the paused window — that path is a known hole
    (``get_watch`` has no enabled filter) and must not be locked here.
    """
    token_a, user_a = _register(client, "dual-resume-eval-a@example.com")
    token_b, user_b = _register(client, "dual-resume-eval-b@example.com")
    headers_a = {"Authorization": f"Bearer {token_a}"}
    headers_b = {"Authorization": f"Bearer {token_b}"}
    addr_a = "tenant-a@example.com"
    addr_b = "tenant-b@example.com"
    url_a = "https://hooks.example/tenant-a"
    url_b = "https://hooks.example/tenant-b"

    saved_a = client.put(
        "/api/alerts/config",
        headers=headers_a,
        json=_dual_payload(
            "aapl_drop",
            "AAPL",
            email_to=addr_a,
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
    assert get_watch(user_a, "aapl_drop")["defaults"]["email_to"] == addr_a
    assert get_watch(user_a, "aapl_drop")["defaults"]["webhook_url"] == url_a
    assert get_watch(user_b, "sibling-msft")["alert"]["notifications"] == ["webhook"]
    assert _enabled_flag(user_a, "aapl_drop") == 1

    enqueue_job(
        JOB_EVALUATE_SYMBOL,
        {"symbol": "AAPL", "price": 100.0, "tick_id": "t-aapl"},
    )
    enqueue_job(
        JOB_EVALUATE_SYMBOL,
        {"symbol": "MSFT", "price": 100.0, "tick_id": "t-msft"},
    )

    paused = client.put(
        "/api/alerts/config",
        headers=headers_a,
        json=_dual_payload(
            "aapl_drop",
            "AAPL",
            email_to=addr_a,
            webhook_url=url_a,
            notifications=["log", "email", "webhook"],
            enabled=False,
        ),
    )
    assert paused.status_code == 200
    assert _enabled_flag(user_a, "aapl_drop") == 0
    assert list_watches_for_symbol("AAPL") == []
    assert "AAPL" not in list_enabled_symbols()

    resumed = client.put(
        "/api/alerts/config",
        headers=headers_a,
        json=_dual_payload(
            "aapl_drop",
            "AAPL",
            email_to=addr_a,
            webhook_url=url_a,
            notifications=["log", "email", "webhook"],
            enabled=True,
        ),
    )
    assert resumed.status_code == 200
    assert resumed.json()["config"]["alerts"][0]["enabled"] is True
    assert _enabled_flag(user_a, "aapl_drop") == 1
    assert {(w["user_id"], w["alert_id"]) for w in list_watches_for_symbol("AAPL")} == {
        (user_a, "aapl_drop")
    }
    assert {(w["user_id"], w["alert_id"]) for w in list_watches_for_symbol("MSFT")} == {
        (user_b, "sibling-msft")
    }

    with patch("src.alerts.alert_engine.LogNotifier.send", return_value=True) as send_log:
        # webhook_notifier and email_delivery both `import requests`; one patch
        # covers both POSTs. Split by URL so a leftover env send cannot hide
        # behind the SendGrid mock.
        with patch(
            "src.alerts.notifiers.webhook_notifier.requests.post",
            side_effect=_post_side_effect,
        ) as mock_post:
            stats = process_job_queue("dual-channel-queued-resume-eval-worker")

    assert stats["evaluated"] == 2
    assert stats["delivered"] == 2
    assert stats["failed"] == 0
    assert pending_job_count([JOB_DELIVER, JOB_EVALUATE_SYMBOL]) == 0
    send_log.assert_called_once()
    log_event = send_log.call_args.args[0]
    assert log_event["alert_id"] == "aapl_drop"
    assert log_event["user_id"] == user_a
    posted = _webhook_urls(mock_post)
    assert set(posted) == {url_a, url_b}
    assert len(posted) == 2
    assert GLOBAL_WEBHOOK not in posted
    assert _discord_env_urls(mock_post) == []
    mailed = _sendgrid_recipients(mock_post)
    assert mailed == [addr_a]
    assert addr_b not in mailed
    assert GLOBAL_MAILBOX not in mailed

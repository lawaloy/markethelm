"""Queued evaluate must skip a tightened dual-channel watch without HTTP leaks.

Log-only threshold mutation (#456) only observes ``LogNotifier``. Dual-channel
pause evaluate (#484) drops the row from ``list_watches_for_symbol`` via
``enabled=0`` and never calls ``evaluate_price_threshold``. Resume evaluate
(#485) restores a still-matching watch. A Settings save that keeps the watch
enabled but raises the bar above the already-queued tick must not SendGrid
the tenant mailbox, POST its webhook, or log — and must still POST the
sibling tenant webhook without env Discord/Slack/mailbox leak.
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
    db_path = tmp_path / "dual-channel-queued-threshold-eval.db"
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
    value: float = 150,
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
                    "value": value,
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


def test_queued_evaluate_skips_tightened_dual_channel_without_touching_sibling(
    client,
) -> None:
    """In-flight evaluate after a tighter threshold must not notify.

    ``list_watches_for_symbol`` still returns the enabled dual-channel row;
    ``evaluate_price_threshold`` must read the post-PUT JSON (100 is no
    longer ``less_than`` 50). Log-only threshold tests never observe
    SendGrid or webhook POSTs, so a merge that still copies defaults onto
    the tightened row would still pass them. Enqueue ticks first, then PUT
    the new value so the worker sees the updated condition. Sibling
    webhook-only must still POST its URL.
    """
    token_a, user_a = _register(client, "dual-threshold-eval-a@example.com")
    token_b, user_b = _register(client, "dual-threshold-eval-b@example.com")
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
            value=150,
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
            value=150,
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
    assert get_watch(user_a, "aapl_drop")["alert"]["condition"]["value"] == 150
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

    tightened = client.put(
        "/api/alerts/config",
        headers=headers_a,
        json=_dual_payload(
            "aapl_drop",
            "AAPL",
            email_to=addr_a,
            webhook_url=url_a,
            notifications=["log", "email", "webhook"],
            value=50,
        ),
    )
    assert tightened.status_code == 200
    assert tightened.json()["config"]["alerts"][0]["condition"]["value"] == 50
    assert get_watch(user_a, "aapl_drop")["alert"]["condition"]["value"] == 50
    assert get_watch(user_a, "aapl_drop")["alert"]["notifications"] == [
        "log",
        "email",
        "webhook",
    ]
    assert _enabled_flag(user_a, "aapl_drop") == 1
    watches_a = list_watches_for_symbol("AAPL")
    assert {(w["user_id"], w["alert_id"]) for w in watches_a} == {(user_a, "aapl_drop")}
    assert watches_a[0]["threshold"] == 50
    assert watches_a[0]["alert"]["condition"]["value"] == 50
    assert "AAPL" in list_enabled_symbols()
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
            stats = process_job_queue("dual-channel-queued-threshold-eval-worker")

    assert stats["evaluated"] == 2
    assert stats["delivered"] == 1
    assert stats["failed"] == 0
    assert pending_job_count([JOB_DELIVER, JOB_EVALUATE_SYMBOL]) == 0
    send_log.assert_not_called()
    posted = _webhook_urls(mock_post)
    assert set(posted) == {url_b}
    assert len(posted) == 1
    assert url_a not in posted
    assert GLOBAL_WEBHOOK not in posted
    assert _discord_env_urls(mock_post) == []
    mailed = _sendgrid_recipients(mock_post)
    assert mailed == []
    assert addr_a not in mailed
    assert GLOBAL_MAILBOX not in mailed

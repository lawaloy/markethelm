"""Queued evaluate must deliver a newly added dual-channel watch.

Retarget (#488) keeps the same ``alert_id`` and drops AAPL from the index.
Replace-id (#489) removes ``aapl_drop`` so the old ticker skips. A Settings
save that *appends* ``goog_drop`` while keeping ``aapl_drop`` must still
SendGrid the tenant mailbox and POST its webhook for *both* symbols — and
must still POST the sibling tenant webhook without env Discord/Slack/mailbox
leak. Tenant defaults stay on the PUT so a leftover single-watch row would
still have credentials if the worker dropped the new id.
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
    db_path = tmp_path / "dual-channel-queued-add-eval.db"
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
    watches: list[tuple[str, str]],
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
            for alert_id, symbol in watches
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


def test_queued_evaluate_delivers_added_dual_channel_watch_without_touching_sibling(
    client,
) -> None:
    """In-flight evaluate after appending a second watch must notify both ids.

    ``list_watches_for_symbol`` must keep AAPL indexed under ``aapl_drop`` and
    add GOOG under ``goog_drop``. Retarget (#488) and replace-id (#489) both
    drop the old ticker, so a merge that treated every Settings save as
    replace-one would still pass those tests and silently skip SendGrid plus
    the tenant webhook for the *new* id here. Enqueue ticks first, then PUT
    both watches so the worker sees the post-save index. Sibling webhook-only
    must still POST its URL.
    """
    token_a, user_a = _register(client, "dual-add-eval-a@example.com")
    token_b, user_b = _register(client, "dual-add-eval-b@example.com")
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
            [("aapl_drop", "AAPL")],
            email_to=addr_a,
            webhook_url=url_a,
            notifications=["log", "email", "webhook"],
        ),
    )
    saved_b = client.put(
        "/api/alerts/config",
        headers=headers_b,
        json=_dual_payload(
            [("sibling-msft", "MSFT")],
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
    assert get_watch(user_a, "goog_drop") is None
    assert get_watch(user_b, "sibling-msft")["alert"]["notifications"] == ["webhook"]
    assert _enabled_flag(user_a, "aapl_drop") == 1

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

    added = client.put(
        "/api/alerts/config",
        headers=headers_a,
        json=_dual_payload(
            [("aapl_drop", "AAPL"), ("goog_drop", "GOOG")],
            email_to=addr_a,
            webhook_url=url_a,
            notifications=["log", "email", "webhook"],
        ),
    )
    assert added.status_code == 200
    assert [alert["id"] for alert in added.json()["config"]["alerts"]] == [
        "aapl_drop",
        "goog_drop",
    ]
    assert added.json()["config"]["alerts"][0]["condition"]["symbol"] == "AAPL"
    assert added.json()["config"]["alerts"][1]["condition"]["symbol"] == "GOOG"
    assert get_watch(user_a, "aapl_drop")["alert"]["condition"]["symbol"] == "AAPL"
    assert get_watch(user_a, "goog_drop")["alert"]["condition"]["symbol"] == "GOOG"
    assert get_watch(user_a, "aapl_drop")["alert"]["notifications"] == [
        "log",
        "email",
        "webhook",
    ]
    assert get_watch(user_a, "goog_drop")["alert"]["notifications"] == [
        "log",
        "email",
        "webhook",
    ]
    assert get_watch(user_a, "aapl_drop")["defaults"]["email_to"] == addr_a
    assert get_watch(user_a, "aapl_drop")["defaults"]["webhook_url"] == url_a
    assert get_watch(user_a, "goog_drop")["defaults"]["email_to"] == addr_a
    assert get_watch(user_a, "goog_drop")["defaults"]["webhook_url"] == url_a
    assert _enabled_flag(user_a, "aapl_drop") == 1
    assert _enabled_flag(user_a, "goog_drop") == 1
    assert {(w["user_id"], w["alert_id"]) for w in list_watches_for_symbol("AAPL")} == {
        (user_a, "aapl_drop")
    }
    assert {(w["user_id"], w["alert_id"]) for w in list_watches_for_symbol("GOOG")} == {
        (user_a, "goog_drop")
    }
    assert {(w["user_id"], w["alert_id"]) for w in list_watches_for_symbol("MSFT")} == {
        (user_b, "sibling-msft")
    }
    assert set(list_enabled_symbols()) == {"AAPL", "GOOG", "MSFT"}

    with patch("src.alerts.alert_engine.LogNotifier.send", return_value=True) as send_log:
        # webhook_notifier and email_delivery both `import requests`; one patch
        # covers both POSTs. Split by URL so a leftover env send cannot hide
        # behind the SendGrid mock.
        with patch(
            "src.alerts.notifiers.webhook_notifier.requests.post",
            side_effect=_post_side_effect,
        ) as mock_post:
            stats = process_job_queue("dual-channel-queued-add-eval-worker")

    assert stats["evaluated"] == 3
    assert stats["delivered"] == 3
    assert stats["failed"] == 0
    assert pending_job_count([JOB_DELIVER, JOB_EVALUATE_SYMBOL]) == 0
    assert send_log.call_count == 2
    logged_ids = [call.args[0]["alert_id"] for call in send_log.call_args_list]
    logged_users = [call.args[0]["user_id"] for call in send_log.call_args_list]
    assert set(logged_ids) == {"aapl_drop", "goog_drop"}
    assert logged_users == [user_a, user_a]
    posted = _webhook_urls(mock_post)
    assert set(posted) == {url_a, url_b}
    assert posted.count(url_a) == 2
    assert posted.count(url_b) == 1
    assert len(posted) == 3
    assert GLOBAL_WEBHOOK not in posted
    assert _discord_env_urls(mock_post) == []
    mailed = _sendgrid_recipients(mock_post)
    assert mailed.count(addr_a) == 2
    assert len(mailed) == 2
    assert addr_b not in mailed
    assert GLOBAL_MAILBOX not in mailed

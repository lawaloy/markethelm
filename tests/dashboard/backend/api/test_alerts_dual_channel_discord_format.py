"""Hosted dual-channel Discord payloads must not fall back to env Slack.

Helmtower's default webhook format is Discord. #473 proved a stored JSON
live check is not reshaped by process-wide ``ALERT_WEBHOOK_FORMAT=slack``.
That assertion still passes if ``apply_alert_defaults`` never copies
``webhook_format``: hosted ``from_alert`` already defaults to JSON when env
is opted out. A stored Discord format is the Helmtower default and fails
closed only when defaults actually merge — otherwise the POST is a JSON
event dict (or Slack ``text``/``blocks`` if env leaks). Settings "Send test"
walks ``run_alert_test``, which is a different merge path than
``AlertEngine.evaluate``.
"""

from unittest.mock import MagicMock, patch
from urllib.parse import urlparse

import pytest
from fastapi.testclient import TestClient

from dashboard.backend.main import app
from src.storage.alert_jobs import JOB_DELIVER, JOB_EVALUATE_SYMBOL, pending_job_count
from src.storage.alert_watches import get_last_triggered, get_watch
from src.storage.database import init_database

GLOBAL_MAILBOX = "global-shared@example.com"
GLOBAL_WEBHOOK = "https://hooks.example/global-shared"


@pytest.fixture
def multi_user_env(tmp_path, monkeypatch):
    db_path = tmp_path / "dual-channel-discord-format.db"
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
    cooldown_minutes: int = 0,
) -> dict:
    return {
        "defaults": {
            "email_to": email_to,
            "webhook_url": webhook_url,
            "webhook_format": "discord",
            "notify_email": "email" in notifications,
            "notify_webhook": "webhook" in notifications,
        },
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
                "notifications": notifications,
            }
        ],
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


def _webhook_bodies(mock_post: MagicMock) -> dict[str, dict]:
    bodies: dict[str, dict] = {}
    for call in mock_post.call_args_list:
        if not call.args:
            continue
        raw = str(call.args[0])
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


def _seed_dual_tenants(client, email_prefix: str):
    token_a, user_a = _register(client, f"{email_prefix}-a@example.com")
    token_b, user_b = _register(client, f"{email_prefix}-b@example.com")
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
            cooldown_minutes=60,
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
    assert get_watch(user_a, "aapl_drop")["defaults"]["webhook_format"] == "discord"
    assert get_watch(user_b, "sibling-msft")["defaults"]["webhook_format"] == "discord"
    assert get_last_triggered(user_a, "aapl_drop") is None
    return {
        "headers_a": headers_a,
        "headers_b": headers_b,
        "user_a": user_a,
        "user_b": user_b,
        "addr_a": addr_a,
        "addr_b": addr_b,
        "url_a": url_a,
        "url_b": url_b,
    }


def test_hosted_run_posts_discord_not_env_slack(client) -> None:
    """Live Check watches now must POST Discord ``content``, not env Slack.

    Hosted ``from_alert`` ignores ``ALERT_WEBHOOK_FORMAT`` unless defaults
    copy ``webhook_format``. Missing that merge yields JSON ``alert_id``,
    not Discord ``content``. Env Slack ``text``/``blocks`` must stay unused,
    email must still SendGrid the tenant mailbox, and the sibling URL must
    not be POSTed.
    """
    seeded = _seed_dual_tenants(client, "dual-discord-run")
    snapshot = (
        "2026-08-27",
        {"AAPL": 140.0},
        [{"symbol": "AAPL", "close": 140.0}],
    )
    with patch("src.alerts.alert_engine.LogNotifier.send", return_value=True) as send_log:
        with patch(
            "src.alerts.notifiers.webhook_notifier.requests.post",
            side_effect=_post_side_effect,
        ) as mock_post:
            with patch(
                "src.alerts.market_snapshot.load_market_snapshot",
                return_value=snapshot,
            ):
                with patch("src.alerts.alert_worker.run_db_worker_cycle") as mock_cycle:
                    ran = client.post("/api/alerts/run", headers=seeded["headers_a"])

    assert ran.status_code == 200
    body = ran.json()
    assert body["triggered"] == 1
    assert body["events"][0]["alert_id"] == "aapl_drop"
    assert body["events"][0].get("test") is not True
    mock_cycle.assert_not_called()
    send_log.assert_called_once()
    assert send_log.call_args.args[0].get("test") is not True
    posted = _webhook_urls(mock_post)
    assert posted == [seeded["url_a"]]
    assert seeded["url_b"] not in posted
    assert GLOBAL_WEBHOOK not in posted
    discord_body = _webhook_bodies(mock_post)[seeded["url_a"]]
    assert "content" in discord_body
    assert "aapl_drop" in discord_body["content"]
    assert "AAPL" in discord_body["content"]
    assert "(test)" not in discord_body["content"]
    assert "alert_id" not in discord_body
    assert "text" not in discord_body
    assert "blocks" not in discord_body
    mailed = _sendgrid_recipients(mock_post)
    assert mailed == [seeded["addr_a"]]
    assert seeded["addr_b"] not in mailed
    assert GLOBAL_MAILBOX not in mailed
    assert pending_job_count([JOB_DELIVER, JOB_EVALUATE_SYMBOL]) == 0
    assert get_last_triggered(seeded["user_a"], "aapl_drop") is not None
    assert get_last_triggered(seeded["user_b"], "sibling-msft") is None


def test_hosted_test_posts_discord_not_env_slack(client) -> None:
    """Send test must POST Discord ``content`` with a test label and skip cooldown.

    ``run_alert_test`` merges defaults then ``_build_notifiers``. A missed
    merge would POST JSON (hosted fallback) or Slack if env leaked. The
    payload must include ``(test)``, must not claim ``last_triggered``, and
    must still SendGrid the tenant mailbox without touching the sibling.
    """
    seeded = _seed_dual_tenants(client, "dual-discord-test")
    with patch("src.alerts.alert_engine.LogNotifier.send", return_value=True) as send_log:
        with patch(
            "src.alerts.notifiers.webhook_notifier.requests.post",
            side_effect=_post_side_effect,
        ) as mock_post:
            sent = client.post(
                "/api/alerts/test",
                json={"id": "aapl_drop", "dry_run": False},
                headers=seeded["headers_a"],
            )

    assert sent.status_code == 200
    body = sent.json()
    assert body["status"] == "sent"
    assert body["alert_id"] == "aapl_drop"
    assert set(body["notifiers"]) == {"email", "webhook"}
    send_log.assert_called_once()
    log_event = send_log.call_args.args[0]
    assert log_event.get("test") is True
    posted = _webhook_urls(mock_post)
    assert posted == [seeded["url_a"]]
    assert seeded["url_b"] not in posted
    assert GLOBAL_WEBHOOK not in posted
    discord_body = _webhook_bodies(mock_post)[seeded["url_a"]]
    assert "content" in discord_body
    assert "aapl_drop" in discord_body["content"]
    assert "(test)" in discord_body["content"]
    assert "alert_id" not in discord_body
    assert "text" not in discord_body
    assert "blocks" not in discord_body
    mailed = _sendgrid_recipients(mock_post)
    assert mailed == [seeded["addr_a"]]
    assert seeded["addr_b"] not in mailed
    assert GLOBAL_MAILBOX not in mailed
    assert pending_job_count([JOB_DELIVER]) == 0
    assert get_last_triggered(seeded["user_a"], "aapl_drop") is None
    assert get_last_triggered(seeded["user_b"], "sibling-msft") is None

    status_a = client.get("/api/alerts/status", headers=seeded["headers_a"])
    status_b = client.get("/api/alerts/status", headers=seeded["headers_b"])
    assert status_a.status_code == 200
    assert status_b.status_code == 200
    channels = {item["channel"] for item in status_a.json()["latest_deliveries"]}
    assert channels == {"email", "webhook"}
    assert all(item["test"] is True for item in status_a.json()["latest_deliveries"])
    assert status_b.json()["latest_deliveries"] == []

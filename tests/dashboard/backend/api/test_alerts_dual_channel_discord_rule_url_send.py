"""Hosted live Discord send must merge format when the webhook URL is on the rule.

Live Discord (#474) puts URL and format on ``defaults`` together, so nesting
the format copy inside the missing-URL branch still POSTs Discord. Slack
rule-URL send (#479) proves the split for Slack ``text``/``blocks``; a gated
merge would still POST JSON ``alert_id`` (hosted fallback, env Slack is opted
out) or Slack if env leaked when the stored format is Discord. Hand-edited /
preserved rule-level URLs keep the secret on the alert and Discord on
defaults — this locks that live split on both ``/run`` and ``/test``.
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
GLOBAL_DISCORD = "https://discord.com/api/webhooks/global/token"


@pytest.fixture
def multi_user_env(tmp_path, monkeypatch):
    db_path = tmp_path / "dual-channel-discord-rule-url-send.db"
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
    cooldown_minutes: int = 0,
) -> dict:
    return {
        "defaults": {
            "email_to": email_to,
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
                "webhook_url": webhook_url,
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
    watch_a = get_watch(user_a, "aapl_drop")
    assert watch_a["defaults"]["webhook_format"] == "discord"
    assert not str(watch_a["defaults"].get("webhook_url") or "").strip()
    assert watch_a["alert"]["webhook_url"] == url_a
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


def test_hosted_run_posts_discord_when_url_is_on_the_rule(client) -> None:
    """Live Check watches now must POST Discord to the rule URL, not env Slack.

    ``apply_alert_defaults`` copies URL and format independently. Gating format
    on a missing URL would still pass #474 (both live on defaults) but this
    POST would be JSON ``alert_id`` (hosted fallback) or Slack ``text``/``blocks``
    if env leaked. Slack rule-URL send (#479) cannot lock Discord ``content``.
    Email must still SendGrid the tenant mailbox.
    """
    seeded = _seed_dual_tenants(client, "dual-discord-rule-url-run")
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


def test_hosted_test_posts_discord_when_url_is_on_the_rule(client) -> None:
    """Send test must POST Discord ``(test)`` to the rule URL and skip cooldown.

    ``run_alert_test`` merges then ``send``s. A missed format copy would POST
    JSON or env Slack ``text``/``blocks`` while #474's defaults-URL Discord
    send still passed. The payload must include ``(test)``, must not claim
    ``last_triggered``, and must still SendGrid the tenant mailbox without
    touching the sibling.
    """
    seeded = _seed_dual_tenants(client, "dual-discord-rule-url-test")
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

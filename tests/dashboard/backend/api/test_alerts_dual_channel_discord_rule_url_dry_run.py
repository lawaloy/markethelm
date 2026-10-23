"""Hosted dry-run must merge Discord format when the webhook URL is on the rule.

Dual-channel Discord dry-run (#475) puts ``webhook_url`` on ``defaults`` so both
URL and format are missing on the alert. ``apply_alert_defaults`` copies them
together; nesting the format copy inside the missing-URL branch would still
pass #475. Live Discord rule-URL send (#480) POSTs, so it cannot lock the
preview/no-send contract. Slack rule-URL dry-run (#478) proves the split only
for Slack ``text``/``blocks``. Hand-edited / preserved rule-level URLs keep
the secret on the alert and Discord on defaults — a gated merge then falls
through to hosted JSON (env Slack is opted out) and the preview ships
``alert_id`` instead of Discord ``content``. This locks that split: rule URL +
defaults Discord, no HTTP, no trigger claim, sibling unused.
"""

from unittest.mock import MagicMock, patch
from urllib.parse import urlparse

import pytest
from fastapi.testclient import TestClient

from dashboard.backend.main import app
from src.storage.alert_jobs import JOB_DELIVER, pending_job_count
from src.storage.alert_watches import get_last_triggered, get_watch
from src.storage.database import init_database

GLOBAL_MAILBOX = "global-shared@example.com"
GLOBAL_WEBHOOK = "https://hooks.example/global-shared"
GLOBAL_DISCORD = "https://discord.com/api/webhooks/global/token"


@pytest.fixture
def multi_user_env(tmp_path, monkeypatch):
    db_path = tmp_path / "dual-channel-discord-rule-url-dry-run.db"
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
                "cooldown_minutes": 60,
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


def test_hosted_dry_run_merges_discord_format_when_url_is_on_the_rule(client) -> None:
    """Rule-level URL must not skip copying defaults.webhook_format on dry-run.

    ``apply_alert_defaults`` copies URL and format independently. Gating format
    on a missing URL would still pass #475 (both live on defaults) but this
    preview would be JSON ``alert_id`` (hosted fallback) or Slack ``text``/``blocks``
    if env leaked. Slack rule-URL dry-run (#478) cannot lock Discord ``content``.
    Live Discord rule-URL send (#480) POSTs, so a preview that still ``send``s
    would pass it. ``send`` must not run, ``last_triggered`` stays unset, and
    sibling/env URLs stay unused.
    """
    token_a, user_a = _register(client, "dual-discord-rule-url-dry-a@example.com")
    token_b, user_b = _register(client, "dual-discord-rule-url-dry-b@example.com")
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

    with patch("src.alerts.alert_engine.LogNotifier.send", return_value=True) as send_log:
        with patch(
            "src.alerts.notifiers.webhook_notifier.requests.post",
            side_effect=_post_side_effect,
        ) as mock_post:
            previewed = client.post(
                "/api/alerts/test",
                json={"id": "aapl_drop", "dry_run": True},
                headers=headers_a,
            )

    assert previewed.status_code == 200
    body = previewed.json()
    assert body["status"] == "dry_run"
    assert body["alert_id"] == "aapl_drop"
    assert set(body["notifiers"]) == {"email", "webhook"}
    send_log.assert_not_called()
    mock_post.assert_not_called()
    assert _webhook_urls(mock_post) == []

    previews = body["previews"]
    assert isinstance(previews, list)
    webhook_previews = [item for item in previews if item.get("notifier") == "WebhookNotifier"]
    assert len(webhook_previews) == 1
    discord_body = webhook_previews[0]["payload"]
    assert "content" in discord_body
    assert "aapl_drop" in discord_body["content"]
    assert "(test)" in discord_body["content"]
    assert "text" not in discord_body
    assert "blocks" not in discord_body
    assert "alert_id" not in discord_body

    email_previews = [item for item in previews if item.get("notifier") == "EmailNotifier"]
    assert len(email_previews) == 1

    assert pending_job_count([JOB_DELIVER]) == 0
    assert get_last_triggered(user_a, "aapl_drop") is None
    assert get_last_triggered(user_b, "sibling-msft") is None

    status_a = client.get("/api/alerts/status", headers=headers_a)
    status_b = client.get("/api/alerts/status", headers=headers_b)
    assert status_a.status_code == 200
    assert status_b.status_code == 200
    assert status_a.json()["latest_deliveries"] == []
    assert status_b.json()["latest_deliveries"] == []

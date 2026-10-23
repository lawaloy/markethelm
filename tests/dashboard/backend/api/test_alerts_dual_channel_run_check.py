"""Hosted dual-channel /api/alerts/run must fire both HTTP notifiers.

Queued both-on deliver (#466) walks ``_process_deliver``. Settings "Send test"
(#471) walks ``run_alert_test`` and must not claim a trigger. "Check watches
now" uses ``run_user_check`` / ``AlertEngine.evaluate``: it must still SendGrid
the tenant mailbox AND POST the stored webhook, must not seed process-wide
``ALERT_EMAIL_TO`` / ``ALERT_WEBHOOK_URL`` / ``ALERT_WEBHOOK_FORMAT``, must not
POST a sibling tenant's URL, must not enqueue worker jobs, and must write a
trigger so cooldown swallows a second click. A miss must stay silent. After
the cooldown window, the same live path must notify again.
"""

from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch
from urllib.parse import urlparse

import pytest
from fastapi.testclient import TestClient

from dashboard.backend.main import app
from src.storage.alert_jobs import JOB_DELIVER, JOB_EVALUATE_SYMBOL, pending_job_count
from src.storage.alert_watches import get_last_triggered, get_watch, record_trigger
from src.storage.database import init_database

GLOBAL_MAILBOX = "global-shared@example.com"
GLOBAL_WEBHOOK = "https://hooks.example/global-shared"


@pytest.fixture
def multi_user_env(tmp_path, monkeypatch):
    db_path = tmp_path / "dual-channel-run-check.db"
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
            "webhook_format": "json",
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


def test_hosted_run_fires_email_and_webhook_and_claims_cooldown(client) -> None:
    """Dual-channel Check watches now must notify both HTTP channels and start cooldown.

    ``run_user_check`` builds notifiers from the tenant config (hosted
    ``allow_env_*`` off) and records a trigger after a live match. Both
    SendGrid and the stored webhook must fire with JSON (not env Slack),
    the sibling URL must stay unused, worker jobs must not be enqueued,
    a second click during cooldown must not send again, and a click after
    the window elapses must notify both HTTP channels again.
    """
    token_a, user_a = _register(client, "dual-run-a@example.com")
    token_b, user_b = _register(client, "dual-run-b@example.com")
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
    assert get_watch(user_a, "aapl_drop")["alert"]["notifications"] == [
        "log",
        "email",
        "webhook",
    ]
    assert get_watch(user_a, "aapl_drop")["defaults"]["email_to"] == addr_a
    assert get_watch(user_a, "aapl_drop")["defaults"]["webhook_url"] == url_a
    assert get_watch(user_b, "sibling-msft")["alert"]["notifications"] == ["webhook"]
    assert get_last_triggered(user_a, "aapl_drop") is None

    snapshot = (
        "2026-08-27",
        {"AAPL": 140.0},
        [{"symbol": "AAPL", "close": 140.0}],
    )
    with patch("src.alerts.alert_engine.LogNotifier.send", return_value=True) as send_log:
        # webhook_notifier and email_delivery both `import requests`; one patch
        # covers both POSTs. Split by URL so a leftover env send cannot hide
        # behind the SendGrid mock.
        with patch(
            "src.alerts.notifiers.webhook_notifier.requests.post",
            side_effect=_post_side_effect,
        ) as mock_post:
            with patch(
                "src.alerts.market_snapshot.load_market_snapshot",
                return_value=snapshot,
            ) as load_snapshot:
                with patch("src.alerts.alert_worker.run_db_worker_cycle") as mock_cycle:
                    first = client.post("/api/alerts/run", headers=headers_a)
                    posts_after_first = len(mock_post.call_args_list)
                    second = client.post("/api/alerts/run", headers=headers_a)

    assert first.status_code == 200
    body = first.json()
    assert body["triggered"] == 1
    assert body["last_data_date"] == "2026-08-27"
    assert body["message"] is None
    assert body["events"][0]["alert_id"] == "aapl_drop"
    assert body["events"][0]["symbols"] == ["AAPL"]
    assert body["events"][0].get("test") is not True
    mock_cycle.assert_not_called()
    load_snapshot.assert_called()
    assert load_snapshot.call_args == (
        (["AAPL"],),
        {"fetch_missing_quotes": True},
    )
    send_log.assert_called_once()
    log_event = send_log.call_args.args[0]
    assert log_event["alert_id"] == "aapl_drop"
    assert log_event.get("test") is not True
    assert log_event.get("symbols") == ["AAPL"]
    posted = _webhook_urls(mock_post)
    assert posted == [url_a]
    assert url_b not in posted
    assert GLOBAL_WEBHOOK not in posted
    # Stored format is json; env ALERT_WEBHOOK_FORMAT=slack must not reshape
    # the tenant POST (hosted allow_env_webhook is off).
    json_body = _webhook_bodies(mock_post)[url_a]
    assert json_body["alert_id"] == "aapl_drop"
    assert json_body["symbols"] == ["AAPL"]
    assert json_body.get("test") is not True
    assert "content" not in json_body
    assert "text" not in json_body
    assert "blocks" not in json_body
    mailed = _sendgrid_recipients(mock_post)
    assert mailed == [addr_a]
    assert addr_b not in mailed
    assert GLOBAL_MAILBOX not in mailed
    assert pending_job_count([JOB_DELIVER, JOB_EVALUATE_SYMBOL]) == 0
    assert get_last_triggered(user_a, "aapl_drop") is not None
    assert get_last_triggered(user_b, "sibling-msft") is None

    assert second.status_code == 200
    assert second.json()["triggered"] == 0
    assert len(mock_post.call_args_list) == posts_after_first
    send_log.assert_called_once()

    past = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
    record_trigger(user_a, "aapl_drop", past)
    with patch("src.alerts.alert_engine.LogNotifier.send", return_value=True) as send_log_again:
        with patch(
            "src.alerts.notifiers.webhook_notifier.requests.post",
            side_effect=_post_side_effect,
        ) as mock_post_again:
            with patch(
                "src.alerts.market_snapshot.load_market_snapshot",
                return_value=snapshot,
            ):
                with patch("src.alerts.alert_worker.run_db_worker_cycle") as mock_cycle_again:
                    third = client.post("/api/alerts/run", headers=headers_a)

    assert third.status_code == 200
    assert third.json()["triggered"] == 1
    mock_cycle_again.assert_not_called()
    send_log_again.assert_called_once()
    assert _webhook_urls(mock_post_again) == [url_a]
    elapsed_body = _webhook_bodies(mock_post_again)[url_a]
    assert elapsed_body["alert_id"] == "aapl_drop"
    assert "text" not in elapsed_body
    assert "blocks" not in elapsed_body
    assert _sendgrid_recipients(mock_post_again) == [addr_a]
    assert pending_job_count([JOB_DELIVER, JOB_EVALUATE_SYMBOL]) == 0

    status_a = client.get("/api/alerts/status", headers=headers_a)
    status_b = client.get("/api/alerts/status", headers=headers_b)
    assert status_a.status_code == 200
    assert status_b.status_code == 200
    channels = {item["channel"] for item in status_a.json()["latest_deliveries"]}
    assert channels == {"email", "webhook"}
    assert all(item["test"] is False for item in status_a.json()["latest_deliveries"])
    assert all(item["alert_id"] == "aapl_drop" for item in status_a.json()["latest_deliveries"])
    assert status_b.json()["latest_deliveries"] == []


def test_hosted_run_miss_does_not_notify_or_claim_cooldown(client) -> None:
    """A dual-channel miss must not HTTP-notify, enqueue jobs, or start cooldown.

    ``evaluate`` only delivers on a match. A price above the less-than
    threshold must leave both HTTP channels silent, must not write
    ``last_triggered`` (or a later real match is swallowed), and must not
    enqueue worker jobs. Sibling/env destinations must stay unused.
    """
    token_a, user_a = _register(client, "dual-run-miss-a@example.com")
    token_b, user_b = _register(client, "dual-run-miss-b@example.com")
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
    assert get_last_triggered(user_a, "aapl_drop") is None

    snapshot = (
        "2026-08-27",
        {"AAPL": 160.0},
        [{"symbol": "AAPL", "close": 160.0}],
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
                    missed = client.post("/api/alerts/run", headers=headers_a)

    assert missed.status_code == 200
    body = missed.json()
    assert body["triggered"] == 0
    assert body["last_data_date"] == "2026-08-27"
    assert body["message"] == "No alerts triggered on latest data."
    assert body["events"] == []
    mock_cycle.assert_not_called()
    send_log.assert_not_called()
    assert _webhook_urls(mock_post) == []
    assert _sendgrid_recipients(mock_post) == []
    assert pending_job_count([JOB_DELIVER, JOB_EVALUATE_SYMBOL]) == 0
    assert get_last_triggered(user_a, "aapl_drop") is None
    assert get_last_triggered(user_b, "sibling-msft") is None

    status_a = client.get("/api/alerts/status", headers=headers_a)
    status_b = client.get("/api/alerts/status", headers=headers_b)
    assert status_a.status_code == 200
    assert status_b.status_code == 200
    assert status_a.json()["latest_deliveries"] == []
    assert status_b.json()["latest_deliveries"] == []

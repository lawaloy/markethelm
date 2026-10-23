"""Channel readiness must use tenant mailboxes, not process-wide env, in hosted mode.

``_channel_status`` ORs ``ALERT_EMAIL_TO`` / webhook env vars only in file mode.
Hosted Settings GET used to look ready (or the staging tenant check would treat
an empty account as having secrets) whenever those process-wide vars were set.
Per-rule ``email_to`` / ``webhook_url`` is the other half: a tenant with empty
defaults must still report channel flags so the UI and staging harness see the
secret. File-mode GET must also infer ``webhook_format=discord`` from
``DISCORD_WEBHOOK_URL`` when ``ALERT_WEBHOOK_FORMAT`` is unset.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from dashboard.backend.main import app
from src.storage.database import init_database


@pytest.fixture
def multi_user_env(tmp_path, monkeypatch):
    db_path = tmp_path / "channel-status.db"
    monkeypatch.setenv("MARKETHELM_DATABASE_URL", f"sqlite:///{db_path.as_posix()}")
    monkeypatch.setenv("MARKETHELM_AUTH_SECRET", "test-secret-min-16-chars")
    monkeypatch.setenv("ALERT_EMAIL_TO", "global-shared@example.com")
    monkeypatch.setenv("ALERT_WEBHOOK_URL", "https://hooks.example/global")
    monkeypatch.setenv("DISCORD_WEBHOOK_URL", "https://discord.com/api/webhooks/global/token")

    init_database()


@pytest.fixture
def client():
    return TestClient(app)


def _register(client, email: str) -> str:
    response = client.post(
        "/api/auth/register",
        json={"email": email, "password": "password123"},
    )
    assert response.status_code == 200
    return response.json()["access_token"]


def _rule(
    *,
    alert_id: str,
    symbol: str,
    email_to: str | None = None,
    webhook_url: str | None = None,
) -> dict:
    notifications = ["log"]
    if email_to:
        notifications.append("email")
    if webhook_url:
        notifications.append("webhook")
    alert = {
        "id": alert_id,
        "name": alert_id,
        "enabled": True,
        "condition": {
            "type": "price_threshold",
            "symbol": symbol,
            "operator": "less_than",
            "value": 100,
        },
        "notifications": notifications,
    }
    if email_to is not None:
        alert["email_to"] = email_to
    if webhook_url is not None:
        alert["webhook_url"] = webhook_url
    return alert


def test_hosted_rule_email_marks_recipients_ready_without_touching_sibling(
    client, multi_user_env
) -> None:
    """defaults.email_to is empty; only tenant A's rule mailbox must flip the flag."""
    token_a = _register(client, "rule-email-a@example.com")
    token_b = _register(client, "rule-email-b@example.com")
    headers_a = {"Authorization": f"Bearer {token_a}"}
    headers_b = {"Authorization": f"Bearer {token_b}"}

    saved = client.put(
        "/api/alerts/config",
        headers=headers_a,
        json={
            "defaults": {},
            "alerts": [
                _rule(
                    alert_id="rule-mailbox",
                    symbol="AAPL",
                    email_to="tenant-rule@example.com",
                )
            ],
        },
    )
    assert saved.status_code == 200
    assert saved.json()["channels"]["email_recipients"] is True
    assert saved.json()["config"]["defaults"].get("email_to") in (None, "")
    assert saved.json()["config"]["alerts"][0]["email_to"] == "tenant-rule@example.com"

    got_a = client.get("/api/alerts/config", headers=headers_a)
    got_b = client.get("/api/alerts/config", headers=headers_b)

    assert got_a.status_code == 200
    assert got_a.json()["channels"]["email_recipients"] is True
    assert got_a.json()["channels"]["webhook_url"] is False
    assert "global-shared@example.com" not in json.dumps(got_a.json())
    assert "hooks.example/global" not in json.dumps(got_a.json())

    assert got_b.status_code == 200
    assert got_b.json()["exists"] is False
    assert got_b.json()["channels"]["email_recipients"] is False
    assert got_b.json()["channels"]["webhook_url"] is False
    assert got_b.json()["config"]["alerts"] == []
    assert "tenant-rule@example.com" not in json.dumps(got_b.json())
    assert "global-shared@example.com" not in json.dumps(got_b.json())


def test_hosted_rule_webhook_marks_channel_ready_without_touching_sibling(
    client, multi_user_env
) -> None:
    """defaults.webhook_url is empty; only tenant A's rule URL must flip the flag."""
    token_a = _register(client, "rule-hook-a@example.com")
    token_b = _register(client, "rule-hook-b@example.com")
    headers_a = {"Authorization": f"Bearer {token_a}"}
    headers_b = {"Authorization": f"Bearer {token_b}"}
    rule_url = "https://hooks.example/tenant-rule"

    saved = client.put(
        "/api/alerts/config",
        headers=headers_a,
        json={
            "defaults": {},
            "alerts": [
                _rule(
                    alert_id="rule-webhook",
                    symbol="AAPL",
                    webhook_url=rule_url,
                )
            ],
        },
    )
    assert saved.status_code == 200
    assert saved.json()["channels"]["webhook_url"] is True
    assert saved.json()["channels"]["email_recipients"] is False
    assert saved.json()["config"]["defaults"].get("webhook_url") in (None, "")
    assert "webhook_url" not in saved.json()["config"]["alerts"][0]
    assert "tenant-rule" not in json.dumps(saved.json())

    got_a = client.get("/api/alerts/config", headers=headers_a)
    got_b = client.get("/api/alerts/config", headers=headers_b)

    assert got_a.status_code == 200
    assert got_a.json()["channels"]["webhook_url"] is True
    assert got_a.json()["channels"]["email_recipients"] is False
    assert "tenant-rule" not in json.dumps(got_a.json())
    assert "hooks.example/global" not in json.dumps(got_a.json())
    assert "global/token" not in json.dumps(got_a.json())

    assert got_b.status_code == 200
    assert got_b.json()["exists"] is False
    assert got_b.json()["channels"]["webhook_url"] is False
    assert got_b.json()["channels"]["email_recipients"] is False
    assert got_b.json()["config"]["alerts"] == []
    assert "tenant-rule" not in json.dumps(got_b.json())
    assert "hooks.example/global" not in json.dumps(got_b.json())


def test_file_mode_env_mailbox_marks_recipients_ready(client, tmp_path: Path, monkeypatch) -> None:
    """File mode still treats process-wide ALERT_EMAIL_TO as this install's mailbox."""
    # Exercise the explicitly injected process environment without loading any
    # repository- or user-level dotenv files from the host running the test.
    monkeypatch.setattr("dashboard.backend.api.alerts._load_env", lambda: None)
    user_dir = tmp_path / "user-config"
    user_dir.mkdir()
    monkeypatch.setattr("src.alerts.alert_paths.user_config_dir", lambda: user_dir)
    monkeypatch.delenv("MARKETHELM_DATABASE_URL", raising=False)
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.delenv("ALERT_WEBHOOK_URL", raising=False)
    monkeypatch.delenv("DISCORD_WEBHOOK_URL", raising=False)
    monkeypatch.delenv("ALERT_WEBHOOK_FORMAT", raising=False)
    config_dir = tmp_path / "markethelm"
    config_dir.mkdir()
    monkeypatch.setenv("MARKETHELM_ALERTS_CONFIG", str(config_dir / "alerts.json"))
    monkeypatch.setenv("ALERT_EMAIL_TO", "ops@example.com")

    response = client.get("/api/alerts/config")
    assert response.status_code == 200
    assert response.json()["channels"]["email_recipients"] is True
    assert response.json()["channels"]["webhook_url"] is False

    monkeypatch.delenv("ALERT_EMAIL_TO", raising=False)
    empty = client.get("/api/alerts/config")
    assert empty.status_code == 200
    assert empty.json()["channels"]["email_recipients"] is False


def test_file_mode_env_webhook_marks_channel_ready(client, tmp_path: Path, monkeypatch) -> None:
    """File mode still treats DISCORD_WEBHOOK_URL as this install's webhook secret."""
    # Exercise the explicitly injected process environment without loading any
    # repository- or user-level dotenv files from the host running the test.
    monkeypatch.setattr("dashboard.backend.api.alerts._load_env", lambda: None)
    user_dir = tmp_path / "user-config"
    user_dir.mkdir()
    monkeypatch.setattr("src.alerts.alert_paths.user_config_dir", lambda: user_dir)
    monkeypatch.delenv("MARKETHELM_DATABASE_URL", raising=False)
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.delenv("ALERT_EMAIL_TO", raising=False)
    monkeypatch.delenv("ALERT_WEBHOOK_URL", raising=False)
    monkeypatch.delenv("ALERT_WEBHOOK_FORMAT", raising=False)
    config_dir = tmp_path / "markethelm"
    config_dir.mkdir()
    monkeypatch.setenv("MARKETHELM_ALERTS_CONFIG", str(config_dir / "alerts.json"))
    monkeypatch.setenv("DISCORD_WEBHOOK_URL", "https://discord.com/api/webhooks/local/token")

    response = client.get("/api/alerts/config")
    assert response.status_code == 200
    assert response.json()["channels"]["webhook_url"] is True
    assert "local/token" not in json.dumps(response.json())
    assert response.json()["config"]["defaults"].get("webhook_url") in (None, "")
    assert response.json()["config"]["defaults"].get("webhook_format") == "discord"

    monkeypatch.delenv("DISCORD_WEBHOOK_URL", raising=False)
    empty = client.get("/api/alerts/config")
    assert empty.status_code == 200
    assert empty.json()["channels"]["webhook_url"] is False
    assert empty.json()["config"]["defaults"].get("webhook_format") in (None, "")

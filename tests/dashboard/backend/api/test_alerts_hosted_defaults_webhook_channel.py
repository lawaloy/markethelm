"""Hosted defaults-only webhook must report channel ready after secrets are stripped.

``_has_webhook_secret`` checks ``defaults.webhook_url`` before per-rule URLs.
Public GET/PUT strip that URL, and an onboarding save can have empty ``alerts``,
so Settings and the staging tenant-isolation harness see only
``channels.webhook_url``. Process-wide webhook env must not flip a sibling.
"""

from __future__ import annotations

import json
from urllib.parse import urlparse

import pytest
from fastapi.testclient import TestClient

from dashboard.backend.main import app
from src.storage.database import init_database
from src.storage.user_alerts import load_user_alerts_config


@pytest.fixture
def multi_user_env(tmp_path, monkeypatch):
    db_path = tmp_path / "defaults-webhook-channel.db"
    monkeypatch.setenv("MARKETHELM_DATABASE_URL", f"sqlite:///{db_path.as_posix()}")
    monkeypatch.setenv("MARKETHELM_AUTH_SECRET", "test-secret-min-16-chars")
    monkeypatch.setenv("ALERT_WEBHOOK_URL", "https://hooks.example/global")
    monkeypatch.setenv("DISCORD_WEBHOOK_URL", "https://discord.com/api/webhooks/global/token")

    init_database()


@pytest.fixture
def client():
    return TestClient(app)


def _register(client, email: str) -> tuple[str, str]:
    response = client.post(
        "/api/auth/register",
        json={"email": email, "password": "password123"},
    )
    assert response.status_code == 200
    body = response.json()
    return body["access_token"], body["user"]["id"]


def test_hosted_defaults_webhook_marks_channel_ready_with_empty_alerts(
    client, multi_user_env
) -> None:
    """Empty alerts + stripped defaults must still flip the channel flag."""

    token_a, user_a = _register(client, "defaults-hook-a@example.com")
    token_b, _user_b = _register(client, "defaults-hook-b@example.com")
    headers_a = {"Authorization": f"Bearer {token_a}"}
    headers_b = {"Authorization": f"Bearer {token_b}"}
    secret = "https://hooks.example/tenant-defaults"

    saved = client.put(
        "/api/alerts/config",
        headers=headers_a,
        json={"defaults": {"webhook_url": secret}, "alerts": []},
    )
    assert saved.status_code == 200
    assert saved.json()["channels"]["webhook_url"] is True
    assert saved.json()["channels"]["email_recipients"] is False
    assert saved.json()["config"]["alerts"] == []
    assert not str(saved.json()["config"]["defaults"].get("webhook_url") or "").strip()
    assert "tenant-defaults" not in json.dumps(saved.json())
    assert "hooks.example/global" not in json.dumps(saved.json())

    _, raw = load_user_alerts_config(user_a)
    assert raw is not None
    assert urlparse(raw["defaults"]["webhook_url"]).hostname == "hooks.example"
    assert raw["alerts"] == []

    got_a = client.get("/api/alerts/config", headers=headers_a)
    got_b = client.get("/api/alerts/config", headers=headers_b)

    assert got_a.status_code == 200
    assert got_a.json()["channels"]["webhook_url"] is True
    assert got_a.json()["config"]["alerts"] == []
    assert not str(got_a.json()["config"]["defaults"].get("webhook_url") or "").strip()
    assert "tenant-defaults" not in json.dumps(got_a.json())
    assert "hooks.example/global" not in json.dumps(got_a.json())
    assert "global/token" not in json.dumps(got_a.json())

    assert got_b.status_code == 200
    assert got_b.json()["exists"] is False
    assert got_b.json()["channels"]["webhook_url"] is False
    assert got_b.json()["channels"]["email_recipients"] is False
    assert got_b.json()["config"]["alerts"] == []
    assert "tenant-defaults" not in json.dumps(got_b.json())
    assert "hooks.example/global" not in json.dumps(got_b.json())

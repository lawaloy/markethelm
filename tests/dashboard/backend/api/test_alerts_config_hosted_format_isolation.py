"""Hosted GET /api/alerts/config must not seed process-wide webhook format."""

import pytest
from fastapi.testclient import TestClient

from dashboard.backend.main import app
from src.storage.database import init_database


@pytest.fixture
def multi_user_env(tmp_path, monkeypatch):
    db_path = tmp_path / "hosted-format.db"
    monkeypatch.setenv("MARKETHELM_DATABASE_URL", f"sqlite:///{db_path.as_posix()}")
    monkeypatch.setenv("MARKETHELM_AUTH_SECRET", "test-secret-min-16-chars")

    init_database()


@pytest.fixture
def client():
    return TestClient(app)


def _register(client, email: str = "format@example.com") -> str:
    r = client.post(
        "/api/auth/register",
        json={"email": email, "password": "password123"},
    )
    assert r.status_code == 200
    return r.json()["access_token"]


def test_hosted_get_does_not_seed_env_webhook_format(client, multi_user_env, monkeypatch):
    monkeypatch.setenv("ALERT_WEBHOOK_FORMAT", "slack")
    monkeypatch.setenv("DISCORD_WEBHOOK_URL", "https://discord.com/api/webhooks/global/token")

    token = _register(client)
    headers = {"Authorization": f"Bearer {token}"}
    assert client.post("/api/alerts/init", headers=headers).status_code == 200

    response = client.get("/api/alerts/config", headers=headers)
    assert response.status_code == 200
    defaults = response.json()["config"]["defaults"]
    assert defaults.get("webhook_format") in (None, "")


def test_hosted_get_keeps_explicit_tenant_webhook_format(client, multi_user_env, monkeypatch):
    monkeypatch.setenv("ALERT_WEBHOOK_FORMAT", "slack")

    token = _register(client, "tenant-format@example.com")
    headers = {"Authorization": f"Bearer {token}"}
    payload = {
        "defaults": {"webhook_format": "discord"},
        "alerts": [],
    }
    assert client.put("/api/alerts/config", json=payload, headers=headers).status_code == 200

    response = client.get("/api/alerts/config", headers=headers)
    assert response.status_code == 200
    assert response.json()["config"]["defaults"].get("webhook_format") == "discord"


def test_file_mode_get_still_surfaces_env_webhook_format(client, tmp_path, monkeypatch):
    # GET reloads ~/.markethelm/.env with override; a leftover Discord format
    # would beat process-wide ALERT_WEBHOOK_FORMAT=slack.
    user_dir = tmp_path / "user-config"
    user_dir.mkdir()
    monkeypatch.setattr("src.alerts.alert_paths.user_config_dir", lambda: user_dir)
    monkeypatch.delenv("MARKETHELM_DATABASE_URL", raising=False)
    config_path = tmp_path / "alerts.json"
    monkeypatch.setenv("MARKETHELM_ALERTS_CONFIG", str(config_path))
    monkeypatch.setenv("ALERT_WEBHOOK_FORMAT", "slack")
    monkeypatch.delenv("DISCORD_WEBHOOK_URL", raising=False)

    response = client.get("/api/alerts/config")
    assert response.status_code == 200
    assert response.json()["config"]["defaults"].get("webhook_format") == "slack"

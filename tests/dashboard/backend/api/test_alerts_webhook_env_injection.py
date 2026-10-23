"""File-mode PUT /config must reject webhook secrets that would poison .env."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from dashboard.backend.main import app


@pytest.fixture
def alerts_config_dir(tmp_path: Path, monkeypatch):
    config_dir = tmp_path / "markethelm"
    config_dir.mkdir()
    monkeypatch.setenv("MARKETHELM_ALERTS_CONFIG", str(config_dir / "alerts.json"))
    monkeypatch.delenv("MARKETHELM_DATABASE_URL", raising=False)
    return config_dir


@pytest.fixture
def client():
    return TestClient(app)


def _price_alert_payload(*, webhook_url: str, webhook_format: str = "discord") -> dict:
    return {
        "defaults": {
            "webhook_url": webhook_url,
            "webhook_format": webhook_format,
        },
        "alerts": [
            {
                "id": "aapl_drop",
                "enabled": True,
                "condition": {
                    "type": "price_threshold",
                    "symbol": "AAPL",
                    "operator": "less_than",
                    "value": 150,
                },
                "notifications": ["webhook"],
                "cooldown_minutes": 60,
            }
        ],
    }


def test_put_config_rejects_crlf_webhook_url(client, alerts_config_dir, tmp_path, monkeypatch):
    user_config_dir = tmp_path / "user-config"
    user_config_dir.mkdir()
    env_file = user_config_dir / ".env"
    env_file.write_text("SHARED_KEEP=1\n", encoding="utf-8")
    monkeypatch.setattr("src.alerts.alert_paths.user_config_dir", lambda: user_config_dir)

    poisoned = (
        "https://discord.com/api/webhooks/secret/token\n" "ALERT_EMAIL_TO=attacker@example.com"
    )
    response = client.put(
        "/api/alerts/config",
        json=_price_alert_payload(webhook_url=poisoned),
    )
    assert response.status_code == 400
    assert "control characters" in response.json()["detail"].lower()
    assert env_file.read_text(encoding="utf-8") == "SHARED_KEEP=1\n"
    assert not (alerts_config_dir / "alerts.json").exists()


def test_put_config_rejects_crlf_webhook_format(client, alerts_config_dir, tmp_path, monkeypatch):
    user_config_dir = tmp_path / "user-config"
    user_config_dir.mkdir()
    env_file = user_config_dir / ".env"
    env_file.write_text("SHARED_KEEP=1\n", encoding="utf-8")
    monkeypatch.setattr("src.alerts.alert_paths.user_config_dir", lambda: user_config_dir)

    response = client.put(
        "/api/alerts/config",
        json=_price_alert_payload(
            webhook_url="https://discord.com/api/webhooks/ok/token",
            webhook_format="discord\nALERT_EMAIL_TO=attacker@example.com",
        ),
    )
    assert response.status_code == 400
    assert "control characters" in response.json()["detail"].lower()
    assert env_file.read_text(encoding="utf-8") == "SHARED_KEEP=1\n"


def test_put_config_still_persists_safe_webhook(client, alerts_config_dir, tmp_path, monkeypatch):
    user_config_dir = tmp_path / "user-config"
    monkeypatch.setattr("src.alerts.alert_paths.user_config_dir", lambda: user_config_dir)
    monkeypatch.delenv("DISCORD_WEBHOOK_URL", raising=False)
    monkeypatch.delenv("ALERT_WEBHOOK_FORMAT", raising=False)

    response = client.put(
        "/api/alerts/config",
        json=_price_alert_payload(
            webhook_url=" https://discord.com/api/webhooks/secret/token ",
            webhook_format="DISCORD",
        ),
    )
    assert response.status_code == 200
    env_text = (user_config_dir / ".env").read_text(encoding="utf-8")
    assert "DISCORD_WEBHOOK_URL=https://discord.com/api/webhooks/secret/token" in env_text
    assert "ALERT_WEBHOOK_FORMAT=discord" in env_text
    assert "\nALERT_EMAIL_TO=" not in env_text

"""File-mode GET infers discord when env format is not json/slack/discord.

GET /api/alerts/config copies ``ALERT_WEBHOOK_FORMAT`` into defaults only when
it is json/slack/discord. An unsupported leftover (e.g. ``teams``) plus
``DISCORD_WEBHOOK_URL`` must still surface ``webhook_format=discord`` so
Settings does not persist an unknown format. ``from_alert`` does not apply
that whitelist: it still uses the env string, so delivery stays raw JSON
until the operator saves.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from dashboard.backend.main import app
from src.alerts.notifiers.webhook_notifier import WebhookNotifier

DISCORD_URL = "https://discord.com/api/webhooks/local/token"


@pytest.fixture
def file_mode(tmp_path: Path, monkeypatch):
    monkeypatch.delenv("MARKETHELM_DATABASE_URL", raising=False)
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.delenv("ALERT_WEBHOOK_URL", raising=False)
    monkeypatch.delenv("ALERT_EMAIL_TO", raising=False)
    user_dir = tmp_path / "markethelm"
    user_dir.mkdir()
    monkeypatch.setattr("src.alerts.alert_paths.user_config_dir", lambda: user_dir)
    # GET reloads user dotenv; keep process env from being overwritten by host files.
    monkeypatch.setattr("dashboard.backend.api.alerts._load_env", lambda: None)
    monkeypatch.setenv("MARKETHELM_ALERTS_CONFIG", str(user_dir / "alerts.json"))
    monkeypatch.setenv("ALERT_WEBHOOK_FORMAT", " Teams ")
    monkeypatch.setenv("DISCORD_WEBHOOK_URL", DISCORD_URL)
    return user_dir


@pytest.fixture
def client():
    return TestClient(app)


def test_file_get_unknown_format_infers_discord_from_url(client, file_mode: Path):
    response = client.get("/api/alerts/config")
    assert response.status_code == 200
    defaults = response.json()["config"]["defaults"]
    assert defaults.get("webhook_format") == "discord"
    assert defaults.get("webhook_format") != "teams"
    assert defaults.get("webhook_url") in (None, "")
    assert "local/token" not in response.text

    event = {
        "alert_id": "a1",
        "alert_name": "Drop",
        "symbols": ["AAPL"],
        "condition_type": "price_threshold",
        "timestamp": "2026-05-21T12:00:00",
    }
    notifier = WebhookNotifier.from_alert({"id": "a1", "notifications": ["webhook"]})
    assert notifier is not None
    assert notifier._url == DISCORD_URL
    assert notifier._payload_format == "teams"
    payload = notifier.build_payload(event)
    assert payload == event
    assert "content" not in payload
    assert "blocks" not in payload

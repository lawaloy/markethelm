"""File-mode GET must keep explicit JSON format over Discord URL inference.

GET /api/alerts/config infers ``webhook_format=discord`` from
``DISCORD_WEBHOOK_URL`` only when ``ALERT_WEBHOOK_FORMAT`` is unset. Operators
who set ``ALERT_WEBHOOK_FORMAT=json`` for a custom HTTPS webhook while a leftover
Discord URL remains in env would otherwise see Settings rewrite the format to
discord; the next PUT persists that and ``from_alert`` POSTs Discord ``content``
instead of the raw JSON payload. Existing GET tests cover slack-from-env and
discord-from-URL-when-unset, but not json winning over a Discord URL.
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
    monkeypatch.setenv("ALERT_WEBHOOK_FORMAT", " JSON ")
    monkeypatch.setenv("DISCORD_WEBHOOK_URL", DISCORD_URL)
    return user_dir


@pytest.fixture
def client():
    return TestClient(app)


def test_file_get_explicit_json_format_wins_over_discord_url(client, file_mode: Path):
    response = client.get("/api/alerts/config")
    assert response.status_code == 200
    defaults = response.json()["config"]["defaults"]
    assert defaults.get("webhook_format") == "json"
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
    assert notifier._payload_format == "json"
    payload = notifier.build_payload(event)
    assert payload == event
    assert "content" not in payload
    assert "blocks" not in payload

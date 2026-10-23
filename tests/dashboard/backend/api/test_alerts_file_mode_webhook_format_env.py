"""File-mode PUT must strip/lower webhook_format into ALERT_WEBHOOK_FORMAT.

``_persist_webhook_secret`` writes Settings ``defaults.webhook_format`` into
``~/.markethelm/.env`` then ``_load_env`` reloads it for CLI ``from_alert``.
Existing PUT tests persist uppercase ``DISCORD``; ``_normalize_config`` covers
padded Discord in memory only. A regression that skipped strip/lower on persist
would leave ``ALERT_WEBHOOK_FORMAT= Slack `` (or ``Slack``) in the operator
dotenv, while JSON already looks canonical.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from dashboard.backend.main import app
from src.alerts.notifiers.webhook_notifier import WebhookNotifier


@pytest.fixture
def file_mode(tmp_path: Path, monkeypatch):
    monkeypatch.delenv("MARKETHELM_DATABASE_URL", raising=False)
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.delenv("ALERT_WEBHOOK_FORMAT", raising=False)
    monkeypatch.delenv("DISCORD_WEBHOOK_URL", raising=False)
    monkeypatch.delenv("ALERT_WEBHOOK_URL", raising=False)
    user_dir = tmp_path / "markethelm"
    user_dir.mkdir()
    monkeypatch.setattr("src.alerts.alert_paths.user_config_dir", lambda: user_dir)
    monkeypatch.setenv("MARKETHELM_ALERTS_CONFIG", str(user_dir / "alerts.json"))
    return user_dir


@pytest.fixture
def client():
    return TestClient(app)


def test_file_put_strips_padded_slack_format_into_env(client, file_mode: Path):
    response = client.put(
        "/api/alerts/config",
        json={"defaults": {"webhook_format": " Slack "}, "alerts": []},
    )
    assert response.status_code == 200
    assert response.json()["config"]["defaults"]["webhook_format"] == "slack"

    env_lines = (file_mode / ".env").read_text(encoding="utf-8").splitlines()
    assert "ALERT_WEBHOOK_FORMAT=slack" in env_lines
    assert "ALERT_WEBHOOK_FORMAT= Slack " not in env_lines
    assert "ALERT_WEBHOOK_FORMAT=Slack" not in env_lines

    # PUT reloads user dotenv; CLI from_alert env fallback must see canonical slack.
    assert os.environ.get("ALERT_WEBHOOK_FORMAT") == "slack"
    notifier = WebhookNotifier.from_alert(
        {
            "id": "a1",
            "webhook_url": "https://[::ffff:8.8.8.8]/hooks/T/B/X",
            "notifications": ["webhook"],
        }
    )
    assert notifier is not None
    assert notifier._payload_format == "slack"

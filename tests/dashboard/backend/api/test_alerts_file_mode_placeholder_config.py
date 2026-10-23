"""File-mode Settings GET must strip bundled placeholder mailboxes and webhooks.

Hosted GET already locks this in ``test_alerts_hosted_placeholder_rule_email``
and ``test_alerts_hosted_placeholder_rule_webhook``. File-mode GET polishes
``alerts.json`` then ORs remaining ``email_to`` / webhook secrets into channel
readiness. A hand-copied example rule used to be untested at the HTTP layer:
skipping that strip would look email- or webhook-ready and leak
``you@example.com``. Public JSON already redacts webhook URLs, so the webhook
lock is the readiness flag. Mixed junk items and poison defaults are locked
separately.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from dashboard.backend.main import app


@pytest.fixture
def file_mode(tmp_path: Path, monkeypatch):
    monkeypatch.delenv("MARKETHELM_DATABASE_URL", raising=False)
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.delenv("ALERT_EMAIL_TO", raising=False)
    monkeypatch.delenv("ALERT_WEBHOOK_URL", raising=False)
    monkeypatch.delenv("DISCORD_WEBHOOK_URL", raising=False)
    monkeypatch.delenv("ALERT_WEBHOOK_FORMAT", raising=False)
    # Do not load host ~/.markethelm/.env into channel status.
    monkeypatch.setattr("dashboard.backend.api.alerts._load_env", lambda: None)
    user_dir = tmp_path / "markethelm"
    user_dir.mkdir()
    monkeypatch.setattr("src.alerts.alert_paths.user_config_dir", lambda: user_dir)
    config_path = user_dir / "alerts.json"
    monkeypatch.setenv("MARKETHELM_ALERTS_CONFIG", str(config_path))
    return config_path


@pytest.fixture
def client():
    return TestClient(app)


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


def test_file_get_config_strips_placeholder_rule_email_without_faking_recipients(
    client, file_mode: Path
) -> None:
    """Stored you@example.com must not flip email_recipients or leak in JSON."""
    file_mode.write_text(
        json.dumps(
            {
                "defaults": {},
                "alerts": [
                    _rule(
                        alert_id="example-aapl",
                        symbol="AAPL",
                        email_to="you@example.com",
                    )
                ],
            }
        ),
        encoding="utf-8",
    )

    response = client.get("/api/alerts/config")
    assert response.status_code == 200
    body = response.json()
    assert body["exists"] is True
    assert [alert["id"] for alert in body["config"]["alerts"]] == ["example-aapl"]
    assert not str(body["config"]["alerts"][0].get("email_to") or "").strip()
    assert body["config"]["defaults"].get("email_to") in (None, "")
    assert body["channels"]["email_recipients"] is False
    assert body["channels"]["webhook_url"] is False
    assert "you@example.com" not in json.dumps(body)


def test_file_get_config_strips_placeholder_rule_webhook_without_faking_channel(
    client, file_mode: Path
) -> None:
    """Stored hooks.example.com must not flip webhook_url readiness."""
    placeholder = "https://hooks.example.com/services/T00/B00/xxx"
    file_mode.write_text(
        json.dumps(
            {
                "defaults": {},
                "alerts": [
                    _rule(
                        alert_id="example-aapl",
                        symbol="AAPL",
                        webhook_url=placeholder,
                    )
                ],
            }
        ),
        encoding="utf-8",
    )

    response = client.get("/api/alerts/config")
    assert response.status_code == 200
    body = response.json()
    assert body["exists"] is True
    assert [alert["id"] for alert in body["config"]["alerts"]] == ["example-aapl"]
    assert "webhook_url" not in body["config"]["alerts"][0]
    assert body["config"]["defaults"].get("webhook_url") in (None, "")
    assert body["channels"]["webhook_url"] is False
    assert body["channels"]["email_recipients"] is False
    assert "hooks.example.com" not in json.dumps(body)
    assert "T00/B00/xxx" not in json.dumps(body)

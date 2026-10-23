"""File-mode Settings GET must skip mixed alert items and poison defaults.

Hosted GET already locks this in ``test_alerts_hosted_mixed_alert_items`` and
``test_alerts_hosted_nondict_defaults``. File-mode GET polishes ``alerts.json``
then calls ``_channel_status``, which does ``alert.get("email_to")`` on each
row. A hand-edited list that still contains a valid rule plus junk, or a
truthy non-dict ``defaults`` (an email-shaped string), used to be untested at
the HTTP layer: dropping the polish guards would 500 Settings while status
already skips non-dicts. Non-list ``alerts`` keys are locked separately.
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


def _rule(*, alert_id: str, symbol: str, email_to: str | None = None) -> dict:
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
        "notifications": ["log", "email"] if email_to else ["log"],
    }
    if email_to is not None:
        alert["email_to"] = email_to
    return alert


def test_file_get_config_skips_mixed_alert_items_and_keeps_rule_email(
    client, file_mode: Path
) -> None:
    """A valid rule must still flip email_recipients after junk items drop."""
    file_mode.write_text(
        json.dumps(
            {
                "defaults": {},
                "alerts": [
                    _rule(
                        alert_id="keep-aapl",
                        symbol="AAPL",
                        email_to="ops-a@example.com",
                    ),
                    1,
                    "poison",
                    None,
                ],
            }
        ),
        encoding="utf-8",
    )

    response = client.get("/api/alerts/config")
    assert response.status_code == 200
    body = response.json()
    assert body["exists"] is True
    assert [alert["id"] for alert in body["config"]["alerts"]] == ["keep-aapl"]
    assert body["config"]["alerts"][0].get("email_to") == "ops-a@example.com"
    assert body["channels"]["email_recipients"] is True
    assert body["channels"]["webhook_url"] is False


def test_file_get_config_soft_fails_string_defaults_without_faking_email(
    client, file_mode: Path
) -> None:
    """Email-shaped ``defaults`` must empty to {} and not fake recipients."""
    file_mode.write_text(
        json.dumps(
            {
                "defaults": "ops@example.com",
                "alerts": [_rule(alert_id="keep-aapl", symbol="AAPL")],
            }
        ),
        encoding="utf-8",
    )

    response = client.get("/api/alerts/config")
    assert response.status_code == 200
    body = response.json()
    assert body["exists"] is True
    defaults = body["config"]["defaults"]
    assert defaults.get("email_to") in (None, "")
    assert defaults.get("webhook_url") in (None, "")
    assert defaults.get("webhook_format") in (None, "")
    assert [alert["id"] for alert in body["config"]["alerts"]] == ["keep-aapl"]
    assert body["channels"]["email_recipients"] is False
    assert body["channels"]["webhook_url"] is False
    assert "ops@example.com" not in json.dumps(body)

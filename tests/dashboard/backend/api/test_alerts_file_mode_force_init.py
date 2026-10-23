"""File-mode POST /api/alerts/init?force=true must reset to empty, not bundled samples.

Hosted force-init is locked in ``test_multi_user_alerts_api``. File mode uses
``init_minimal_user_alerts_config`` (empty alerts) instead of copying
``alerts.example.json``. A later swap to CLI ``init_user_alerts_config`` would
replace a user's rules with bundled sample ids and still 200.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from dashboard.backend.main import app


@pytest.fixture
def file_mode(tmp_path: Path, monkeypatch):
    monkeypatch.delenv("MARKETHELM_DATABASE_URL", raising=False)
    monkeypatch.delenv("DATABASE_URL", raising=False)
    user_dir = tmp_path / "markethelm"
    user_dir.mkdir()
    monkeypatch.setattr("src.alerts.alert_paths.user_config_dir", lambda: user_dir)
    config_path = user_dir / "alerts.json"
    monkeypatch.setenv("MARKETHELM_ALERTS_CONFIG", str(config_path))
    return config_path


@pytest.fixture
def client():
    return TestClient(app)


def _custom_rule() -> dict:
    return {
        "id": "keep-me",
        "name": "Custom watch",
        "enabled": True,
        "condition": {
            "type": "price_threshold",
            "symbol": "AAPL",
            "operator": "less_than",
            "value": 100,
        },
        "notifications": ["log"],
    }


def test_file_mode_init_force_resets_to_empty_not_bundled_example(client, file_mode: Path) -> None:
    """Conflict must keep the custom rule; force must wipe it without sample ids."""
    saved = client.put(
        "/api/alerts/config",
        json={"defaults": {"email_to": "ops@example.com"}, "alerts": [_custom_rule()]},
    )
    assert saved.status_code == 200
    assert saved.json()["config"]["alerts"][0]["id"] == "keep-me"

    conflict = client.post("/api/alerts/init")
    assert conflict.status_code == 409
    assert "force=true" in conflict.json()["detail"]

    still_there = client.get("/api/alerts/config")
    assert still_there.status_code == 200
    assert [row["id"] for row in still_there.json()["config"]["alerts"]] == ["keep-me"]
    assert still_there.json()["config"]["defaults"]["email_to"] == "ops@example.com"

    forced = client.post("/api/alerts/init?force=true")
    assert forced.status_code == 200

    reset = client.get("/api/alerts/config")
    assert reset.status_code == 200
    assert reset.json()["exists"] is True
    reset_ids = [row["id"] for row in reset.json()["config"]["alerts"]]
    # Empty onboarding config — not alerts.example.json sample ids.
    assert reset_ids == []
    assert file_mode.exists()

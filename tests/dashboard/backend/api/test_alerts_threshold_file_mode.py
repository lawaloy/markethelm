"""File-mode PUT /api/alerts/config must reject missing/null price thresholds.

Hosted Settings already locks this in ``test_alerts_threshold_required``.
File-mode previously skipped ``validate_watches_config``, so a null or omitted
threshold could persist and never evaluate. A later skip of that coerce would
write the incomplete rule to alerts.json instead of HTTP 400.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from dashboard.backend.main import app


@pytest.fixture
def client():
    return TestClient(app)


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


def _payload(*, value=150, include_value: bool = True) -> dict:
    condition = {
        "type": "price_threshold",
        "symbol": "AAPL",
        "operator": "less_than",
    }
    if include_value:
        condition["value"] = value
    return {
        "defaults": {},
        "alerts": [
            {
                "id": "aapl-low",
                "name": "AAPL low",
                "enabled": True,
                "cooldown_minutes": 15,
                "condition": condition,
                "notifications": ["log"],
            }
        ],
    }


def test_file_put_rejects_null_price_threshold(client, file_mode: Path) -> None:
    body = json.dumps(_payload(value=0)).replace('"value": 0', '"value": null')

    response = client.put(
        "/api/alerts/config",
        content=body,
        headers={"Content-Type": "application/json"},
    )

    assert response.status_code == 400
    assert "price threshold" in response.json()["detail"]
    assert not file_mode.exists()


def test_file_put_rejects_omitted_price_threshold(client, file_mode: Path) -> None:
    response = client.put("/api/alerts/config", json=_payload(include_value=False))

    assert response.status_code == 400
    assert "price threshold" in response.json()["detail"]
    assert not file_mode.exists()


def test_file_put_null_threshold_preserves_existing_config(client, file_mode: Path) -> None:
    ok = client.put("/api/alerts/config", json=_payload(value=150))
    assert ok.status_code == 200
    before = json.loads(file_mode.read_text(encoding="utf-8"))
    assert before["alerts"][0]["condition"]["value"] == 150

    body = json.dumps(_payload(value=0)).replace('"value": 0', '"value": null')
    bad = client.put(
        "/api/alerts/config",
        content=body,
        headers={"Content-Type": "application/json"},
    )
    assert bad.status_code == 400
    assert "price threshold" in bad.json()["detail"]

    after = json.loads(file_mode.read_text(encoding="utf-8"))
    assert after["alerts"][0]["condition"]["value"] == 150

"""File-mode PUT /api/alerts/config must reject invalid cooldown_minutes."""

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
    """Isolate file-mode alerts config; ensure hosted DB mode is off."""
    config_path = tmp_path / "alerts.json"
    monkeypatch.delenv("MARKETHELM_DATABASE_URL", raising=False)
    monkeypatch.setenv("MARKETHELM_ALERTS_CONFIG", str(config_path))
    return config_path


def _payload(cooldown_minutes):
    return {
        "defaults": {},
        "alerts": [
            {
                "id": "aapl-low",
                "name": "AAPL low",
                "enabled": True,
                "cooldown_minutes": cooldown_minutes,
                "condition": {
                    "type": "price_threshold",
                    "symbol": "AAPL",
                    "operator": "less_than",
                    "value": 150,
                },
                "notifications": ["log"],
            }
        ],
    }


def test_file_put_rejects_negative_cooldown(client, file_mode):
    response = client.put("/api/alerts/config", json=_payload(-5))

    assert response.status_code == 400
    assert "cooldown_minutes" in response.json()["detail"]
    assert not file_mode.exists()


def test_file_put_rejects_huge_cooldown(client, file_mode):
    response = client.put("/api/alerts/config", json=_payload(1e15))

    assert response.status_code == 400
    assert "cooldown_minutes" in response.json()["detail"]
    assert not file_mode.exists()


def test_file_put_negative_preserves_existing_config(client, file_mode):
    ok = client.put("/api/alerts/config", json=_payload(15))
    assert ok.status_code == 200
    assert file_mode.exists()
    before = json.loads(file_mode.read_text(encoding="utf-8"))
    assert before["alerts"][0]["cooldown_minutes"] == 15

    bad = client.put("/api/alerts/config", json=_payload(-1))
    assert bad.status_code == 400

    after = json.loads(file_mode.read_text(encoding="utf-8"))
    assert after["alerts"][0]["cooldown_minutes"] == 15


def test_file_put_accepts_zero_cooldown(client, file_mode):
    response = client.put("/api/alerts/config", json=_payload(0))

    assert response.status_code == 200
    assert response.json()["config"]["alerts"][0]["cooldown_minutes"] == 0

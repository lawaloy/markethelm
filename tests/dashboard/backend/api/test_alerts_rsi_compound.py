"""PUT /api/alerts/config must reject invalid RSI periods and compound shapes.

#634 added rsi_threshold / compound rules. File mode and hosted both coerce
period bounds and compound shape before persisting so a later skip of that
gate cannot write a watch that never evaluates or that overflows the leaf cap.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from dashboard.backend.main import app
from src.alerts.alert_rules import MAX_COMPOUND_LEAVES
from src.storage.database import init_database


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


@pytest.fixture
def multi_user_env(tmp_path, monkeypatch):
    db_path = tmp_path / "rsi-compound-api.db"
    monkeypatch.setenv("MARKETHELM_DATABASE_URL", f"sqlite:///{db_path.as_posix()}")
    monkeypatch.setenv("MARKETHELM_AUTH_SECRET", "test-secret-min-16-chars")

    init_database()


def _register(client, email: str) -> str:
    r = client.post(
        "/api/auth/register",
        json={"email": email, "password": "password123"},
    )
    assert r.status_code == 200
    return r.json()["access_token"]


def _rsi_payload(*, period=14, value=30):
    condition = {
        "type": "rsi_threshold",
        "symbol": "AAPL",
        "operator": "less_than",
        "value": value,
    }
    if period is not None:
        condition["period"] = period
    return {
        "defaults": {},
        "alerts": [
            {
                "id": "aapl-rsi",
                "name": "AAPL RSI",
                "enabled": True,
                "cooldown_minutes": 15,
                "condition": condition,
                "notifications": ["log"],
            }
        ],
    }


def _price_leaf(value=150):
    return {
        "type": "price_threshold",
        "symbol": "AAPL",
        "operator": "less_than",
        "value": value,
    }


def _rsi_leaf(value=30):
    return {
        "type": "rsi_threshold",
        "symbol": "AAPL",
        "period": 14,
        "operator": "less_than",
        "value": value,
    }


def _compound_payload(*, op="and", leaves=None):
    return {
        "defaults": {},
        "alerts": [
            {
                "id": "aapl-combo",
                "name": "AAPL combo",
                "enabled": True,
                "cooldown_minutes": 15,
                "condition": {
                    "type": "compound",
                    "op": op,
                    "conditions": leaves if leaves is not None else [_price_leaf(), _rsi_leaf()],
                },
                "notifications": ["log"],
            }
        ],
    }


@pytest.mark.parametrize("period", [1, 51, "nope"])
def test_file_put_rejects_invalid_rsi_period(client, file_mode: Path, period):
    response = client.put("/api/alerts/config", json=_rsi_payload(period=period))
    assert response.status_code == 400
    assert "invalid RSI period" in response.json()["detail"]
    assert not file_mode.exists()


def test_file_put_persists_valid_rsi_and_rejects_later_poison(client, file_mode: Path):
    ok = client.put("/api/alerts/config", json=_rsi_payload(period=14))
    assert ok.status_code == 200
    assert ok.json()["config"]["alerts"][0]["condition"]["period"] == 14

    bad = client.put("/api/alerts/config", json=_rsi_payload(period=1))
    assert bad.status_code == 400
    assert "invalid RSI period" in bad.json()["detail"]

    saved = client.get("/api/alerts/config")
    assert saved.status_code == 200
    assert saved.json()["config"]["alerts"][0]["condition"]["period"] == 14


def test_file_put_rejects_xor_and_oversized_compound(client, file_mode: Path):
    xor = client.put("/api/alerts/config", json=_compound_payload(op="xor"))
    assert xor.status_code == 400
    assert "and" in xor.json()["detail"]

    oversized = client.put(
        "/api/alerts/config",
        json=_compound_payload(
            leaves=[_price_leaf(value=100 + i) for i in range(MAX_COMPOUND_LEAVES + 1)]
        ),
    )
    assert oversized.status_code == 400
    assert "exceeds 5 conditions" in oversized.json()["detail"]
    assert not file_mode.exists()


def test_file_put_persists_valid_compound(client, file_mode: Path):
    response = client.put("/api/alerts/config", json=_compound_payload())
    assert response.status_code == 200
    condition = response.json()["config"]["alerts"][0]["condition"]
    assert condition["type"] == "compound"
    assert condition["op"] == "and"
    assert len(condition["conditions"]) == 2


def test_hosted_put_rejects_invalid_rsi_period(client, multi_user_env):
    token = _register(client, "rsi-period@example.com")
    headers = {"Authorization": f"Bearer {token}"}

    response = client.put("/api/alerts/config", json=_rsi_payload(period=51), headers=headers)
    assert response.status_code == 400
    assert "invalid RSI period" in response.json()["detail"]

    saved = client.get("/api/alerts/config", headers=headers)
    assert saved.status_code == 200
    assert saved.json()["exists"] is False


def test_hosted_put_persists_valid_rsi(client, multi_user_env):
    token = _register(client, "rsi-ok@example.com")
    headers = {"Authorization": f"Bearer {token}"}

    response = client.put("/api/alerts/config", json=_rsi_payload(period=14), headers=headers)
    assert response.status_code == 200
    assert response.json()["config"]["alerts"][0]["condition"]["type"] == "rsi_threshold"
    assert response.json()["config"]["alerts"][0]["condition"]["period"] == 14


def test_hosted_put_rejects_oversized_compound_without_replacing(client, multi_user_env):
    token = _register(client, "compound-cap@example.com")
    headers = {"Authorization": f"Bearer {token}"}

    ok = client.put("/api/alerts/config", json=_compound_payload(), headers=headers)
    assert ok.status_code == 200

    bad = client.put(
        "/api/alerts/config",
        json=_compound_payload(
            leaves=[_price_leaf(value=100 + i) for i in range(MAX_COMPOUND_LEAVES + 1)]
        ),
        headers=headers,
    )
    assert bad.status_code == 400
    assert "exceeds 5 conditions" in bad.json()["detail"]

    saved = client.get("/api/alerts/config", headers=headers)
    assert saved.status_code == 200
    assert saved.json()["config"]["alerts"][0]["id"] == "aapl-combo"
    assert len(saved.json()["config"]["alerts"][0]["condition"]["conditions"]) == 2

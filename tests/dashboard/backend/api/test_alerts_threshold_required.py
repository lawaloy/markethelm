"""PUT /api/alerts/config must reject missing/null price threshold values."""

import json

import pytest
from fastapi.testclient import TestClient

from dashboard.backend.main import app
from src.storage.database import init_database


@pytest.fixture
def client():
    return TestClient(app)


@pytest.fixture
def multi_user_env(tmp_path, monkeypatch):
    db_path = tmp_path / "threshold-required-api.db"
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


def test_hosted_put_rejects_null_price_threshold(client, multi_user_env):
    token = _register(client, "threshold-null@example.com")
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }
    body = json.dumps(_payload(value=0)).replace('"value": 0', '"value": null')

    response = client.put("/api/alerts/config", content=body, headers=headers)

    assert response.status_code == 400
    assert "price threshold" in response.json()["detail"]
    saved = client.get("/api/alerts/config", headers={"Authorization": f"Bearer {token}"})
    assert saved.status_code == 200
    assert saved.json()["exists"] is False


def test_hosted_put_rejects_omitted_price_threshold(client, multi_user_env):
    token = _register(client, "threshold-omit@example.com")
    headers = {"Authorization": f"Bearer {token}"}

    response = client.put(
        "/api/alerts/config",
        json=_payload(include_value=False),
        headers=headers,
    )

    assert response.status_code == 400
    assert "price threshold" in response.json()["detail"]


def test_hosted_put_null_threshold_preserves_existing(client, multi_user_env):
    token = _register(client, "threshold-null-keep@example.com")
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }
    ok = client.put("/api/alerts/config", json=_payload(value=150), headers=headers)
    assert ok.status_code == 200

    body = json.dumps(_payload(value=0)).replace('"value": 0', '"value": null')
    bad = client.put("/api/alerts/config", content=body, headers=headers)
    assert bad.status_code == 400

    saved = client.get("/api/alerts/config", headers={"Authorization": f"Bearer {token}"})
    assert saved.status_code == 200
    assert saved.json()["config"]["alerts"][0]["condition"]["value"] == 150

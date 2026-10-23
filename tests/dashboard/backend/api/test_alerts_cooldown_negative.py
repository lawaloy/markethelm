"""PUT /api/alerts/config must reject negative cooldown_minutes with HTTP 400."""

import pytest
from fastapi.testclient import TestClient

from dashboard.backend.main import app
from src.storage.database import init_database


@pytest.fixture
def client():
    return TestClient(app)


@pytest.fixture
def multi_user_env(tmp_path, monkeypatch):
    db_path = tmp_path / "cooldown-neg-api.db"
    monkeypatch.setenv("MARKETHELM_DATABASE_URL", f"sqlite:///{db_path.as_posix()}")
    monkeypatch.setenv("MARKETHELM_AUTH_SECRET", "test-secret-min-16-chars")

    init_database()


def _register(client, email: str = "cooldown-neg@example.com") -> str:
    r = client.post(
        "/api/auth/register",
        json={"email": email, "password": "password123"},
    )
    assert r.status_code == 200
    return r.json()["access_token"]


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


def test_hosted_put_rejects_negative_cooldown(client, multi_user_env):
    token = _register(client)
    headers = {"Authorization": f"Bearer {token}"}

    response = client.put(
        "/api/alerts/config",
        json=_payload(-5),
        headers=headers,
    )

    assert response.status_code == 400
    assert "cooldown_minutes" in response.json()["detail"]

    saved = client.get("/api/alerts/config", headers=headers)
    assert saved.status_code == 200
    assert saved.json()["exists"] is False
    assert saved.json()["config"]["alerts"] == []


def test_hosted_put_negative_cooldown_preserves_existing_config(client, multi_user_env):
    token = _register(client, "cooldown-neg-preserve@example.com")
    headers = {"Authorization": f"Bearer {token}"}

    ok = client.put(
        "/api/alerts/config",
        json=_payload(15),
        headers=headers,
    )
    assert ok.status_code == 200
    assert ok.json()["config"]["alerts"][0]["cooldown_minutes"] == 15

    bad = client.put(
        "/api/alerts/config",
        json=_payload(-1),
        headers=headers,
    )
    assert bad.status_code == 400
    assert "cooldown_minutes" in bad.json()["detail"]

    saved = client.get("/api/alerts/config", headers=headers)
    assert saved.status_code == 200
    assert saved.json()["exists"] is True
    assert saved.json()["config"]["alerts"][0]["cooldown_minutes"] == 15


def test_hosted_put_accepts_zero_cooldown(client, multi_user_env):
    """Zero remains a valid 'no cooldown' sentinel; only negatives are rejected."""
    token = _register(client, "cooldown-zero@example.com")
    headers = {"Authorization": f"Bearer {token}"}

    response = client.put(
        "/api/alerts/config",
        json=_payload(0),
        headers=headers,
    )

    assert response.status_code == 200
    assert response.json()["config"]["alerts"][0]["cooldown_minutes"] == 0

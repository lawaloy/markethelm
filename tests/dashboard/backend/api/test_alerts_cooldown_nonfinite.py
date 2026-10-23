"""PUT /api/alerts/config must reject Inf/NaN cooldown_minutes with HTTP 400."""

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
    db_path = tmp_path / "cooldown-api.db"
    monkeypatch.setenv("MARKETHELM_DATABASE_URL", f"sqlite:///{db_path.as_posix()}")
    monkeypatch.setenv("MARKETHELM_AUTH_SECRET", "test-secret-min-16-chars")

    init_database()


def _register(client, email: str = "cooldown-api@example.com") -> str:
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


@pytest.mark.parametrize(
    ("raw_token", "email"),
    [
        ("Infinity", "cooldown-inf@example.com"),
        ("-Infinity", "cooldown-ninf@example.com"),
        ("NaN", "cooldown-nan@example.com"),
    ],
)
def test_hosted_put_rejects_nonfinite_cooldown(client, multi_user_env, raw_token, email):
    token = _register(client, email)
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }
    # httpx refuses to serialize Inf/NaN; send Python-json non-finite literals
    # the way a permissive client / proxy might.
    body = json.dumps(_payload(0)).replace(
        '"cooldown_minutes": 0',
        f'"cooldown_minutes": {raw_token}',
    )

    response = client.put("/api/alerts/config", content=body, headers=headers)

    assert response.status_code == 400
    assert "cooldown_minutes" in response.json()["detail"]

    saved = client.get("/api/alerts/config", headers={"Authorization": f"Bearer {token}"})
    assert saved.status_code == 200
    assert saved.json()["exists"] is False
    assert saved.json()["config"]["alerts"] == []


def test_hosted_put_accepts_finite_cooldown(client, multi_user_env):
    token = _register(client, "cooldown-ok@example.com")
    headers = {"Authorization": f"Bearer {token}"}

    response = client.put(
        "/api/alerts/config",
        json=_payload(30),
        headers=headers,
    )

    assert response.status_code == 200
    assert response.json()["config"]["alerts"][0]["cooldown_minutes"] == 30

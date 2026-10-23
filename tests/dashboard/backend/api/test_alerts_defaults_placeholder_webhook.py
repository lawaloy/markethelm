"""Hosted GET/PUT must not report webhook ready for placeholder default URLs."""

import pytest
from fastapi.testclient import TestClient

from dashboard.backend.main import app
from src.storage.database import init_database


@pytest.fixture
def client():
    return TestClient(app)


@pytest.fixture
def multi_user_env(tmp_path, monkeypatch):
    db_path = tmp_path / "placeholder-webhook.db"
    monkeypatch.setenv("MARKETHELM_DATABASE_URL", f"sqlite:///{db_path.as_posix()}")
    monkeypatch.setenv("MARKETHELM_AUTH_SECRET", "test-secret-min-16-chars")

    init_database()


def _register(client, email: str = "placeholder@example.com") -> str:
    r = client.post(
        "/api/auth/register",
        json={"email": email, "password": "password123"},
    )
    assert r.status_code == 200
    return r.json()["access_token"]


def test_hosted_put_strips_placeholder_defaults_webhook(client, multi_user_env):
    token = _register(client)
    headers = {"Authorization": f"Bearer {token}"}

    payload = {
        "defaults": {
            "webhook_url": "https://hooks.example.com/services/T00/B00/xxx",
            "webhook_format": "slack",
        },
        "alerts": [
            {
                "id": "aapl-low",
                "enabled": True,
                "condition": {
                    "type": "price_threshold",
                    "symbol": "AAPL",
                    "operator": "less_than",
                    "value": 150,
                },
                "notifications": ["webhook"],
            }
        ],
    }
    response = client.put("/api/alerts/config", json=payload, headers=headers)
    assert response.status_code == 200
    assert response.json()["channels"]["webhook_url"] is False

    fetched = client.get("/api/alerts/config", headers=headers)
    assert fetched.status_code == 200
    # Public schema may still expose webhook_url as null; readiness must stay false.
    defaults = fetched.json()["config"].get("defaults") or {}
    assert not defaults.get("webhook_url")
    assert fetched.json()["channels"]["webhook_url"] is False

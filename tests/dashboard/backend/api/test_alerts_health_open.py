"""Hosted /api/alerts/health must stay anonymously open for quote-bootstrap probes."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from dashboard.backend.main import app
from src.storage.database import init_database


@pytest.fixture
def multi_user_env(tmp_path, monkeypatch):
    db_path = tmp_path / "alerts-health.db"
    monkeypatch.setenv("MARKETHELM_DATABASE_URL", f"sqlite:///{db_path.as_posix()}")
    monkeypatch.setenv("MARKETHELM_AUTH_SECRET", "test-secret-min-16-chars")

    init_database()


@pytest.fixture
def client():
    return TestClient(app)


def test_alerts_health_remains_open_without_auth(client, multi_user_env):
    """Picker / useSymbolPrices probes health before a session exists."""
    r = client.get("/api/alerts/health")
    assert r.status_code == 200
    assert r.json()["ok"] is True
    assert r.json()["quotes"] is True
    assert isinstance(r.json()["live_quotes_configured"], bool)


def test_alerts_health_still_ok_with_bearer(client, multi_user_env):
    reg = client.post(
        "/api/auth/register",
        json={"email": "health@example.com", "password": "password123"},
    )
    assert reg.status_code == 200
    token = reg.json()["access_token"]
    r = client.get(
        "/api/alerts/health",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.status_code == 200
    assert r.json()["ok"] is True

"""Server-side session revocation behavior."""

import pytest
from fastapi.testclient import TestClient

from dashboard.backend.main import app
from src.storage.database import init_database


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv(
        "MARKETHELM_DATABASE_URL", f"sqlite:///{(tmp_path / 'sessions.db').as_posix()}"
    )
    monkeypatch.setenv("MARKETHELM_AUTH_SECRET", "test-secret-min-16-chars")

    init_database()

    return TestClient(app)


def _register(client, email="session@example.com"):
    response = client.post("/api/auth/register", json={"email": email, "password": "password123"})
    assert response.status_code == 200
    return response.json()["access_token"], response.json()["user"]["id"]


def test_logout_revokes_existing_token(client):
    token, _user_id = _register(client)
    headers = {"Authorization": f"Bearer {token}"}
    assert client.get("/api/auth/me", headers=headers).status_code == 200
    logout = client.post("/api/auth/logout", headers=headers)
    assert logout.status_code == 200
    assert client.get("/api/auth/me", headers=headers).status_code == 401


def test_password_reset_revokes_all_existing_tokens(client, monkeypatch):
    sent = {}
    monkeypatch.setattr(
        "dashboard.backend.api.auth.send_account_email",
        lambda **kwargs: sent.update(kwargs) is None or True,
    )
    first_token, _user_id = _register(client, "reset-session@example.com")
    second = client.post(
        "/api/auth/login",
        json={"email": "reset-session@example.com", "password": "password123"},
    )
    second_token = second.json()["access_token"]
    client.post("/api/auth/password-reset/request", json={"email": "reset-session@example.com"})
    reset = client.post(
        "/api/auth/password-reset/confirm",
        json={"token": sent["token"], "password": "new-password-123"},
    )
    assert reset.status_code == 200
    for token in (first_token, second_token):
        response = client.get("/api/auth/me", headers={"Authorization": f"Bearer {token}"})
        assert response.status_code == 401
        assert response.json()["detail"] == "Session revoked."
    fresh = client.post(
        "/api/auth/login",
        json={"email": "reset-session@example.com", "password": "new-password-123"},
    )
    assert fresh.status_code == 200
    assert (
        client.get(
            "/api/auth/me",
            headers={"Authorization": f"Bearer {fresh.json()['access_token']}"},
        ).status_code
        == 200
    )

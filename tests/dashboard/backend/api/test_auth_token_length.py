"""Authenticated routes must reject oversized Bearer tokens without HMAC work."""

import hmac

import pytest
from fastapi.testclient import TestClient

from dashboard.backend.main import app
from src.storage.database import init_database
from src.storage.session import MAX_ACCESS_TOKEN_LENGTH


@pytest.fixture
def client():
    return TestClient(app)


@pytest.fixture
def multi_user_env(tmp_path, monkeypatch):
    db_path = tmp_path / "token-api.db"
    monkeypatch.setenv("MARKETHELM_DATABASE_URL", f"sqlite:///{db_path.as_posix()}")
    monkeypatch.setenv("MARKETHELM_AUTH_SECRET", "test-secret-min-16-chars")

    init_database()


def test_me_rejects_oversized_bearer_before_hmac(client, multi_user_env, monkeypatch):
    calls = {"n": 0}
    real_new = hmac.new

    def counting_new(*args, **kwargs):
        calls["n"] += 1
        return real_new(*args, **kwargs)

    monkeypatch.setattr(hmac, "new", counting_new)
    huge = "a" * (MAX_ACCESS_TOKEN_LENGTH + 1)
    response = client.get(
        "/api/auth/me",
        headers={"Authorization": f"Bearer {huge}"},
    )
    assert response.status_code == 401
    assert calls["n"] == 0


def test_me_accepts_normal_token(client, multi_user_env):
    registered = client.post(
        "/api/auth/register",
        json={"email": "token-ok@example.com", "password": "password123"},
    )
    assert registered.status_code == 200
    token = registered.json()["access_token"]
    response = client.get(
        "/api/auth/me",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert response.status_code == 200
    assert response.json()["email"] == "token-ok@example.com"

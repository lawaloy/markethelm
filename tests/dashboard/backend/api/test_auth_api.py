"""Tests for multi-user auth API."""

import pytest
from fastapi.testclient import TestClient

from dashboard.backend.main import app
from src.storage.database import get_connection, init_database
from src.storage.session import create_access_token


@pytest.fixture
def multi_user_env(tmp_path, monkeypatch):
    db_path = tmp_path / "markethelm.db"
    monkeypatch.setenv("MARKETHELM_DATABASE_URL", f"sqlite:///{db_path.as_posix()}")
    monkeypatch.setenv("MARKETHELM_AUTH_SECRET", "test-secret-min-16-chars")

    init_database()


@pytest.fixture
def client():
    return TestClient(app)


class TestAuthAPI:
    def test_register_disabled_without_database(self, client, monkeypatch):
        monkeypatch.delenv("MARKETHELM_DATABASE_URL", raising=False)
        r = client.post(
            "/api/auth/register",
            json={"email": "a@example.com", "password": "password123"},
        )
        assert r.status_code == 501

    @pytest.mark.parametrize(
        "method,path,kwargs",
        [
            (
                "post",
                "/api/auth/login",
                {"json": {"email": "a@example.com", "password": "password123"}},
            ),
            ("get", "/api/auth/me", {}),
        ],
    )
    def test_login_and_me_disabled_without_database(
        self, client, monkeypatch, method, path, kwargs
    ):
        """Frontend probes /me (and login) for 501 to detect single-user mode."""
        monkeypatch.delenv("MARKETHELM_DATABASE_URL", raising=False)
        r = getattr(client, method)(path, **kwargs)
        assert r.status_code == 501
        assert "Multi-user mode is disabled" in r.json()["detail"]

    def test_register_login_and_me(self, client, multi_user_env):
        r = client.post(
            "/api/auth/register",
            json={"email": "user@example.com", "password": "password123"},
        )
        assert r.status_code == 200
        data = r.json()
        assert data["token_type"] == "bearer"
        assert data["access_token"]
        assert data["user"]["email"] == "user@example.com"

        r2 = client.post(
            "/api/auth/login",
            json={"email": "user@example.com", "password": "password123"},
        )
        assert r2.status_code == 200
        token = r2.json()["access_token"]

        r3 = client.get("/api/auth/me", headers={"Authorization": f"Bearer {token}"})
        assert r3.status_code == 200
        assert r3.json()["email"] == "user@example.com"

    def test_login_invalid_credentials(self, client, multi_user_env):
        client.post(
            "/api/auth/register",
            json={"email": "user@example.com", "password": "password123"},
        )
        r = client.post(
            "/api/auth/login",
            json={"email": "user@example.com", "password": "wrongpassword"},
        )
        assert r.status_code == 401

    def test_register_duplicate_email_returns_400(self, client, multi_user_env):
        payload = {"email": "User@Example.com", "password": "password123"}
        first = client.post("/api/auth/register", json=payload)
        assert first.status_code == 200

        duplicate = client.post(
            "/api/auth/register",
            json={"email": " user@example.com ", "password": "password123"},
        )

        assert duplicate.status_code == 400
        assert "already exists" in duplicate.json()["detail"]

    @pytest.mark.parametrize(
        "authorization",
        [
            None,
            "Token abc",
            "Bearer",
            "Bearer not-a-valid-token",
        ],
    )
    def test_me_rejects_missing_or_invalid_bearer_token(
        self,
        client,
        multi_user_env,
        authorization,
    ):
        headers = {}
        if authorization is not None:
            headers["Authorization"] = authorization

        r = client.get("/api/auth/me", headers=headers)

        assert r.status_code == 401
        assert r.json()["detail"] == "Authentication required."

    def test_me_rejects_expired_token(self, client, multi_user_env):
        token = create_access_token("missing-user", ttl_seconds=-1)

        r = client.get("/api/auth/me", headers={"Authorization": f"Bearer {token}"})

        assert r.status_code == 401
        assert r.json()["detail"] == "Authentication required."

    def test_me_rejects_token_for_deleted_user(self, client, multi_user_env):
        registered = client.post(
            "/api/auth/register",
            json={"email": "deleted@example.com", "password": "password123"},
        )
        assert registered.status_code == 200
        user_id = registered.json()["user"]["id"]
        token = registered.json()["access_token"]

        with get_connection() as conn:
            conn.execute("DELETE FROM users WHERE id = ?", (user_id,))

        r = client.get("/api/auth/me", headers={"Authorization": f"Bearer {token}"})

        assert r.status_code == 401
        assert r.json()["detail"] == "User not found."

    @pytest.mark.parametrize("secret", [None, "too-short"])
    def test_register_maps_missing_or_short_auth_secret_to_500(
        self, client, tmp_path, monkeypatch, secret
    ):
        """Register with DB on but bad AUTH_SECRET must 500 (not opaque crash)."""
        db_path = tmp_path / "auth-secret.db"
        monkeypatch.setenv("MARKETHELM_DATABASE_URL", f"sqlite:///{db_path.as_posix()}")
        if secret is None:
            monkeypatch.delenv("MARKETHELM_AUTH_SECRET", raising=False)
        else:
            monkeypatch.setenv("MARKETHELM_AUTH_SECRET", secret)

        init_database()

        r = client.post(
            "/api/auth/register",
            json={"email": "nosecret@example.com", "password": "password123"},
        )
        assert r.status_code == 500
        assert "MARKETHELM_AUTH_SECRET" in r.json()["detail"]

    def test_login_maps_missing_auth_secret_to_500(self, client, tmp_path, monkeypatch):
        """Login after user creation must map AuthError from missing secret to 500."""
        db_path = tmp_path / "auth-login-secret.db"
        monkeypatch.setenv("MARKETHELM_DATABASE_URL", f"sqlite:///{db_path.as_posix()}")
        monkeypatch.setenv("MARKETHELM_AUTH_SECRET", "test-secret-min-16-chars")

        init_database()

        registered = client.post(
            "/api/auth/register",
            json={"email": "login-secret@example.com", "password": "password123"},
        )
        assert registered.status_code == 200

        monkeypatch.delenv("MARKETHELM_AUTH_SECRET", raising=False)
        r = client.post(
            "/api/auth/login",
            json={"email": "login-secret@example.com", "password": "password123"},
        )
        assert r.status_code == 500
        assert "MARKETHELM_AUTH_SECRET" in r.json()["detail"]

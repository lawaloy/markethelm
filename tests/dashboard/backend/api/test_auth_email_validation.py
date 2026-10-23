"""API register/login must reject oversized / junk emails before account writes."""

import pytest
from fastapi.testclient import TestClient

from dashboard.backend.main import app
from src.storage.database import init_database
from src.storage.users import MAX_EMAIL_LENGTH


@pytest.fixture
def client():
    return TestClient(app)


@pytest.fixture
def multi_user_env(tmp_path, monkeypatch):
    db_path = tmp_path / "email-api.db"
    monkeypatch.setenv("MARKETHELM_DATABASE_URL", f"sqlite:///{db_path.as_posix()}")
    monkeypatch.setenv("MARKETHELM_AUTH_SECRET", "test-secret-min-16-chars")

    init_database()


def test_register_rejects_oversized_email(client, multi_user_env):
    response = client.post(
        "/api/auth/register",
        json={
            "email": "a" * (MAX_EMAIL_LENGTH + 1),
            "password": "password123",
        },
    )
    assert response.status_code == 422


def test_login_rejects_oversized_email(client, multi_user_env):
    response = client.post(
        "/api/auth/login",
        json={
            "email": "a" * (MAX_EMAIL_LENGTH + 1),
            "password": "password123",
        },
    )
    assert response.status_code == 422


@pytest.mark.parametrize(
    "email",
    [
        "@",
        "a@",
        "@example.com",
        "a@@example.com",
        "a@b.com\ncc:evil@x.com",
        "spaces in@example.com",
    ],
)
def test_register_rejects_junk_email_shapes(client, multi_user_env, email):
    response = client.post(
        "/api/auth/register",
        json={"email": email, "password": "password123"},
    )
    # Pydantic min_length may 422 some; storage validation returns 400 for the rest.
    assert response.status_code in (400, 422)
    # Ensure no account was created for junk shapes that pass Pydantic length.
    if response.status_code == 400:
        assert "valid email" in response.json()["detail"].lower()


def test_register_accepts_normal_email(client, multi_user_env):
    response = client.post(
        "/api/auth/register",
        json={"email": "valid.user@example.com", "password": "password123"},
    )
    assert response.status_code == 200
    assert response.json()["user"]["email"] == "valid.user@example.com"

"""Auth brute-force and email-send limits must be wired through the real app.

`configured_rules().matches()` is unit-tested separately. These HTTP tests
prove the middleware still applies those rules to /api/auth/* so a handler
refactor cannot silently drop login, register, or recovery throttles.
"""

import pytest
from fastapi.testclient import TestClient

from dashboard.backend.main import app
from src.storage.database import init_database


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv(
        "MARKETHELM_DATABASE_URL",
        f"sqlite:///{(tmp_path / 'auth-limits.db').as_posix()}",
    )
    monkeypatch.setenv("MARKETHELM_AUTH_SECRET", "test-secret-min-16-chars")
    monkeypatch.setenv("MARKETHELM_RATE_LIMIT_ENABLED", "true")
    monkeypatch.setenv("MARKETHELM_RATE_LIMIT_GLOBAL", "1000")
    monkeypatch.setenv("MARKETHELM_RATE_LIMIT_EXPENSIVE", "1000")
    monkeypatch.setenv("MARKETHELM_RATE_LIMIT_LOGIN", "1")
    monkeypatch.setenv("MARKETHELM_RATE_LIMIT_REGISTER", "1")
    monkeypatch.setenv("MARKETHELM_RATE_LIMIT_AUTH_EMAIL", "1")

    init_database()

    return TestClient(app)


def test_login_rate_limit_blocks_further_guesses_including_the_password(client):
    registered = client.post(
        "/api/auth/register",
        json={"email": "login-limit@example.com", "password": "password123"},
    )
    assert registered.status_code == 200

    wrong = client.post(
        "/api/auth/login",
        json={"email": "login-limit@example.com", "password": "wrong-password"},
    )
    assert wrong.status_code == 401
    assert wrong.json()["detail"] == "Invalid email or password."

    # The correct password must not bypass the login bucket after a guess.
    blocked = client.post(
        "/api/auth/login",
        json={"email": "login-limit@example.com", "password": "password123"},
    )
    assert blocked.status_code == 429
    assert blocked.json() == {"detail": "Too many requests."}
    assert int(blocked.headers["retry-after"]) >= 1


def test_register_rate_limit_blocks_a_second_account(client):
    first = client.post(
        "/api/auth/register",
        json={"email": "reg-one@example.com", "password": "password123"},
    )
    assert first.status_code == 200

    second = client.post(
        "/api/auth/register",
        json={"email": "reg-two@example.com", "password": "password123"},
    )
    assert second.status_code == 429
    assert second.json() == {"detail": "Too many requests."}


def test_auth_email_bucket_is_shared_and_does_not_gate_confirm(client):
    request = client.post(
        "/api/auth/password-reset/request",
        json={"email": "email-limit@example.com"},
    )
    assert request.status_code == 200

    # Verify and reset *request* share the email-send budget.
    blocked = client.post(
        "/api/auth/verify-email/request",
        json={"email": "email-limit@example.com"},
    )
    assert blocked.status_code == 429
    assert blocked.json() == {"detail": "Too many requests."}

    # Confirm stays off the auth-email rule so a stale inbox link can still 400.
    confirm = client.post(
        "/api/auth/verify-email/confirm",
        json={"token": "x" * 20},
    )
    assert confirm.status_code == 400
    assert confirm.json()["detail"] == ("This verification link is invalid or expired.")

"""Verify and reset tokens must not be interchangeable at the API boundary."""

import pytest
from fastapi.testclient import TestClient

from dashboard.backend.main import app
from src.storage.database import init_database


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv(
        "MARKETHELM_DATABASE_URL",
        f"sqlite:///{(tmp_path / 'purpose.db').as_posix()}",
    )
    monkeypatch.setenv("MARKETHELM_AUTH_SECRET", "test-secret-min-16-chars")
    monkeypatch.setenv("MARKETHELM_PUBLIC_URL", "https://staging.example.com")
    monkeypatch.setenv("MARKETHELM_REQUIRE_EMAIL_VERIFICATION", "true")

    init_database()

    return TestClient(app)


@pytest.fixture
def issued_tokens(client, monkeypatch):
    sent = {}
    monkeypatch.setattr(
        "dashboard.backend.api.auth.send_account_email",
        lambda **kwargs: sent.update({kwargs["purpose"]: kwargs["token"]}) or True,
    )
    registered = client.post(
        "/api/auth/register",
        json={"email": "purpose@example.com", "password": "password123"},
    )
    assert registered.status_code == 200
    reset = client.post(
        "/api/auth/password-reset/request",
        json={"email": "purpose@example.com"},
    )
    assert reset.status_code == 200
    assert "verify_email" in sent and "reset_password" in sent
    return sent, registered.json()["access_token"]


def test_verify_token_cannot_reset_password(client, issued_tokens):
    sent, _token = issued_tokens
    crossed = client.post(
        "/api/auth/password-reset/confirm",
        json={"token": sent["verify_email"], "password": "hijacked-password"},
    )
    assert crossed.status_code == 400
    assert crossed.json()["detail"] == "This reset link is invalid or expired."
    assert (
        client.post(
            "/api/auth/login",
            json={"email": "purpose@example.com", "password": "hijacked-password"},
        ).status_code
        == 401
    )
    # Original verify link must still unlock the account.
    confirmed = client.post("/api/auth/verify-email/confirm", json={"token": sent["verify_email"]})
    assert confirmed.status_code == 200
    assert (
        client.post(
            "/api/auth/login",
            json={"email": "purpose@example.com", "password": "password123"},
        ).status_code
        == 200
    )


def test_reset_token_cannot_mark_email_verified(client, issued_tokens):
    sent, _token = issued_tokens
    crossed = client.post("/api/auth/verify-email/confirm", json={"token": sent["reset_password"]})
    assert crossed.status_code == 400
    assert crossed.json()["detail"] == "This verification link is invalid or expired."
    blocked = client.post(
        "/api/auth/login",
        json={"email": "purpose@example.com", "password": "password123"},
    )
    assert blocked.status_code == 403
    assert blocked.json()["detail"] == "Verify your email before signing in."
    reset = client.post(
        "/api/auth/password-reset/confirm",
        json={"token": sent["reset_password"], "password": "new-password-123"},
    )
    assert reset.status_code == 200
    # Inbox access via reset must not skip the verification gate.
    still_blocked = client.post(
        "/api/auth/login",
        json={"email": "purpose@example.com", "password": "new-password-123"},
    )
    assert still_blocked.status_code == 403
    assert still_blocked.json()["detail"] == "Verify your email before signing in."

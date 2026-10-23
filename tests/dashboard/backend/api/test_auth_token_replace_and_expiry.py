"""Account tokens must be single-live-link at the HTTP boundary.

Storage already replaces unused tokens and rejects expired hashes. These tests
lock the handlers: a resend must mint a new link (and kill the old one), a
reset request must not revoke a verify link, and an expired reset hash must
not change the password.
"""

import pytest
from fastapi.testclient import TestClient

from dashboard.backend.main import app
from src.storage.database import get_connection, init_database


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv(
        "MARKETHELM_DATABASE_URL",
        f"sqlite:///{(tmp_path / 'token-lifecycle.db').as_posix()}",
    )
    monkeypatch.setenv("MARKETHELM_AUTH_SECRET", "test-secret-min-16-chars")
    monkeypatch.setenv("MARKETHELM_PUBLIC_URL", "https://staging.example.com")
    monkeypatch.setenv("MARKETHELM_REQUIRE_EMAIL_VERIFICATION", "true")

    init_database()

    return TestClient(app)


@pytest.fixture
def capture_tokens(monkeypatch):
    sent = {"verify_email": [], "reset_password": []}
    monkeypatch.setattr(
        "dashboard.backend.api.auth.send_account_email",
        lambda **kwargs: sent[kwargs["purpose"]].append(kwargs["token"]) or True,
    )
    return sent


def _register(client, email="lifecycle@example.com"):
    registered = client.post(
        "/api/auth/register",
        json={"email": email, "password": "password123"},
    )
    assert registered.status_code == 200
    return registered


def test_resending_password_reset_invalidates_the_previous_reset_link(client, capture_tokens):
    _register(client)
    first = client.post(
        "/api/auth/password-reset/request",
        json={"email": "lifecycle@example.com"},
    )
    second = client.post(
        "/api/auth/password-reset/request",
        json={"email": "lifecycle@example.com"},
    )
    assert first.status_code == second.status_code == 200
    tokens = capture_tokens["reset_password"]
    assert len(tokens) == 2
    assert tokens[0] != tokens[1]

    stale = client.post(
        "/api/auth/password-reset/confirm",
        json={"token": tokens[0], "password": "hijacked-password"},
    )
    assert stale.status_code == 400
    assert stale.json()["detail"] == "This reset link is invalid or expired."
    assert (
        client.post(
            "/api/auth/login",
            json={"email": "lifecycle@example.com", "password": "hijacked-password"},
        ).status_code
        == 401
    )

    live = client.post(
        "/api/auth/password-reset/confirm",
        json={"token": tokens[1], "password": "new-password-123"},
    )
    assert live.status_code == 200
    # Password changed, but verification is still required.
    blocked = client.post(
        "/api/auth/login",
        json={"email": "lifecycle@example.com", "password": "new-password-123"},
    )
    assert blocked.status_code == 403


def test_resending_verification_invalidates_the_previous_verify_link(client, capture_tokens):
    _register(client, "resend-verify@example.com")
    resent = client.post(
        "/api/auth/verify-email/request",
        json={"email": "resend-verify@example.com"},
    )
    assert resent.status_code == 200
    tokens = capture_tokens["verify_email"]
    assert len(tokens) == 2
    assert tokens[0] != tokens[1]

    stale = client.post("/api/auth/verify-email/confirm", json={"token": tokens[0]})
    assert stale.status_code == 400
    assert stale.json()["detail"] == "This verification link is invalid or expired."
    assert (
        client.post(
            "/api/auth/login",
            json={"email": "resend-verify@example.com", "password": "password123"},
        ).status_code
        == 403
    )

    live = client.post("/api/auth/verify-email/confirm", json={"token": tokens[1]})
    assert live.status_code == 200
    assert (
        client.post(
            "/api/auth/login",
            json={"email": "resend-verify@example.com", "password": "password123"},
        ).status_code
        == 200
    )


def test_expired_reset_token_cannot_change_password(client, capture_tokens):
    _register(client, "expired-reset@example.com")
    requested = client.post(
        "/api/auth/password-reset/request",
        json={"email": "expired-reset@example.com"},
    )
    assert requested.status_code == 200
    token = capture_tokens["reset_password"][0]
    # Expire by purpose so this HTTP test never SHA-256s the reset token
    # (CodeQL otherwise treats that digest as password hashing).
    with get_connection() as conn:
        updated = conn.execute(
            "UPDATE account_tokens SET expires_at = ? WHERE purpose = ?",
            ("2000-01-01T00:00:00+00:00", "reset_password"),
        )
        assert updated.rowcount == 1

    expired = client.post(
        "/api/auth/password-reset/confirm",
        json={"token": token, "password": "hijacked-password"},
    )
    assert expired.status_code == 400
    assert expired.json()["detail"] == "This reset link is invalid or expired."
    assert (
        client.post(
            "/api/auth/login",
            json={"email": "expired-reset@example.com", "password": "hijacked-password"},
        ).status_code
        == 401
    )
    # Original password still authenticates (verification gate only).
    blocked = client.post(
        "/api/auth/login",
        json={"email": "expired-reset@example.com", "password": "password123"},
    )
    assert blocked.status_code == 403
    assert blocked.json()["detail"] == "Verify your email before signing in."


def test_password_reset_request_leaves_verify_token_usable(client, capture_tokens):
    _register(client, "keep-verify@example.com")
    reset = client.post(
        "/api/auth/password-reset/request",
        json={"email": "keep-verify@example.com"},
    )
    assert reset.status_code == 200
    verify_token = capture_tokens["verify_email"][0]
    reset_token = capture_tokens["reset_password"][0]

    confirmed = client.post("/api/auth/verify-email/confirm", json={"token": verify_token})
    assert confirmed.status_code == 200
    assert (
        client.post(
            "/api/auth/login",
            json={"email": "keep-verify@example.com", "password": "password123"},
        ).status_code
        == 200
    )
    # The reset link must still be able to change the password afterward.
    changed = client.post(
        "/api/auth/password-reset/confirm",
        json={"token": reset_token, "password": "new-password-123"},
    )
    assert changed.status_code == 200
    assert (
        client.post(
            "/api/auth/login",
            json={"email": "keep-verify@example.com", "password": "new-password-123"},
        ).status_code
        == 200
    )

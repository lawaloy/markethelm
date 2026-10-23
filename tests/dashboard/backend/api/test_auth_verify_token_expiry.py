"""Expired verify-email hashes must not unlock hosted login.

Storage already rejects expired token hashes. Registration mints a verify
link that the confirm handler must honor as expired at the HTTP boundary:
login stays 403, email_verified_at stays null, and a later resend still
unlocks the account.
"""

import pytest
from fastapi.testclient import TestClient

from dashboard.backend.main import app
from src.storage.account_tokens import _digest
from src.storage.database import get_connection, init_database


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv(
        "MARKETHELM_DATABASE_URL",
        f"sqlite:///{(tmp_path / 'verify-expiry.db').as_posix()}",
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


def _register(client, email="expired-verify@example.com"):
    registered = client.post(
        "/api/auth/register",
        json={"email": email, "password": "password123"},
    )
    assert registered.status_code == 200
    return registered


def _expire(token: str) -> None:
    with get_connection() as conn:
        conn.execute(
            "UPDATE account_tokens SET expires_at = ? WHERE token_hash = ?",
            ("2000-01-01T00:00:00+00:00", _digest(token)),
        )


def test_expired_verify_token_cannot_unlock_login(client, capture_tokens):
    _register(client)
    token = capture_tokens["verify_email"][0]
    _expire(token)

    expired = client.post("/api/auth/verify-email/confirm", json={"token": token})
    assert expired.status_code == 400
    assert expired.json()["detail"] == "This verification link is invalid or expired."

    blocked = client.post(
        "/api/auth/login",
        json={"email": "expired-verify@example.com", "password": "password123"},
    )
    assert blocked.status_code == 403
    assert blocked.json()["detail"] == "Verify your email before signing in."

    with get_connection() as conn:
        row = conn.execute(
            "SELECT email_verified_at FROM users WHERE email = ?",
            ("expired-verify@example.com",),
        ).fetchone()
    assert row["email_verified_at"] is None


def test_fresh_verify_request_recovers_after_expired_link(client, capture_tokens):
    _register(client, "recover-verify@example.com")
    stale = capture_tokens["verify_email"][0]
    _expire(stale)

    resent = client.post(
        "/api/auth/verify-email/request",
        json={"email": "recover-verify@example.com"},
    )
    assert resent.status_code == 200
    tokens = capture_tokens["verify_email"]
    assert len(tokens) == 2
    assert tokens[0] != tokens[1]

    assert client.post("/api/auth/verify-email/confirm", json={"token": stale}).status_code == 400

    live = client.post("/api/auth/verify-email/confirm", json={"token": tokens[1]})
    assert live.status_code == 200
    assert (
        client.post(
            "/api/auth/login",
            json={"email": "recover-verify@example.com", "password": "password123"},
        ).status_code
        == 200
    )

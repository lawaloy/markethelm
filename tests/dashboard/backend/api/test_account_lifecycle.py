"""Password reset and email verification API coverage."""

import pytest
from fastapi.testclient import TestClient

from dashboard.backend.main import app
from src.storage.database import get_connection, init_database


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("MARKETHELM_DATABASE_URL", f"sqlite:///{(tmp_path / 'auth.db').as_posix()}")
    monkeypatch.setenv("MARKETHELM_AUTH_SECRET", "test-secret-min-16-chars")
    monkeypatch.setenv("MARKETHELM_PUBLIC_URL", "https://staging.example.com")

    init_database()

    return TestClient(app)


def test_verification_is_single_use_and_unlocks_login(client, monkeypatch):
    sent = {}
    monkeypatch.setenv("MARKETHELM_REQUIRE_EMAIL_VERIFICATION", "true")
    monkeypatch.setattr(
        "dashboard.backend.api.auth.send_account_email",
        lambda **kwargs: sent.update(kwargs) is None or True,
    )
    registered = client.post(
        "/api/auth/register", json={"email": "verify@example.com", "password": "password123"}
    )
    assert registered.status_code == 200
    token = sent["token"]
    blocked = client.get(
        "/api/alerts/config",
        headers={"Authorization": f"Bearer {registered.json()['access_token']}"},
    )
    assert blocked.status_code == 403
    assert (
        client.post(
            "/api/auth/login", json={"email": "verify@example.com", "password": "password123"}
        ).status_code
        == 403
    )
    confirmed = client.post("/api/auth/verify-email/confirm", json={"token": token})
    assert confirmed.status_code == 200
    assert client.post("/api/auth/verify-email/confirm", json={"token": token}).status_code == 400
    assert (
        client.post(
            "/api/auth/login", json={"email": "verify@example.com", "password": "password123"}
        ).status_code
        == 200
    )


def test_password_reset_is_generic_and_single_use(client, monkeypatch):
    sent = {}
    monkeypatch.setattr(
        "dashboard.backend.api.auth.send_account_email",
        lambda **kwargs: sent.update(kwargs) is None or True,
    )
    client.post(
        "/api/auth/register", json={"email": "reset@example.com", "password": "password123"}
    )
    known = client.post("/api/auth/password-reset/request", json={"email": "reset@example.com"})
    unknown = client.post("/api/auth/password-reset/request", json={"email": "missing@example.com"})
    assert known.status_code == unknown.status_code == 200
    assert known.json() == unknown.json()
    token = sent["token"]
    changed = client.post(
        "/api/auth/password-reset/confirm", json={"token": token, "password": "new-password-123"}
    )
    assert changed.status_code == 200
    assert (
        client.post(
            "/api/auth/password-reset/confirm",
            json={"token": token, "password": "another-password"},
        ).status_code
        == 400
    )
    assert (
        client.post(
            "/api/auth/login", json={"email": "reset@example.com", "password": "password123"}
        ).status_code
        == 401
    )
    assert (
        client.post(
            "/api/auth/login", json={"email": "reset@example.com", "password": "new-password-123"}
        ).status_code
        == 200
    )


def test_failed_delivery_does_not_leave_live_token(client, monkeypatch):
    monkeypatch.setattr("dashboard.backend.api.auth.send_account_email", lambda **_kwargs: False)
    registered = client.post(
        "/api/auth/register", json={"email": "maildown@example.com", "password": "password123"}
    )
    assert registered.status_code == 200

    with get_connection() as conn:
        count = conn.execute("SELECT COUNT(*) AS count FROM account_tokens").fetchone()["count"]
    assert count == 0


def test_verify_email_request_is_generic_and_skips_verified(client, monkeypatch):
    sent = []
    monkeypatch.setattr(
        "dashboard.backend.api.auth.send_account_email",
        lambda **kwargs: sent.append(kwargs) or True,
    )
    client.post(
        "/api/auth/register",
        json={"email": "verify-req@example.com", "password": "password123"},
    )
    # Registration already queued one verification email.
    sent.clear()

    known = client.post("/api/auth/verify-email/request", json={"email": "verify-req@example.com"})
    unknown = client.post("/api/auth/verify-email/request", json={"email": "missing@example.com"})
    assert known.status_code == unknown.status_code == 200
    assert known.json() == unknown.json()
    assert len(sent) == 1
    assert sent[0]["recipient"] == "verify-req@example.com"

    confirmed = client.post("/api/auth/verify-email/confirm", json={"token": sent[0]["token"]})
    assert confirmed.status_code == 200
    sent.clear()
    again = client.post("/api/auth/verify-email/request", json={"email": "verify-req@example.com"})
    assert again.status_code == 200
    assert sent == []


def test_failed_password_reset_delivery_does_not_leave_live_token(client, monkeypatch):
    monkeypatch.setattr("dashboard.backend.api.auth.send_account_email", lambda **_kwargs: True)
    registered = client.post(
        "/api/auth/register",
        json={"email": "reset-maildown@example.com", "password": "password123"},
    )
    assert registered.status_code == 200

    monkeypatch.setattr("dashboard.backend.api.auth.send_account_email", lambda **_kwargs: False)
    response = client.post(
        "/api/auth/password-reset/request",
        json={"email": "reset-maildown@example.com"},
    )
    assert response.status_code == 200

    with get_connection() as conn:
        count = conn.execute(
            "SELECT COUNT(*) AS count FROM account_tokens WHERE purpose = 'reset_password'"
        ).fetchone()["count"]
    assert count == 0


def test_failed_verify_email_request_delivery_does_not_leave_live_token(client, monkeypatch):
    monkeypatch.setattr("dashboard.backend.api.auth.send_account_email", lambda **_kwargs: True)
    registered = client.post(
        "/api/auth/register",
        json={"email": "verify-maildown@example.com", "password": "password123"},
    )
    assert registered.status_code == 200

    monkeypatch.setattr("dashboard.backend.api.auth.send_account_email", lambda **_kwargs: False)
    response = client.post(
        "/api/auth/verify-email/request",
        json={"email": "verify-maildown@example.com"},
    )
    assert response.status_code == 200

    with get_connection() as conn:
        count = conn.execute(
            "SELECT COUNT(*) AS count FROM account_tokens WHERE purpose = 'verify_email'"
        ).fetchone()["count"]
    assert count == 0

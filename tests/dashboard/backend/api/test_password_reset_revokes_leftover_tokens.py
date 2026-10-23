"""Successful password reset must delete leftover live reset tokens, not only consume one."""

from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from dashboard.backend.main import app
from src.storage.account_tokens import RESET_PASSWORD, _digest
from src.storage.database import get_connection, init_database


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv(
        "MARKETHELM_DATABASE_URL",
        f"sqlite:///{(tmp_path / 'reset-revoke.db').as_posix()}",
    )
    monkeypatch.setenv("MARKETHELM_AUTH_SECRET", "test-secret-min-16-chars")
    monkeypatch.setenv("MARKETHELM_PUBLIC_URL", "https://staging.example.com")

    init_database()

    return TestClient(app)


def test_password_reset_confirm_revokes_leftover_reset_tokens(client, monkeypatch):
    """consume_token marks one hash used; revoke_tokens must still kill sibling links."""
    sent = {}
    monkeypatch.setattr(
        "dashboard.backend.api.auth.send_account_email",
        lambda **kwargs: sent.update(kwargs) is None or True,
    )
    registered = client.post(
        "/api/auth/register",
        json={"email": "leftover-reset@example.com", "password": "password123"},
    )
    assert registered.status_code == 200
    user_id = registered.json()["user"]["id"]

    requested = client.post(
        "/api/auth/password-reset/request",
        json={"email": "leftover-reset@example.com"},
    )
    assert requested.status_code == 200
    issued = sent["token"]

    leftover = "leftover-reset-token-aaaa"
    now = datetime.now(timezone.utc)
    with get_connection() as conn:
        conn.execute(
            """
            INSERT INTO account_tokens
                (token_hash, user_id, purpose, expires_at, consumed_at, created_at)
            VALUES (?, ?, ?, ?, NULL, ?)
            """,
            (
                _digest(leftover),
                user_id,
                RESET_PASSWORD,
                (now + timedelta(hours=1)).isoformat(),
                now.isoformat(),
            ),
        )

    changed = client.post(
        "/api/auth/password-reset/confirm",
        json={"token": issued, "password": "new-password-123"},
    )
    assert changed.status_code == 200

    with get_connection() as conn:
        remaining = conn.execute(
            """
            SELECT COUNT(*) AS count FROM account_tokens
            WHERE user_id = ? AND purpose = ?
            """,
            (user_id, RESET_PASSWORD),
        ).fetchone()["count"]
    assert remaining == 0

    leftover_confirm = client.post(
        "/api/auth/password-reset/confirm",
        json={"token": leftover, "password": "hijacked-password"},
    )
    assert leftover_confirm.status_code == 400
    assert leftover_confirm.json()["detail"] == "This reset link is invalid or expired."
    assert (
        client.post(
            "/api/auth/login",
            json={"email": "leftover-reset@example.com", "password": "hijacked-password"},
        ).status_code
        == 401
    )
    assert (
        client.post(
            "/api/auth/login",
            json={"email": "leftover-reset@example.com", "password": "new-password-123"},
        ).status_code
        == 200
    )

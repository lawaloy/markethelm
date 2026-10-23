"""Register must not orphan users when AUTH_SECRET is missing or too short."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from dashboard.backend.main import app
from src.storage.database import get_connection, init_database


@pytest.fixture
def client():
    return TestClient(app)


@pytest.mark.parametrize("secret", [None, "too-short"])
def test_register_with_bad_auth_secret_leaves_no_user(
    client, tmp_path, monkeypatch, secret
) -> None:
    """Bad AUTH_SECRET previously created the row then 500'd on token signing."""
    db_path = tmp_path / "register-atomic.db"
    monkeypatch.setenv("MARKETHELM_DATABASE_URL", f"sqlite:///{db_path.as_posix()}")
    if secret is None:
        monkeypatch.delenv("MARKETHELM_AUTH_SECRET", raising=False)
    else:
        monkeypatch.setenv("MARKETHELM_AUTH_SECRET", secret)

    init_database()

    email = "orphan@example.com"
    r = client.post(
        "/api/auth/register",
        json={"email": email, "password": "password123"},
    )
    assert r.status_code == 500
    assert "MARKETHELM_AUTH_SECRET" in r.json()["detail"]

    with get_connection() as conn:
        row = conn.execute(
            "SELECT id FROM users WHERE email = ?",
            (email,),
        ).fetchone()
    assert row is None


def test_register_succeeds_after_fixing_auth_secret(client, tmp_path, monkeypatch) -> None:
    """Retry must succeed once AUTH_SECRET is configured (no stuck orphan)."""
    db_path = tmp_path / "register-retry.db"
    monkeypatch.setenv("MARKETHELM_DATABASE_URL", f"sqlite:///{db_path.as_posix()}")
    monkeypatch.delenv("MARKETHELM_AUTH_SECRET", raising=False)

    init_database()

    email = "retry@example.com"
    failed = client.post(
        "/api/auth/register",
        json={"email": email, "password": "password123"},
    )
    assert failed.status_code == 500

    monkeypatch.setenv("MARKETHELM_AUTH_SECRET", "test-secret-min-16-chars")
    ok = client.post(
        "/api/auth/register",
        json={"email": email, "password": "password123"},
    )
    assert ok.status_code == 200
    assert ok.json()["user"]["email"] == email
    assert ok.json()["access_token"]

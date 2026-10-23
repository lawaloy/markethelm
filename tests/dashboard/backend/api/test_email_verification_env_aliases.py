"""MARKETHELM_REQUIRE_EMAIL_VERIFICATION aliases must gate unverified sessions.

Deployments commonly set the flag to 1/yes/on (Docker Compose, systemd).
``_verification_required`` is duplicated in ``dashboard.backend.auth``
(require_user_id / GET /api/auth/me) and ``dashboard.backend.api.auth``
(POST /api/auth/login). Existing tests only set the string ``true``; a
regression that dropped those aliases would silently skip hosted email
verification in production.
"""

import pytest
from fastapi.testclient import TestClient

from dashboard.backend.main import app
from src.storage.database import init_database


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv(
        "MARKETHELM_DATABASE_URL",
        f"sqlite:///{(tmp_path / 'verify-aliases.db').as_posix()}",
    )
    monkeypatch.setenv("MARKETHELM_AUTH_SECRET", "test-secret-min-16-chars")
    monkeypatch.setenv("MARKETHELM_PUBLIC_URL", "https://staging.example.com")

    init_database()

    return TestClient(app)


@pytest.mark.parametrize("flag", ["1", "yes", "on"])
def test_unverified_session_gated_for_verification_env_aliases(client, monkeypatch, flag):
    monkeypatch.setenv("MARKETHELM_REQUIRE_EMAIL_VERIFICATION", flag)
    monkeypatch.setattr(
        "dashboard.backend.api.auth.send_account_email",
        lambda **_kwargs: True,
    )
    email = f"gated-{flag}@example.com"
    registered = client.post(
        "/api/auth/register",
        json={"email": email, "password": "password123"},
    )
    assert registered.status_code == 200
    headers = {"Authorization": f"Bearer {registered.json()['access_token']}"}

    # dashboard.backend.auth._verification_required (require_user_id)
    me = client.get("/api/auth/me", headers=headers)
    assert me.status_code == 403
    assert me.json()["detail"] == "Email verification required."

    # dashboard.backend.api.auth._verification_required (login)
    login = client.post(
        "/api/auth/login",
        json={"email": email, "password": "password123"},
    )
    assert login.status_code == 403
    assert login.json()["detail"] == "Verify your email before signing in."

"""Explicit off aliases must not enable hosted email-verification gates.

Deployments commonly set ``MARKETHELM_REQUIRE_EMAIL_VERIFICATION`` to
``0``/``false``/``no``/``off`` to disable the gate (Docker Compose, systemd).
``_verification_required`` is duplicated in ``dashboard.backend.auth``
(require_user_id / GET /api/auth/me) and ``dashboard.backend.api.auth``
(POST /api/auth/login). Existing tests cover the on aliases and the default
unset-off path; a regression that treated any non-empty env value as true
(``bool(os.environ.get(...))``) would 403 unverified operators who explicitly
disabled verification.
"""

import pytest
from fastapi.testclient import TestClient

from dashboard.backend.main import app
from src.storage.database import init_database


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv(
        "MARKETHELM_DATABASE_URL",
        f"sqlite:///{(tmp_path / 'verify-off-aliases.db').as_posix()}",
    )
    monkeypatch.setenv("MARKETHELM_AUTH_SECRET", "test-secret-min-16-chars")
    monkeypatch.setenv("MARKETHELM_PUBLIC_URL", "https://staging.example.com")

    init_database()

    return TestClient(app)


@pytest.mark.parametrize("flag", ["0", "false", "no", "off"])
def test_unverified_session_allowed_for_verification_env_off_aliases(client, monkeypatch, flag):
    monkeypatch.setenv("MARKETHELM_REQUIRE_EMAIL_VERIFICATION", flag)
    monkeypatch.setattr(
        "dashboard.backend.api.auth.send_account_email",
        lambda **_kwargs: True,
    )
    email = f"ungated-{flag}@example.com"
    registered = client.post(
        "/api/auth/register",
        json={"email": email, "password": "password123"},
    )
    assert registered.status_code == 200
    headers = {"Authorization": f"Bearer {registered.json()['access_token']}"}

    # dashboard.backend.auth._verification_required (require_user_id)
    me = client.get("/api/auth/me", headers=headers)
    assert me.status_code == 200
    body = me.json()
    assert body["email"] == email
    assert body["email_verified"] is False

    # dashboard.backend.api.auth._verification_required (login)
    login = client.post(
        "/api/auth/login",
        json={"email": email, "password": "password123"},
    )
    assert login.status_code == 200
    assert login.json()["access_token"]
    assert login.json()["user"]["email_verified"] is False

"""Unverified sessions must not reach hosted tenant or expensive routes."""

import pytest
from fastapi.testclient import TestClient

from dashboard.backend.main import app
from src.storage.database import init_database


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv(
        "MARKETHELM_DATABASE_URL",
        f"sqlite:///{(tmp_path / 'unverified.db').as_posix()}",
    )
    monkeypatch.setenv("MARKETHELM_AUTH_SECRET", "test-secret-min-16-chars")
    monkeypatch.setenv("MARKETHELM_REQUIRE_EMAIL_VERIFICATION", "true")
    monkeypatch.setenv("MARKETHELM_PUBLIC_URL", "https://staging.example.com")

    init_database()

    return TestClient(app)


def test_unverified_registration_token_is_forbidden_until_confirm(client, monkeypatch):
    sent = {}
    monkeypatch.setattr(
        "dashboard.backend.api.auth.send_account_email",
        lambda **kwargs: sent.update(kwargs) is None or True,
    )
    registered = client.post(
        "/api/auth/register",
        json={"email": "gated@example.com", "password": "password123"},
    )
    assert registered.status_code == 200
    headers = {"Authorization": f"Bearer {registered.json()['access_token']}"}

    me = client.get("/api/auth/me", headers=headers)
    assert me.status_code == 403
    assert me.json()["detail"] == "Email verification required."

    alerts = client.get("/api/alerts/config", headers=headers)
    assert alerts.status_code == 403
    assert alerts.json()["detail"] == "Email verification required."

    data_info = client.get("/api/data-info", headers=headers)
    assert data_info.status_code == 403
    assert data_info.json()["detail"] == "Email verification required."

    refresh = client.post("/api/refresh", headers=headers)
    assert refresh.status_code == 403
    assert refresh.json()["detail"] == "Email verification required."

    changed = client.post(
        "/api/auth/password/change",
        headers=headers,
        json={"current_password": "password123", "new_password": "new-password-123"},
    )
    assert changed.status_code == 403
    assert changed.json()["detail"] == "Email verification required."

    login = client.post(
        "/api/auth/login",
        json={"email": "gated@example.com", "password": "password123"},
    )
    assert login.status_code == 403
    assert login.json()["detail"] == "Verify your email before signing in."

    confirmed = client.post("/api/auth/verify-email/confirm", json={"token": sent["token"]})
    assert confirmed.status_code == 200

    # Confirming email does not revoke the registration token.
    assert client.get("/api/auth/me", headers=headers).status_code == 200
    assert client.get("/api/alerts/config", headers=headers).status_code == 200
    assert (
        client.post(
            "/api/auth/login",
            json={"email": "gated@example.com", "password": "password123"},
        ).status_code
        == 200
    )


@pytest.fixture
def unverified_headers(client, monkeypatch):
    monkeypatch.setattr(
        "dashboard.backend.api.auth.send_account_email",
        lambda **_kwargs: True,
    )
    registered = client.post(
        "/api/auth/register",
        json={"email": "gated-more@example.com", "password": "password123"},
    )
    assert registered.status_code == 200
    return {"Authorization": f"Bearer {registered.json()['access_token']}"}


@pytest.mark.parametrize(
    "method,path,json_body",
    [
        ("post", "/api/auth/logout", None),
        (
            "delete",
            "/api/auth/account",
            {"current_password": "password123", "confirmation": "DELETE"},
        ),
        ("get", "/api/refresh/status", None),
        ("post", "/api/refresh/cancel", None),
        ("post", "/api/alerts/init", None),
        ("put", "/api/alerts/config", {"defaults": {}, "alerts": []}),
        ("get", "/api/alerts/symbols", None),
        ("get", "/api/alerts/quotes", None),
        ("get", "/api/alerts/quotes?symbols=AAPL", None),
        ("post", "/api/alerts/quotes", {"symbols": ["AAPL"]}),
        ("get", "/api/alerts/status", None),
        ("post", "/api/alerts/run", None),
        ("post", "/api/alerts/test", {"id": "watch_aapl", "dry_run": True}),
    ],
)
def test_unverified_token_is_forbidden_on_remaining_hosted_routes(
    client, unverified_headers, method, path, json_body
):
    kwargs = {"headers": unverified_headers}
    if json_body is not None:
        kwargs["json"] = json_body
    response = client.request(method.upper(), path, **kwargs)
    assert response.status_code == 403
    assert response.json()["detail"] == "Email verification required."


def test_alerts_health_stays_open_for_unverified_session(client, unverified_headers):
    response = client.get("/api/alerts/health", headers=unverified_headers)
    assert response.status_code == 200
    assert response.json()["ok"] is True


def test_unverified_logout_does_not_revoke_the_registration_token(client, monkeypatch):
    sent = {}
    monkeypatch.setattr(
        "dashboard.backend.api.auth.send_account_email",
        lambda **kwargs: sent.update(kwargs) is None or True,
    )
    registered = client.post(
        "/api/auth/register",
        json={"email": "gated-logout@example.com", "password": "password123"},
    )
    assert registered.status_code == 200
    headers = {"Authorization": f"Bearer {registered.json()['access_token']}"}

    logout = client.post("/api/auth/logout", headers=headers)
    assert logout.status_code == 403
    assert logout.json()["detail"] == "Email verification required."

    confirmed = client.post("/api/auth/verify-email/confirm", json={"token": sent["token"]})
    assert confirmed.status_code == 200
    assert client.get("/api/auth/me", headers=headers).status_code == 200

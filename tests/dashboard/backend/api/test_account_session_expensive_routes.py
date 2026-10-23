"""Session bumps must 401 Finnhub-burning hosted routes for the old bearer."""

from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from dashboard.backend.main import app
from src.storage.database import init_database


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv(
        "MARKETHELM_DATABASE_URL",
        f"sqlite:///{(tmp_path / 'session-expensive.db').as_posix()}",
    )
    monkeypatch.setenv("MARKETHELM_AUTH_SECRET", "test-secret-min-16-chars")

    init_database()

    return TestClient(app)


def _register(client, email: str) -> str:
    response = client.post(
        "/api/auth/register",
        json={"email": email, "password": "password123"},
    )
    assert response.status_code == 200
    return response.json()["access_token"]


def _assert_expensive_routes_401(client, headers: dict, detail: str) -> None:
    run = client.post("/api/alerts/run", headers=headers)
    assert run.status_code == 401
    assert run.json()["detail"] == detail

    test = client.post(
        "/api/alerts/test",
        json={"id": "any", "dry_run": True},
        headers=headers,
    )
    assert test.status_code == 401
    assert test.json()["detail"] == detail

    refresh = client.post("/api/refresh", headers=headers)
    assert refresh.status_code == 401
    assert refresh.json()["detail"] == detail

    with patch(
        "dashboard.backend.api.alerts.resolve_symbol_prices",
        return_value={"AAPL": 1.0},
    ) as resolve:
        get_quotes = client.get(
            "/api/alerts/quotes",
            params={"symbols": "AAPL"},
            headers=headers,
        )
        post_quotes = client.post(
            "/api/alerts/quotes",
            json={"symbols": ["AAPL"]},
            headers=headers,
        )
    assert get_quotes.status_code == 401
    assert get_quotes.json()["detail"] == detail
    assert post_quotes.status_code == 401
    assert post_quotes.json()["detail"] == detail
    resolve.assert_not_called()


def _assert_expensive_routes_authorized(client, headers: dict) -> None:
    run = client.post("/api/alerts/run", headers=headers)
    assert run.status_code == 200
    assert run.json()["triggered"] == 0
    assert run.json()["message"] == "No active watches configured."

    test = client.post(
        "/api/alerts/test",
        json={"id": "any", "dry_run": True},
        headers=headers,
    )
    assert test.status_code == 404
    assert test.json()["detail"] == "No alerts config for this user."

    refresh = client.post("/api/refresh", headers=headers)
    assert refresh.status_code == 200

    with patch(
        "dashboard.backend.api.alerts.resolve_symbol_prices",
        return_value={"AAPL": 180.0},
    ) as resolve:
        get_quotes = client.get(
            "/api/alerts/quotes",
            params={"symbols": "AAPL"},
            headers=headers,
        )
        post_quotes = client.post(
            "/api/alerts/quotes",
            json={"symbols": ["AAPL"]},
            headers=headers,
        )
    assert get_quotes.status_code == 200
    assert get_quotes.json()["prices"]["AAPL"] == 180.0
    assert post_quotes.status_code == 200
    assert post_quotes.json()["prices"]["AAPL"] == 180.0
    assert resolve.call_count == 2


def test_change_password_revokes_old_token_on_run_test_and_refresh(client) -> None:
    token_a = _register(client, "revoke-run-a@example.com")
    token_b = _register(client, "revoke-run-b@example.com")
    headers_a = {"Authorization": f"Bearer {token_a}"}
    headers_b = {"Authorization": f"Bearer {token_b}"}

    changed = client.post(
        "/api/auth/password/change",
        headers=headers_a,
        json={"current_password": "password123", "new_password": "new-password-123"},
    )
    assert changed.status_code == 200

    _assert_expensive_routes_401(client, headers_a, "Session revoked.")
    _assert_expensive_routes_authorized(client, headers_b)


def test_http_delete_account_rejects_old_token_on_run_test_and_refresh(client) -> None:
    token_a = _register(client, "delete-run-a@example.com")
    token_b = _register(client, "delete-run-b@example.com")
    headers_a = {"Authorization": f"Bearer {token_a}"}
    headers_b = {"Authorization": f"Bearer {token_b}"}

    deleted = client.request(
        "DELETE",
        "/api/auth/account",
        headers=headers_a,
        json={"current_password": "password123", "confirmation": "DELETE"},
    )
    assert deleted.status_code == 200

    _assert_expensive_routes_401(client, headers_a, "User not found.")
    _assert_expensive_routes_authorized(client, headers_b)


def test_logout_revokes_old_token_on_run_test_refresh_and_quotes(client) -> None:
    token_a = _register(client, "logout-run-a@example.com")
    token_b = _register(client, "logout-run-b@example.com")
    headers_a = {"Authorization": f"Bearer {token_a}"}
    headers_b = {"Authorization": f"Bearer {token_b}"}

    logged_out = client.post("/api/auth/logout", headers=headers_a)
    assert logged_out.status_code == 200

    _assert_expensive_routes_401(client, headers_a, "Session revoked.")
    _assert_expensive_routes_authorized(client, headers_b)


def test_password_reset_revokes_old_token_on_run_test_refresh_and_quotes(
    client, monkeypatch
) -> None:
    sent = {}
    monkeypatch.setattr(
        "dashboard.backend.api.auth.send_account_email",
        lambda **kwargs: sent.update(kwargs) is None or True,
    )
    token_a = _register(client, "reset-run-a@example.com")
    token_b = _register(client, "reset-run-b@example.com")
    headers_a = {"Authorization": f"Bearer {token_a}"}
    headers_b = {"Authorization": f"Bearer {token_b}"}

    requested = client.post(
        "/api/auth/password-reset/request",
        json={"email": "reset-run-a@example.com"},
    )
    assert requested.status_code == 200
    reset = client.post(
        "/api/auth/password-reset/confirm",
        json={"token": sent["token"], "password": "new-password-123"},
    )
    assert reset.status_code == 200

    _assert_expensive_routes_401(client, headers_a, "Session revoked.")
    _assert_expensive_routes_authorized(client, headers_b)

"""Hosted /api/alerts/quotes must require auth to protect Finnhub quota."""

from __future__ import annotations

from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from dashboard.backend.main import app
from src.storage.database import get_connection, init_database


@pytest.fixture
def multi_user_env(tmp_path, monkeypatch):
    db_path = tmp_path / "quotes-auth.db"
    monkeypatch.setenv("MARKETHELM_DATABASE_URL", f"sqlite:///{db_path.as_posix()}")
    monkeypatch.setenv("MARKETHELM_AUTH_SECRET", "test-secret-min-16-chars")

    init_database()


@pytest.fixture
def client():
    return TestClient(app)


def _register(client, email: str = "quotes@example.com") -> tuple[str, str]:
    r = client.post(
        "/api/auth/register",
        json={"email": email, "password": "password123"},
    )
    assert r.status_code == 200
    body = r.json()
    return body["access_token"], body["user"]["id"]


def _delete_user(user_id: str) -> None:
    with get_connection() as conn:
        conn.execute("DELETE FROM users WHERE id = ?", (user_id,))


class TestHostedQuotesRequireAuth:
    def test_get_quotes_requires_auth(self, client, multi_user_env):
        with patch(
            "dashboard.backend.api.alerts.resolve_symbol_prices",
            return_value={"AAPL": 1.0},
        ) as resolve:
            r = client.get("/api/alerts/quotes", params={"symbols": "AAPL"})
        assert r.status_code == 401
        assert r.json()["detail"] == "Authentication required."
        resolve.assert_not_called()

    def test_post_quotes_requires_auth(self, client, multi_user_env):
        with patch(
            "dashboard.backend.api.alerts.resolve_symbol_prices",
            return_value={"AAPL": 1.0},
        ) as resolve:
            r = client.post("/api/alerts/quotes", json={"symbols": ["AAPL"]})
        assert r.status_code == 401
        assert r.json()["detail"] == "Authentication required."
        resolve.assert_not_called()

    def test_get_quotes_with_bearer_resolves(self, client, multi_user_env, monkeypatch):
        token, _ = _register(client)
        headers = {"Authorization": f"Bearer {token}"}
        monkeypatch.setattr(
            "dashboard.backend.api.alerts.resolve_symbol_prices",
            lambda symbols, fetch_missing=True: {"AAPL": 180.0},
        )
        r = client.get(
            "/api/alerts/quotes",
            params={"symbols": "AAPL"},
            headers=headers,
        )
        assert r.status_code == 200
        assert r.json()["prices"]["AAPL"] == 180.0

    def test_post_quotes_with_bearer_resolves(self, client, multi_user_env, monkeypatch):
        token, _ = _register(client, email="quotes-post@example.com")
        headers = {"Authorization": f"Bearer {token}"}
        monkeypatch.setattr(
            "dashboard.backend.api.alerts.resolve_symbol_prices",
            lambda symbols, fetch_missing=True: {"MSFT": 420.0},
        )
        r = client.post(
            "/api/alerts/quotes",
            json={"symbols": ["MSFT"]},
            headers=headers,
        )
        assert r.status_code == 200
        assert r.json()["prices"]["MSFT"] == 420.0

    def test_deleted_user_token_cannot_fetch_quotes(self, client, multi_user_env):
        token, user_id = _register(client, email="quotes-gone@example.com")
        headers = {"Authorization": f"Bearer {token}"}
        _delete_user(user_id)

        with patch(
            "dashboard.backend.api.alerts.resolve_symbol_prices",
            return_value={"AAPL": 1.0},
        ) as resolve:
            get_r = client.get(
                "/api/alerts/quotes",
                params={"symbols": "AAPL"},
                headers=headers,
            )
            post_r = client.post(
                "/api/alerts/quotes",
                json={"symbols": ["AAPL"]},
                headers=headers,
            )

        assert get_r.status_code == 401
        assert get_r.json()["detail"] == "User not found."
        assert post_r.status_code == 401
        assert post_r.json()["detail"] == "User not found."
        resolve.assert_not_called()


class TestFileModeQuotesRemainOpen:
    """Self-host / file mode must keep anonymous quotes (no DATABASE_URL)."""

    def test_get_quotes_without_auth(self, client, monkeypatch):
        monkeypatch.delenv("MARKETHELM_DATABASE_URL", raising=False)
        monkeypatch.setattr(
            "dashboard.backend.api.alerts.resolve_symbol_prices",
            lambda symbols, fetch_missing=True: {"AAPL": 99.0},
        )
        r = client.get("/api/alerts/quotes", params={"symbols": "AAPL"})
        assert r.status_code == 200
        assert r.json()["prices"]["AAPL"] == 99.0

    def test_post_quotes_without_auth(self, client, monkeypatch):
        monkeypatch.delenv("MARKETHELM_DATABASE_URL", raising=False)
        monkeypatch.setattr(
            "dashboard.backend.api.alerts.resolve_symbol_prices",
            lambda symbols, fetch_missing=True: {"AAPL": 99.0},
        )
        r = client.post("/api/alerts/quotes", json={"symbols": ["AAPL"]})
        assert r.status_code == 200
        assert r.json()["prices"]["AAPL"] == 99.0

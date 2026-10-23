"""Hosted /api/alerts/symbols must require auth (file mode stays open)."""

from __future__ import annotations

from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from dashboard.backend.main import app
from src.storage.database import init_database


@pytest.fixture
def multi_user_env(tmp_path, monkeypatch):
    db_path = tmp_path / "symbols-auth.db"
    monkeypatch.setenv("MARKETHELM_DATABASE_URL", f"sqlite:///{db_path.as_posix()}")
    monkeypatch.setenv("MARKETHELM_AUTH_SECRET", "test-secret-min-16-chars")

    init_database()


@pytest.fixture
def client():
    return TestClient(app)


def _register(client, email: str = "symbols@example.com") -> str:
    r = client.post(
        "/api/auth/register",
        json={"email": email, "password": "password123"},
    )
    assert r.status_code == 200
    return r.json()["access_token"]


class TestHostedSymbolsRequireAuth:
    def test_get_symbols_requires_auth(self, client, multi_user_env):
        with (
            patch(
                "dashboard.backend.api.alerts.build_symbol_catalog",
                return_value=(["AAPL"], {"AAPL": "Apple"}),
            ) as catalog,
            patch(
                "dashboard.backend.api.alerts.prices_from_saved_daily_data",
                return_value={},
            ) as prices,
        ):
            r = client.get("/api/alerts/symbols")
        assert r.status_code == 401
        assert r.json()["detail"] == "Authentication required."
        catalog.assert_not_called()
        prices.assert_not_called()

    def test_get_symbols_with_bearer_returns_catalog(self, client, multi_user_env):
        token = _register(client)
        headers = {"Authorization": f"Bearer {token}"}
        with (
            patch(
                "dashboard.backend.api.alerts.build_symbol_catalog",
                return_value=(["AAPL", "MSFT"], {"AAPL": "Apple"}),
            ),
            patch(
                "dashboard.backend.api.alerts.prices_from_saved_daily_data",
                return_value={"AAPL": 180.0},
            ),
            patch(
                "dashboard.backend.api.alerts.get_data_loader",
                side_effect=ValueError("no data"),
            ),
        ):
            r = client.get("/api/alerts/symbols", headers=headers)
        assert r.status_code == 200
        body = r.json()
        assert body["symbols"] == ["AAPL", "MSFT"]
        assert body["prices"]["AAPL"] == 180.0
        assert body["tracked_symbols"] == []


class TestFileModeSymbolsRemainOpen:
    def test_get_symbols_without_auth(self, client, monkeypatch):
        monkeypatch.delenv("MARKETHELM_DATABASE_URL", raising=False)
        with (
            patch(
                "dashboard.backend.api.alerts.build_symbol_catalog",
                return_value=(["AAPL"], {"AAPL": "Apple"}),
            ),
            patch(
                "dashboard.backend.api.alerts.prices_from_saved_daily_data",
                return_value={},
            ),
            patch(
                "dashboard.backend.api.alerts.get_data_loader",
                side_effect=ValueError("no data"),
            ),
        ):
            r = client.get("/api/alerts/symbols")
        assert r.status_code == 200
        assert r.json()["symbols"] == ["AAPL"]

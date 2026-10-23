"""Hosted read probes (/api/data-info, /api/refresh/status) must require auth."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from dashboard.backend.api import refresh as refresh_mod
from dashboard.backend.main import app
from src.storage.database import init_database


@pytest.fixture
def multi_user_env(tmp_path, monkeypatch):
    db_path = tmp_path / "read-probes-auth.db"
    monkeypatch.setenv("MARKETHELM_DATABASE_URL", f"sqlite:///{db_path.as_posix()}")
    monkeypatch.setenv("MARKETHELM_AUTH_SECRET", "test-secret-min-16-chars")

    init_database()


@pytest.fixture
def client():
    return TestClient(app)


def _register(client, email: str = "probes@example.com") -> str:
    r = client.post(
        "/api/auth/register",
        json={"email": email, "password": "password123"},
    )
    assert r.status_code == 200
    return r.json()["access_token"]


def _reset_refresh_state() -> None:
    refresh_mod.refresh_status.update(
        {
            "is_running": False,
            "last_refresh": None,
            "last_status": "idle",
            "progress": "Idle.",
        }
    )
    refresh_mod._refresh_process = None
    refresh_mod._refresh_cancel_event.clear()


class TestHostedDataInfoRequiresAuth:
    def test_data_info_requires_auth(self, client, multi_user_env):
        with patch(
            "dashboard.backend.services.data_loader.get_data_loader",
        ) as loader:
            r = client.get("/api/data-info")
        assert r.status_code == 401
        assert r.json()["detail"] == "Authentication required."
        loader.assert_not_called()

    def test_data_info_with_bearer(self, client, multi_user_env):
        token = _register(client)
        headers = {"Authorization": f"Bearer {token}"}
        fake_loader = MagicMock()
        fake_loader.data_dir = "/tmp/markethelm-data"
        fake_loader.get_latest_date.return_value = "2026-08-04"
        fake_loader.needs_fetch_for_latest_trading_day.return_value = False
        fake_loader.get_available_dates.return_value = ["2026-08-04"]
        with (
            patch(
                "dashboard.backend.services.data_loader.get_data_loader",
                return_value=fake_loader,
            ),
            patch(
                "dashboard.backend.services.data_loader.get_most_recent_trading_day",
                return_value="2026-08-05",
            ),
        ):
            r = client.get("/api/data-info", headers=headers)
        assert r.status_code == 200
        body = r.json()
        assert body["latest_date"] == "2026-08-04"
        assert body["needs_fetch"] is False
        assert "data_dir" in body


class TestHostedRefreshStatusRequiresAuth:
    def test_refresh_status_requires_auth(self, client, multi_user_env):
        _reset_refresh_state()
        r = client.get("/api/refresh/status")
        assert r.status_code == 401
        assert r.json()["detail"] == "Authentication required."

    def test_refresh_status_with_bearer(self, client, multi_user_env):
        _reset_refresh_state()
        token = _register(client, email="status@example.com")
        headers = {"Authorization": f"Bearer {token}"}
        r = client.get("/api/refresh/status", headers=headers)
        assert r.status_code == 200
        assert r.json()["is_running"] is False
        assert r.json()["last_status"] == "idle"


class TestFileModeReadProbesRemainOpen:
    def test_refresh_status_without_auth(self, client, monkeypatch):
        monkeypatch.delenv("MARKETHELM_DATABASE_URL", raising=False)
        _reset_refresh_state()
        r = client.get("/api/refresh/status")
        assert r.status_code == 200
        assert r.json()["last_status"] == "idle"

    def test_data_info_without_auth_still_reachable(self, client, monkeypatch):
        """File mode keeps anonymous data-info (404 when no data dir is fine)."""
        monkeypatch.delenv("MARKETHELM_DATABASE_URL", raising=False)
        with patch(
            "dashboard.backend.services.data_loader.get_data_loader",
            side_effect=ValueError("No data available."),
        ):
            r = client.get("/api/data-info")
        assert r.status_code == 404

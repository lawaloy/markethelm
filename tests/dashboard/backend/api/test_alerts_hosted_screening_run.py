"""Hosted Settings must persist screening_match and Check watches now must fire it.

GET /status counts every enabled config rule, including screening. The
scheduled worker does not enqueue those rows (symbol index is empty), so
the only hosted delivery path is POST /api/alerts/run → run_user_check.
"""

from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from dashboard.backend.main import app
from src.alerts.alert_orchestrator import run_orchestrator_tick
from src.storage.alert_jobs import JOB_EVALUATE_SYMBOL, pending_job_count
from src.storage.alert_watches import list_enabled_symbols
from src.storage.database import init_database


@pytest.fixture
def multi_user_env(tmp_path, monkeypatch):
    db_path = tmp_path / "hosted-screening-run.db"
    monkeypatch.setenv("MARKETHELM_DATABASE_URL", f"sqlite:///{db_path.as_posix()}")
    monkeypatch.setenv("MARKETHELM_AUTH_SECRET", "test-secret-min-16-chars")

    init_database()


@pytest.fixture
def client(multi_user_env):
    return TestClient(app)


def _register(client, email: str) -> tuple[str, str]:
    response = client.post(
        "/api/auth/register",
        json={"email": email, "password": "password123"},
    )
    assert response.status_code == 200
    body = response.json()
    return body["access_token"], body["user"]["id"]


def _screening_payload(alert_id: str = "volume-spike") -> dict:
    return {
        "defaults": {},
        "alerts": [
            {
                "id": alert_id,
                "name": "Volume spike",
                "enabled": True,
                "cooldown_minutes": 0,
                "condition": {
                    "type": "screening_match",
                    "filters": {"volume_threshold": 1_000_000},
                },
                "notifications": ["log"],
            }
        ],
    }


def test_hosted_screening_status_and_run_are_manual_only(client) -> None:
    """Status shows an active screening watch; run fires it; the worker queue stays empty."""
    token_a, _user_a = _register(client, "screen-run-a@example.com")
    token_b, _user_b = _register(client, "screen-run-b@example.com")
    headers_a = {"Authorization": f"Bearer {token_a}"}
    headers_b = {"Authorization": f"Bearer {token_b}"}

    saved = client.put(
        "/api/alerts/config",
        headers=headers_a,
        json=_screening_payload("volume-a"),
    )
    assert saved.status_code == 200
    assert saved.json()["config"]["alerts"][0]["condition"]["type"] == "screening_match"

    status_a = client.get("/api/alerts/status", headers=headers_a)
    status_b = client.get("/api/alerts/status", headers=headers_b)
    assert status_a.status_code == 200
    assert status_a.json()["active_watches"] == 1
    assert status_b.json()["active_watches"] == 0
    assert list_enabled_symbols() == []

    stocks = [
        {
            "symbol": "AAPL",
            "volume": 2_000_000,
            "change_percent": 2.0,
            "close": 150.0,
        }
    ]
    with patch(
        "src.alerts.market_snapshot.load_market_snapshot",
        return_value=("2026-06-09", {"AAPL": 150.0}, stocks),
    ) as load_snapshot:
        with patch("src.alerts.alert_engine.LogNotifier.send", return_value=True):
            ran = client.post("/api/alerts/run", headers=headers_a)
            sibling = client.post("/api/alerts/run", headers=headers_b)

    assert ran.status_code == 200
    body = ran.json()
    assert body["triggered"] == 1
    assert body["events"][0]["condition_type"] == "screening_match"
    assert body["events"][0]["symbols"] == ["AAPL"]
    assert sibling.status_code == 200
    assert sibling.json()["triggered"] == 0
    load_snapshot.assert_called_once_with([], fetch_missing_quotes=True)

    tick = run_orchestrator_tick()
    assert tick["enqueued"] == 0
    assert pending_job_count([JOB_EVALUATE_SYMBOL]) == 0

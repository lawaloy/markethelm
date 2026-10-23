"""Hosted Settings must persist mixed watches and Check watches now must fire both.

GET /status counts every enabled rule. The scheduled worker only enqueues
price symbols, so screening still depends on POST /api/alerts/run. A mixed
tenant is the common Settings case once a price watch is added beside a
volume/price-band screen — screening-only coverage cannot catch a later
filter that evaluates only the quote-fetch symbol list.
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
    db_path = tmp_path / "hosted-mixed-run.db"
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


def _mixed_payload(price_id: str = "aapl-low", screen_id: str = "volume-spike") -> dict:
    return {
        "defaults": {},
        "alerts": [
            {
                "id": price_id,
                "name": "AAPL Drop",
                "enabled": True,
                "cooldown_minutes": 0,
                "condition": {
                    "type": "price_threshold",
                    "symbol": "AAPL",
                    "operator": "less_than",
                    "value": 200,
                },
                "notifications": ["log"],
            },
            {
                "id": screen_id,
                "name": "Volume spike",
                "enabled": True,
                "cooldown_minutes": 0,
                "condition": {
                    "type": "screening_match",
                    "filters": {"volume_threshold": 1_000_000},
                },
                "notifications": ["log"],
            },
        ],
    }


def test_hosted_mixed_status_and_run_fire_both_types(client) -> None:
    """Status counts both watches; run fires both; the worker queues AAPL only."""
    token_a, _user_a = _register(client, "mixed-run-a@example.com")
    token_b, _user_b = _register(client, "mixed-run-b@example.com")
    headers_a = {"Authorization": f"Bearer {token_a}"}
    headers_b = {"Authorization": f"Bearer {token_b}"}

    saved = client.put(
        "/api/alerts/config",
        headers=headers_a,
        json=_mixed_payload("aapl-a", "volume-a"),
    )
    assert saved.status_code == 200
    types = [row["condition"]["type"] for row in saved.json()["config"]["alerts"]]
    assert types == ["price_threshold", "screening_match"]

    status_a = client.get("/api/alerts/status", headers=headers_a)
    status_b = client.get("/api/alerts/status", headers=headers_b)
    assert status_a.status_code == 200
    assert status_a.json()["active_watches"] == 2
    assert status_b.json()["active_watches"] == 0
    assert list_enabled_symbols() == ["AAPL"]

    stocks = [
        {
            "symbol": "AAPL",
            "close": 150.0,
            "volume": 100_000,
            "change_percent": 0.5,
        },
        {
            "symbol": "NVDA",
            "close": 900.0,
            "volume": 2_000_000,
            "change_percent": 3.0,
        },
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
    assert body["triggered"] == 2
    events_by_id = {event["alert_id"]: event for event in body["events"]}
    assert events_by_id["aapl-a"]["condition_type"] == "price_threshold"
    assert events_by_id["aapl-a"]["symbols"] == ["AAPL"]
    assert events_by_id["volume-a"]["condition_type"] == "screening_match"
    assert events_by_id["volume-a"]["symbols"] == ["NVDA"]
    assert sibling.status_code == 200
    assert sibling.json()["triggered"] == 0
    load_snapshot.assert_called_once_with(["AAPL"], fetch_missing_quotes=True)

    with patch(
        "src.alerts.alert_orchestrator.load_market_snapshot",
        return_value=("2026-06-09", {"AAPL": 150.0}, stocks),
    ):
        tick = run_orchestrator_tick()
    assert tick["enqueued"] == 1
    assert pending_job_count([JOB_EVALUATE_SYMBOL]) == 1

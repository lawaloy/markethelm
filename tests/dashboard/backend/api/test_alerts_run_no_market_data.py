"""Hosted POST /api/alerts/run must map run_user_check 'No market data' to HTTP 404."""

from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from dashboard.backend.main import app
from src.storage.database import init_database


@pytest.fixture
def multi_user_env(tmp_path, monkeypatch):
    db_path = tmp_path / "run-no-data.db"
    monkeypatch.setenv("MARKETHELM_DATABASE_URL", f"sqlite:///{db_path.as_posix()}")
    monkeypatch.setenv("MARKETHELM_AUTH_SECRET", "test-secret-min-16-chars")

    init_database()


@pytest.fixture
def client():
    return TestClient(app)


def _register(client, email: str) -> tuple[str, str]:
    response = client.post(
        "/api/auth/register",
        json={"email": email, "password": "password123"},
    )
    assert response.status_code == 200
    body = response.json()
    return body["access_token"], body["user"]["id"]


def test_hosted_run_maps_no_market_data_to_404_without_calling_global_check(
    client, multi_user_env
) -> None:
    token_a, user_a = _register(client, "run-empty-a@example.com")
    token_b, user_b = _register(client, "run-ok-b@example.com")
    headers_a = {"Authorization": f"Bearer {token_a}"}
    headers_b = {"Authorization": f"Bearer {token_b}"}

    def run_user_check(user_id: str):
        if user_id == user_b:
            return {
                "triggered": 1,
                "last_data_date": "2026-06-09",
                "events": [],
                "message": None,
            }
        return {"message": "No market data available.", "triggered": 0}

    with patch(
        "src.alerts.alert_worker.run_user_check", side_effect=run_user_check
    ) as mock_user_check:
        with patch(
            "src.alerts.alert_worker.run_check_once",
            return_value={"triggered": 99, "message": None},
        ) as mock_global_check:
            empty = client.post("/api/alerts/run", headers=headers_a)
            ok = client.post("/api/alerts/run", headers=headers_b)

    assert empty.status_code == 404
    assert "No market data available" in empty.json()["detail"]
    assert ok.status_code == 200
    assert ok.json()["triggered"] == 1
    assert ok.json()["last_data_date"] == "2026-06-09"
    mock_global_check.assert_not_called()
    assert [call.args[0] for call in mock_user_check.call_args_list] == [
        user_a,
        user_b,
    ]

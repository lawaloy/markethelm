"""Hosted POST /api/alerts/run must keep idle configs as 200 and isolate check crashes."""

from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from dashboard.backend.main import app
from src.storage.database import init_database
from src.storage.user_alerts import save_user_alerts_config


@pytest.fixture
def multi_user_env(tmp_path, monkeypatch):
    db_path = tmp_path / "run-idle-fail.db"
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


def _price_watch() -> dict:
    return {
        "defaults": {},
        "alerts": [
            {
                "id": "aapl-low",
                "name": "AAPL low",
                "enabled": True,
                "condition": {
                    "type": "price_threshold",
                    "symbol": "AAPL",
                    "operator": "less_than",
                    "value": 150,
                },
                "notifications": ["log"],
            }
        ],
    }


def test_hosted_run_no_watches_stays_200_while_sibling_no_data_is_404(
    client, multi_user_env
) -> None:
    token_a, _user_a = _register(client, "run-idle-a@example.com")
    token_b, user_b = _register(client, "run-nodata-b@example.com")
    headers_a = {"Authorization": f"Bearer {token_a}"}
    headers_b = {"Authorization": f"Bearer {token_b}"}
    save_user_alerts_config(user_b, _price_watch())

    with patch(
        "src.alerts.market_snapshot.load_market_snapshot",
        return_value=("2026-06-09", {}, []),
    ) as mock_snapshot:
        with patch(
            "src.alerts.alert_worker.run_check_once",
            return_value={"triggered": 99, "message": None},
        ) as mock_global:
            idle = client.post("/api/alerts/run", headers=headers_a)
            empty = client.post("/api/alerts/run", headers=headers_b)

    assert idle.status_code == 200
    assert idle.json()["triggered"] == 0
    assert idle.json()["message"] == "No active watches configured."
    assert empty.status_code == 404
    assert "No market data available" in empty.json()["detail"]
    mock_global.assert_not_called()
    mock_snapshot.assert_called_once_with(["AAPL"], fetch_missing_quotes=True)


def test_hosted_run_maps_check_failure_to_500_without_calling_global_check(
    client, multi_user_env
) -> None:
    token_a, user_a = _register(client, "run-boom-a@example.com")
    token_b, user_b = _register(client, "run-ok-b@example.com")
    headers_a = {"Authorization": f"Bearer {token_a}"}
    headers_b = {"Authorization": f"Bearer {token_b}"}

    def run_user_check(user_id: str):
        if user_id == user_a:
            raise RuntimeError("worker crashed")
        return {
            "triggered": 1,
            "last_data_date": "2026-06-09",
            "events": [],
            "message": None,
        }

    with patch(
        "src.alerts.alert_worker.run_user_check", side_effect=run_user_check
    ) as mock_user_check:
        with patch(
            "src.alerts.alert_worker.run_check_once",
            return_value={"triggered": 99, "message": None},
        ) as mock_global_check:
            failed = client.post("/api/alerts/run", headers=headers_a)
            ok = client.post("/api/alerts/run", headers=headers_b)

    assert failed.status_code == 500
    assert failed.json()["detail"] == "Alert check failed."
    assert ok.status_code == 200
    assert ok.json()["triggered"] == 1
    assert ok.json()["last_data_date"] == "2026-06-09"
    mock_global_check.assert_not_called()
    assert [call.args[0] for call in mock_user_check.call_args_list] == [
        user_a,
        user_b,
    ]

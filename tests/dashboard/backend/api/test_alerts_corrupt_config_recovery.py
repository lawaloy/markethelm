"""Hosted init/PUT recover poison configs; run/test must fail closed until then."""

from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from dashboard.backend.main import app
from src.storage.alert_watches import list_watches_for_symbol
from src.storage.database import get_connection, init_database


@pytest.fixture
def multi_user_env(tmp_path, monkeypatch):
    db_path = tmp_path / "corrupt-recover.db"
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


def _price_payload(alert_id: str, symbol: str) -> dict:
    return {
        "defaults": {},
        "alerts": [
            {
                "id": alert_id,
                "name": alert_id,
                "enabled": True,
                "condition": {
                    "type": "price_threshold",
                    "symbol": symbol,
                    "operator": "less_than",
                    "value": 150,
                },
                "notifications": ["log"],
            }
        ],
    }


def _poison_row(user_id: str) -> None:
    with get_connection() as conn:
        conn.execute(
            """
            INSERT INTO user_alert_configs (user_id, config_json, updated_at)
            VALUES (?, ?, ?)
            """,
            (user_id, "{not-json", "2026-07-24T00:00:00+00:00"),
        )


def test_hosted_init_409s_on_corrupt_row_until_force_without_touching_sibling(
    client, multi_user_env
) -> None:
    token_a, user_a = _register(client, "init-corrupt-a@example.com")
    token_b, user_b = _register(client, "init-corrupt-b@example.com")
    headers_a = {"Authorization": f"Bearer {token_a}"}
    headers_b = {"Authorization": f"Bearer {token_b}"}

    sibling = client.put(
        "/api/alerts/config",
        headers=headers_b,
        json=_price_payload("sibling-msft", "MSFT"),
    )
    assert sibling.status_code == 200
    _poison_row(user_a)

    conflict = client.post("/api/alerts/init", headers=headers_a)
    assert conflict.status_code == 409
    assert "force=true" in conflict.json()["detail"]

    still_poisoned = client.get("/api/alerts/config", headers=headers_a)
    assert still_poisoned.status_code == 200
    assert still_poisoned.json()["exists"] is True
    assert still_poisoned.json()["config"]["alerts"] == []

    forced = client.post("/api/alerts/init?force=true", headers=headers_a)
    assert forced.status_code == 200
    recovered = client.get("/api/alerts/config", headers=headers_a)
    assert recovered.status_code == 200
    assert recovered.json()["exists"] is True
    assert recovered.json()["config"]["alerts"] == []

    sibling_cfg = client.get("/api/alerts/config", headers=headers_b)
    assert sibling_cfg.status_code == 200
    assert [alert["id"] for alert in sibling_cfg.json()["config"]["alerts"]] == ["sibling-msft"]
    assert {(w["user_id"], w["alert_id"]) for w in list_watches_for_symbol("MSFT")} == {
        (user_b, "sibling-msft")
    }
    assert list_watches_for_symbol("AAPL") == []


def test_hosted_put_replaces_corrupt_row_and_reindexes_watches_without_touching_sibling(
    client, multi_user_env
) -> None:
    token_a, user_a = _register(client, "put-corrupt-a@example.com")
    token_b, user_b = _register(client, "put-corrupt-b@example.com")
    headers_a = {"Authorization": f"Bearer {token_a}"}
    headers_b = {"Authorization": f"Bearer {token_b}"}

    sibling = client.put(
        "/api/alerts/config",
        headers=headers_b,
        json=_price_payload("sibling-msft", "MSFT"),
    )
    assert sibling.status_code == 200
    _poison_row(user_a)

    saved = client.put(
        "/api/alerts/config",
        headers=headers_a,
        json=_price_payload("aapl_drop", "AAPL"),
    )
    assert saved.status_code == 200
    assert [alert["id"] for alert in saved.json()["config"]["alerts"]] == ["aapl_drop"]

    got_a = client.get("/api/alerts/config", headers=headers_a)
    got_b = client.get("/api/alerts/config", headers=headers_b)
    assert got_a.status_code == 200
    assert [alert["id"] for alert in got_a.json()["config"]["alerts"]] == ["aapl_drop"]
    assert got_b.status_code == 200
    assert [alert["id"] for alert in got_b.json()["config"]["alerts"]] == ["sibling-msft"]

    aapl = {(w["user_id"], w["alert_id"]) for w in list_watches_for_symbol("AAPL")}
    assert aapl == {(user_a, "aapl_drop")}
    assert [w["alert_id"] for w in list_watches_for_symbol("MSFT")] == ["sibling-msft"]
    assert {w["user_id"] for w in list_watches_for_symbol("MSFT")} == {user_b}


def test_hosted_run_poison_row_stays_idle_200_without_touching_sibling(
    client, multi_user_env
) -> None:
    """exists=True + unparseable JSON must idle like a missing config, not 500."""
    token_a, user_a = _register(client, "run-corrupt-a@example.com")
    token_b, _user_b = _register(client, "run-corrupt-b@example.com")
    headers_a = {"Authorization": f"Bearer {token_a}"}
    headers_b = {"Authorization": f"Bearer {token_b}"}

    sibling = client.put(
        "/api/alerts/config",
        headers=headers_b,
        json=_price_payload("sibling-msft", "MSFT"),
    )
    assert sibling.status_code == 200
    _poison_row(user_a)

    with patch(
        "src.alerts.market_snapshot.load_market_snapshot",
        return_value=("2026-06-09", {}, []),
    ) as mock_snapshot:
        with patch(
            "src.alerts.alert_worker.run_check_once",
            return_value={"triggered": 99, "message": None},
        ) as mock_global:
            poisoned = client.post("/api/alerts/run", headers=headers_a)
            sibling_run = client.post("/api/alerts/run", headers=headers_b)

    assert poisoned.status_code == 200
    assert poisoned.json()["triggered"] == 0
    assert poisoned.json()["message"] == "No active watches configured."
    assert sibling_run.status_code == 404
    assert "No market data available" in sibling_run.json()["detail"]
    mock_global.assert_not_called()
    mock_snapshot.assert_called_once_with(["MSFT"], fetch_missing_quotes=True)


def test_hosted_test_poison_row_404s_without_building_notifiers_or_touching_sibling(
    client, multi_user_env
) -> None:
    token_a, user_a = _register(client, "test-corrupt-a@example.com")
    token_b, _user_b = _register(client, "test-corrupt-b@example.com")
    headers_a = {"Authorization": f"Bearer {token_a}"}
    headers_b = {"Authorization": f"Bearer {token_b}"}

    sibling = client.put(
        "/api/alerts/config",
        headers=headers_b,
        json=_price_payload("sibling-msft", "MSFT"),
    )
    assert sibling.status_code == 200
    _poison_row(user_a)

    with patch(
        "src.cli.alerts_commands.AlertEngine._build_notifiers",
    ) as mock_build:
        poisoned = client.post(
            "/api/alerts/test",
            json={"id": "sibling-msft", "dry_run": True},
            headers=headers_a,
        )
        mock_build.assert_not_called()

    sibling_test = client.post(
        "/api/alerts/test",
        json={"id": "sibling-msft", "dry_run": True},
        headers=headers_b,
    )

    assert poisoned.status_code == 404
    assert poisoned.json()["detail"] == "No alerts config for this user."
    assert sibling_test.status_code == 200
    assert sibling_test.json()["alert_id"] == "sibling-msft"
    assert sibling_test.json()["status"] == "dry_run"

"""Force-init/PUT must clear stale watches after config_json is poisoned."""

import pytest
from fastapi.testclient import TestClient

from dashboard.backend.main import app
from src.storage.alert_watches import list_enabled_symbols, list_watches_for_symbol
from src.storage.database import get_connection, init_database


@pytest.fixture
def multi_user_env(tmp_path, monkeypatch):
    db_path = tmp_path / "desync-recover.db"
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


def _poison_config(user_id: str) -> None:
    with get_connection() as conn:
        updated = conn.execute(
            "UPDATE user_alert_configs SET config_json = ? WHERE user_id = ?",
            ("{not-json", user_id),
        )
        assert updated.rowcount == 1


def _watch_ids(symbol: str) -> set[tuple[str, str]]:
    return {(w["user_id"], w["alert_id"]) for w in list_watches_for_symbol(symbol)}


def test_force_init_after_save_then_poison_clears_stale_watches_without_touching_sibling(
    client,
) -> None:
    token_a, user_a = _register(client, "desync-init-a@example.com")
    token_b, user_b = _register(client, "desync-init-b@example.com")
    headers_a = {"Authorization": f"Bearer {token_a}"}
    headers_b = {"Authorization": f"Bearer {token_b}"}

    saved_a = client.put(
        "/api/alerts/config",
        headers=headers_a,
        json=_price_payload("aapl_drop", "AAPL"),
    )
    saved_b = client.put(
        "/api/alerts/config",
        headers=headers_b,
        json=_price_payload("sibling-msft", "MSFT"),
    )
    assert saved_a.status_code == 200
    assert saved_b.status_code == 200
    assert _watch_ids("AAPL") == {(user_a, "aapl_drop")}
    assert _watch_ids("MSFT") == {(user_b, "sibling-msft")}

    _poison_config(user_a)
    assert _watch_ids("AAPL") == {(user_a, "aapl_drop")}
    status = client.get("/api/alerts/status", headers=headers_a)
    assert status.status_code == 200
    assert status.json()["active_watches"] == 0

    conflict = client.post("/api/alerts/init", headers=headers_a)
    assert conflict.status_code == 409

    forced = client.post("/api/alerts/init?force=true", headers=headers_a)
    assert forced.status_code == 200
    recovered = client.get("/api/alerts/config", headers=headers_a)
    assert recovered.status_code == 200
    assert recovered.json()["config"]["alerts"] == []
    assert _watch_ids("AAPL") == set()
    assert "AAPL" not in list_enabled_symbols()
    assert _watch_ids("MSFT") == {(user_b, "sibling-msft")}
    assert list_enabled_symbols() == ["MSFT"]

    sibling = client.get("/api/alerts/config", headers=headers_b)
    assert sibling.status_code == 200
    assert [alert["id"] for alert in sibling.json()["config"]["alerts"]] == ["sibling-msft"]


def test_put_after_save_then_poison_replaces_stale_watches_without_touching_sibling(
    client,
) -> None:
    token_a, user_a = _register(client, "desync-put-a@example.com")
    token_b, user_b = _register(client, "desync-put-b@example.com")
    headers_a = {"Authorization": f"Bearer {token_a}"}
    headers_b = {"Authorization": f"Bearer {token_b}"}

    saved_a = client.put(
        "/api/alerts/config",
        headers=headers_a,
        json=_price_payload("aapl_drop", "AAPL"),
    )
    saved_b = client.put(
        "/api/alerts/config",
        headers=headers_b,
        json=_price_payload("sibling-msft", "MSFT"),
    )
    assert saved_a.status_code == 200
    assert saved_b.status_code == 200

    _poison_config(user_a)
    assert _watch_ids("AAPL") == {(user_a, "aapl_drop")}

    replaced = client.put(
        "/api/alerts/config",
        headers=headers_a,
        json=_price_payload("goog_drop", "GOOG"),
    )
    assert replaced.status_code == 200
    assert [alert["id"] for alert in replaced.json()["config"]["alerts"]] == ["goog_drop"]
    assert _watch_ids("AAPL") == set()
    assert _watch_ids("GOOG") == {(user_a, "goog_drop")}
    assert _watch_ids("MSFT") == {(user_b, "sibling-msft")}
    assert list_enabled_symbols() == ["GOOG", "MSFT"]

    sibling = client.get("/api/alerts/config", headers=headers_b)
    assert sibling.status_code == 200
    assert [alert["id"] for alert in sibling.json()["config"]["alerts"]] == ["sibling-msft"]

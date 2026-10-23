"""Hosted PUT must keep the first duplicate price-threshold key and leave siblings intact."""

import pytest
from fastapi.testclient import TestClient

from dashboard.backend.main import app
from src.storage.alert_watches import list_watches_for_symbol
from src.storage.database import init_database


@pytest.fixture
def multi_user_env(tmp_path, monkeypatch):
    db_path = tmp_path / "dup-price.db"
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


def _price_alert(alert_id: str, symbol: str, operator: str, value: float) -> dict:
    return {
        "id": alert_id,
        "name": alert_id,
        "enabled": True,
        "condition": {
            "type": "price_threshold",
            "symbol": symbol,
            "operator": operator,
            "value": value,
        },
        "notifications": ["log"],
    }


def test_hosted_put_keeps_first_duplicate_price_key_without_dropping_sibling(
    client, multi_user_env
) -> None:
    token_a, user_a = _register(client, "dup-price-a@example.com")
    token_b, user_b = _register(client, "dup-price-b@example.com")
    headers_a = {"Authorization": f"Bearer {token_a}"}
    headers_b = {"Authorization": f"Bearer {token_b}"}

    sibling = client.put(
        "/api/alerts/config",
        headers=headers_b,
        json={
            "defaults": {},
            "alerts": [_price_alert("aapl-high", "AAPL", "greater_than", 400)],
        },
    )
    assert sibling.status_code == 200

    saved = client.put(
        "/api/alerts/config",
        headers=headers_a,
        json={
            "defaults": {},
            "alerts": [
                _price_alert("aapl_drop", "AAPL", "less_than", 150),
                _price_alert("aapl_drop_copy", "AAPL", "less_than", 150),
                _price_alert("msft_low", "MSFT", "less_than", 300),
            ],
        },
    )
    assert saved.status_code == 200
    assert [alert["id"] for alert in saved.json()["config"]["alerts"]] == [
        "aapl_drop",
        "msft_low",
    ]

    got_a = client.get("/api/alerts/config", headers=headers_a)
    got_b = client.get("/api/alerts/config", headers=headers_b)
    assert got_a.status_code == 200
    assert [alert["id"] for alert in got_a.json()["config"]["alerts"]] == [
        "aapl_drop",
        "msft_low",
    ]
    assert got_b.status_code == 200
    assert [alert["id"] for alert in got_b.json()["config"]["alerts"]] == ["aapl-high"]

    aapl = {(w["user_id"], w["alert_id"]) for w in list_watches_for_symbol("AAPL")}
    assert aapl == {(user_a, "aapl_drop"), (user_b, "aapl-high")}
    assert [w["alert_id"] for w in list_watches_for_symbol("MSFT")] == ["msft_low"]

"""PUT /api/alerts/config must reject duplicate alert ids with HTTP 400."""

import pytest
from fastapi.testclient import TestClient

from dashboard.backend.main import app
from src.storage.database import init_database


@pytest.fixture
def client():
    return TestClient(app)


@pytest.fixture
def file_mode(tmp_path, monkeypatch):
    monkeypatch.delenv("MARKETHELM_DATABASE_URL", raising=False)
    config_path = tmp_path / "alerts.json"
    monkeypatch.setenv("MARKETHELM_ALERTS_CONFIG", str(config_path))
    return config_path


@pytest.fixture
def multi_user_env(tmp_path, monkeypatch):
    db_path = tmp_path / "dup-id.db"
    monkeypatch.setenv("MARKETHELM_DATABASE_URL", f"sqlite:///{db_path.as_posix()}")
    monkeypatch.setenv("MARKETHELM_AUTH_SECRET", "test-secret-min-16-chars")

    init_database()


def _register(client, email: str = "dup-id@example.com") -> str:
    r = client.post(
        "/api/auth/register",
        json={"email": email, "password": "password123"},
    )
    assert r.status_code == 200
    return r.json()["access_token"]


def _alert(alert_id: str, symbol: str = "AAPL"):
    return {
        "id": alert_id,
        "name": f"{alert_id} watch",
        "enabled": True,
        "condition": {
            "type": "price_threshold",
            "symbol": symbol,
            "operator": "less_than",
            "value": 150,
        },
        "notifications": ["log"],
    }


def _payload(*alert_ids):
    return {
        "defaults": {},
        "alerts": [_alert(aid, symbol=f"S{i}") for i, aid in enumerate(alert_ids)],
    }


def test_file_mode_put_rejects_duplicate_alert_ids(client, file_mode):
    response = client.put("/api/alerts/config", json=_payload("same", "same"))
    assert response.status_code == 400
    assert "duplicate" in response.json()["detail"].lower()
    assert not file_mode.exists()


def test_file_mode_put_rejects_duplicate_ids_after_strip(client, file_mode):
    response = client.put("/api/alerts/config", json=_payload("same", " same "))
    assert response.status_code == 400
    assert "duplicate" in response.json()["detail"].lower()
    assert not file_mode.exists()


def test_hosted_put_rejects_duplicate_alert_ids(client, multi_user_env):
    token = _register(client)
    headers = {"Authorization": f"Bearer {token}"}

    # Seed a valid config so we can assert failed updates preserve it.
    ok = client.put("/api/alerts/config", json=_payload("keep-me"), headers=headers)
    assert ok.status_code == 200
    assert ok.json()["config"]["alerts"][0]["id"] == "keep-me"

    response = client.put(
        "/api/alerts/config",
        json=_payload("same", " same "),
        headers=headers,
    )
    assert response.status_code == 400
    assert "duplicate" in response.json()["detail"].lower()

    saved = client.get("/api/alerts/config", headers=headers)
    assert saved.status_code == 200
    assert [a["id"] for a in saved.json()["config"]["alerts"]] == ["keep-me"]


def test_hosted_put_allows_distinct_ids(client, multi_user_env):
    token = _register(client, "dup-ok@example.com")
    headers = {"Authorization": f"Bearer {token}"}

    response = client.put(
        "/api/alerts/config",
        json=_payload("aapl-low", "msft-low"),
        headers=headers,
    )
    assert response.status_code == 200
    assert [a["id"] for a in response.json()["config"]["alerts"]] == [
        "aapl-low",
        "msft-low",
    ]

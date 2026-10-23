"""PUT /api/alerts/config must reject blank / whitespace-only alert ids."""

import json

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
    db_path = tmp_path / "blank-id.db"
    monkeypatch.setenv("MARKETHELM_DATABASE_URL", f"sqlite:///{db_path.as_posix()}")
    monkeypatch.setenv("MARKETHELM_AUTH_SECRET", "test-secret-min-16-chars")

    init_database()


def _register(client, email: str = "blank-id@example.com") -> str:
    r = client.post(
        "/api/auth/register",
        json={"email": email, "password": "password123"},
    )
    assert r.status_code == 200
    return r.json()["access_token"]


def _payload(alert_id):
    return {
        "defaults": {},
        "alerts": [
            {
                "id": alert_id,
                "name": "blank id",
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


@pytest.mark.parametrize("alert_id", ["", "   ", "\t"])
def test_file_mode_put_rejects_blank_alert_id(client, file_mode, alert_id):
    response = client.put("/api/alerts/config", json=_payload(alert_id))
    assert response.status_code == 400
    assert "id" in response.json()["detail"].lower()
    assert not file_mode.exists()


@pytest.mark.parametrize(
    ("alert_id", "email"),
    [
        ("", "blank-empty@example.com"),
        ("   ", "blank-spaces@example.com"),
        ("\t", "blank-tab@example.com"),
    ],
)
def test_hosted_put_rejects_blank_alert_id(client, multi_user_env, alert_id, email):
    token = _register(client, email)
    headers = {"Authorization": f"Bearer {token}"}

    response = client.put("/api/alerts/config", json=_payload(alert_id), headers=headers)
    assert response.status_code == 400
    assert "id" in response.json()["detail"].lower()

    saved = client.get("/api/alerts/config", headers=headers)
    assert saved.status_code == 200
    assert saved.json()["config"]["alerts"] == []


def test_file_mode_put_strips_alert_id_whitespace(client, file_mode):
    response = client.put("/api/alerts/config", json=_payload("  watch-aapl  "))
    assert response.status_code == 200
    assert response.json()["config"]["alerts"][0]["id"] == "watch-aapl"
    on_disk = json.loads(file_mode.read_text())
    assert on_disk["alerts"][0]["id"] == "watch-aapl"

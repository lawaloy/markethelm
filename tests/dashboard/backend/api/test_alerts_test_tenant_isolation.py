"""Hosted /api/alerts/test must not fire another tenant's alert id."""

from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from dashboard.backend.main import app
from src.storage.database import init_database


@pytest.fixture
def multi_user_env(tmp_path, monkeypatch):
    db_path = tmp_path / "test-tenant.db"
    monkeypatch.setenv("MARKETHELM_DATABASE_URL", f"sqlite:///{db_path.as_posix()}")
    monkeypatch.setenv("MARKETHELM_AUTH_SECRET", "test-secret-min-16-chars")

    init_database()


@pytest.fixture
def client():
    return TestClient(app)


def _register(client, email: str) -> str:
    response = client.post(
        "/api/auth/register",
        json={"email": email, "password": "password123"},
    )
    assert response.status_code == 200
    return response.json()["access_token"]


def _price_payload(alert_id: str, symbol: str, email_to: str) -> dict:
    return {
        "defaults": {"email_to": email_to, "notify_email": True},
        "alerts": [
            {
                "id": alert_id,
                "name": alert_id,
                "enabled": True,
                "notifications": ["email"],
                "condition": {
                    "type": "price_threshold",
                    "symbol": symbol,
                    "operator": "less_than",
                    "value": 200,
                },
            }
        ],
    }


def test_hosted_test_cannot_fire_sibling_tenant_alert_id(client, multi_user_env):
    headers_a = {"Authorization": f"Bearer {_register(client, 'tenant-a@example.com')}"}
    headers_b = {"Authorization": f"Bearer {_register(client, 'tenant-b@example.com')}"}

    assert client.post("/api/alerts/init", headers=headers_a).status_code == 200
    assert client.post("/api/alerts/init", headers=headers_b).status_code == 200
    assert (
        client.put(
            "/api/alerts/config",
            json=_price_payload("price_watch", "AAPL", "a@example.com"),
            headers=headers_a,
        ).status_code
        == 200
    )
    assert (
        client.put(
            "/api/alerts/config",
            json=_price_payload("msft_watch", "MSFT", "b@example.com"),
            headers=headers_b,
        ).status_code
        == 200
    )

    class EmailNotifier:
        def send(self, _event):
            return True

    with patch(
        "src.cli.alerts_commands.AlertEngine._build_notifiers",
        return_value=[EmailNotifier()],
    ) as mock_build:
        stolen = client.post(
            "/api/alerts/test",
            json={"id": "price_watch", "dry_run": False},
            headers=headers_b,
        )

    assert stolen.status_code == 404
    assert "price_watch" in stolen.json()["detail"]
    mock_build.assert_not_called()

    status_a = client.get("/api/alerts/status", headers=headers_a)
    status_b = client.get("/api/alerts/status", headers=headers_b)
    assert status_a.status_code == 200
    assert status_b.status_code == 200
    assert status_a.json()["latest_deliveries"] == []
    assert status_b.json()["latest_deliveries"] == []

"""Authenticated password change and account deletion API coverage."""

import pytest
from fastapi.testclient import TestClient

from dashboard.backend.main import app
from src.storage.alert_watches import sync_watches_from_config
from src.storage.database import get_connection, init_database
from src.storage.user_alerts import save_user_alerts_config


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv(
        "MARKETHELM_DATABASE_URL", f"sqlite:///{(tmp_path / 'account.db').as_posix()}"
    )
    monkeypatch.setenv("MARKETHELM_AUTH_SECRET", "test-secret-min-16-chars")

    init_database()

    return TestClient(app)


def _register(client, email="account@example.com"):
    response = client.post("/api/auth/register", json={"email": email, "password": "password123"})
    assert response.status_code == 200
    return response.json()


def test_change_password_requires_current_password_and_revokes_sessions(client):
    registered = _register(client)
    headers = {"Authorization": f"Bearer {registered['access_token']}"}
    wrong = client.post(
        "/api/auth/password/change",
        headers=headers,
        json={"current_password": "wrong-password", "new_password": "new-password-123"},
    )
    assert wrong.status_code == 400
    assert client.get("/api/auth/me", headers=headers).status_code == 200

    changed = client.post(
        "/api/auth/password/change",
        headers=headers,
        json={"current_password": "password123", "new_password": "new-password-123"},
    )
    assert changed.status_code == 200
    assert client.get("/api/auth/me", headers=headers).status_code == 401
    assert (
        client.post(
            "/api/auth/login", json={"email": "account@example.com", "password": "password123"}
        ).status_code
        == 401
    )
    assert (
        client.post(
            "/api/auth/login", json={"email": "account@example.com", "password": "new-password-123"}
        ).status_code
        == 200
    )


def test_change_password_rejects_reuse(client):
    registered = _register(client)
    response = client.post(
        "/api/auth/password/change",
        headers={"Authorization": f"Bearer {registered['access_token']}"},
        json={"current_password": "password123", "new_password": "password123"},
    )
    assert response.status_code == 400
    assert "different" in response.json()["detail"]


def test_delete_account_requires_confirmation_and_cascades_tenant_data(client):
    registered = _register(client, "delete@example.com")
    user_id = registered["user"]["id"]
    headers = {"Authorization": f"Bearer {registered['access_token']}"}
    config = {
        "defaults": {},
        "alerts": [
            {
                "id": "delete-watch",
                "enabled": True,
                "condition": {
                    "type": "price_threshold",
                    "symbol": "AAPL",
                    "operator": "greater_than",
                    "value": 1,
                },
            }
        ],
    }
    save_user_alerts_config(user_id, config)
    sync_watches_from_config(user_id, config)

    rejected = client.request(
        "DELETE",
        "/api/auth/account",
        headers=headers,
        json={"current_password": "password123", "confirmation": "delete"},
    )
    assert rejected.status_code == 400
    assert client.get("/api/auth/me", headers=headers).status_code == 200

    deleted = client.request(
        "DELETE",
        "/api/auth/account",
        headers=headers,
        json={"current_password": "password123", "confirmation": "DELETE"},
    )
    assert deleted.status_code == 200
    assert client.get("/api/auth/me", headers=headers).status_code == 401
    with get_connection() as conn:
        for table in ("users", "user_alert_configs", "alert_watches", "account_tokens"):
            column = "id" if table == "users" else "user_id"
            count = conn.execute(
                f"SELECT COUNT(*) AS n FROM {table} WHERE {column} = ?", (user_id,)
            ).fetchone()["n"]
            assert count == 0


def test_delete_account_rejects_wrong_password(client):
    registered = _register(client)
    response = client.request(
        "DELETE",
        "/api/auth/account",
        headers={"Authorization": f"Bearer {registered['access_token']}"},
        json={"current_password": "wrong-password", "confirmation": "DELETE"},
    )
    assert response.status_code == 400
    assert response.json()["detail"] == "Current password is incorrect."

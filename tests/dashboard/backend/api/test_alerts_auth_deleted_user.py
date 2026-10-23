"""Deleted-user tokens must not authorize hosted alert routes."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from dashboard.backend.main import app
from src.storage.database import get_connection, init_database


@pytest.fixture
def multi_user_env(tmp_path, monkeypatch):
    db_path = tmp_path / "deleted-user-alerts.db"
    monkeypatch.setenv("MARKETHELM_DATABASE_URL", f"sqlite:///{db_path.as_posix()}")
    monkeypatch.setenv("MARKETHELM_AUTH_SECRET", "test-secret-min-16-chars")

    init_database()


@pytest.fixture
def client():
    return TestClient(app)


def _register(client, email: str = "gone@example.com") -> tuple[str, str]:
    r = client.post(
        "/api/auth/register",
        json={"email": email, "password": "password123"},
    )
    assert r.status_code == 200
    body = r.json()
    return body["access_token"], body["user"]["id"]


def _delete_user(user_id: str) -> None:
    with get_connection() as conn:
        conn.execute("DELETE FROM users WHERE id = ?", (user_id,))


@pytest.mark.parametrize(
    "method,path,kwargs",
    [
        ("get", "/api/alerts/config", {}),
        ("post", "/api/alerts/init", {}),
        (
            "put",
            "/api/alerts/config",
            {
                "json": {
                    "defaults": {},
                    "alerts": [],
                }
            },
        ),
        ("get", "/api/alerts/status", {}),
    ],
)
def test_alert_routes_reject_token_for_deleted_user(
    client, multi_user_env, method, path, kwargs
) -> None:
    token, user_id = _register(client)
    headers = {"Authorization": f"Bearer {token}"}

    # Seed so a stale-token GET would otherwise return empty config (200) rather
    # than 401 if require_user_id only checked signature.
    seeded = client.post("/api/alerts/init", headers=headers)
    assert seeded.status_code == 200

    _delete_user(user_id)

    r = getattr(client, method)(path, headers=headers, **kwargs)
    assert r.status_code == 401
    assert r.json()["detail"] == "User not found."


def test_deleted_user_token_cannot_put_config(client, multi_user_env) -> None:
    token, user_id = _register(client, email="put-gone@example.com")
    headers = {"Authorization": f"Bearer {token}"}
    assert client.post("/api/alerts/init", headers=headers).status_code == 200
    _delete_user(user_id)

    r = client.put(
        "/api/alerts/config",
        headers=headers,
        json={
            "defaults": {"email_to": "x@example.com"},
            "alerts": [
                {
                    "id": "watch",
                    "enabled": True,
                    "condition": {
                        "type": "price_threshold",
                        "symbol": "AAPL",
                        "operator": "less_than",
                        "value": 100,
                    },
                }
            ],
        },
    )
    assert r.status_code == 401
    assert r.json()["detail"] == "User not found."

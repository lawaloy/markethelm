"""Hosted GET must skip non-dict alerts items instead of 500ing Settings.

``polish_alerts_config`` drops non-dict rows, then ``_channel_status`` calls
``alert.get("email_to")`` on whatever remains. A hand-edited ``alerts`` list
that still contains a valid rule plus poison items used to be untested: dropping
the polish skip would TypeError on GET ``/config`` while a sibling tenant looks
fine. Status already guards ``isinstance``; config GET did not have an HTTP lock.
"""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from dashboard.backend.main import app
from src.storage.database import get_connection, init_database


@pytest.fixture
def multi_user_env(tmp_path, monkeypatch):
    db_path = tmp_path / "hosted-mixed-items.db"
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


def _rule(*, alert_id: str, symbol: str, email_to: str | None = None) -> dict:
    alert = {
        "id": alert_id,
        "name": alert_id,
        "enabled": True,
        "condition": {
            "type": "price_threshold",
            "symbol": symbol,
            "operator": "less_than",
            "value": 100,
        },
        "notifications": ["log", "email"] if email_to else ["log"],
    }
    if email_to is not None:
        alert["email_to"] = email_to
    return alert


def test_hosted_get_config_skips_non_dict_alert_items_without_touching_sibling(
    client, multi_user_env
) -> None:
    """A valid rule must still flip email_recipients after poison list items drop."""
    token_a, user_a = _register(client, "mixed-items-a@example.com")
    token_b, _user_b = _register(client, "mixed-items-b@example.com")
    headers_a = {"Authorization": f"Bearer {token_a}"}
    headers_b = {"Authorization": f"Bearer {token_b}"}

    saved = client.put(
        "/api/alerts/config",
        headers=headers_b,
        json={
            "defaults": {},
            "alerts": [_rule(alert_id="sibling-msft", symbol="MSFT")],
        },
    )
    assert saved.status_code == 200

    valid = _rule(
        alert_id="keep-aapl",
        symbol="AAPL",
        email_to="ops-a@example.com",
    )

    with get_connection() as conn:
        conn.execute(
            """
            INSERT INTO user_alert_configs (user_id, config_json, updated_at)
            VALUES (?, ?, ?)
            """,
            (
                user_a,
                json.dumps({"defaults": {}, "alerts": [valid, 1, "poison", None]}),
                "2026-07-24T00:00:00+00:00",
            ),
        )

    poisoned = client.get("/api/alerts/config", headers=headers_a)
    sibling = client.get("/api/alerts/config", headers=headers_b)

    assert poisoned.status_code == 200
    body = poisoned.json()
    assert body["exists"] is True
    assert [alert["id"] for alert in body["config"]["alerts"]] == ["keep-aapl"]
    assert body["config"]["alerts"][0].get("email_to") == "ops-a@example.com"
    assert body["channels"]["email_recipients"] is True
    assert body["channels"]["webhook_url"] is False

    assert sibling.status_code == 200
    assert sibling.json()["channels"]["email_recipients"] is False
    assert [alert["id"] for alert in sibling.json()["config"]["alerts"]] == ["sibling-msft"]
    assert "ops-a@example.com" not in json.dumps(sibling.json())

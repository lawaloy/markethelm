"""Hosted alerts GET paths must skip non-dict stored rows without dropping siblings."""

from __future__ import annotations

import json
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

from dashboard.backend.main import app
from src.storage.database import get_connection, init_database


@pytest.fixture
def multi_user_env(tmp_path, monkeypatch):
    db_path = tmp_path / "hosted-nondict.db"
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


def _price_payload(alert_id: str, symbol: str = "AAPL") -> dict:
    return {
        "defaults": {"email_to": f"{alert_id}@example.com"},
        "alerts": [
            {
                "id": alert_id,
                "name": alert_id,
                "enabled": True,
                "condition": {
                    "type": "price_threshold",
                    "symbol": symbol,
                    "operator": "less_than",
                    "value": 100,
                },
                "notifications": ["log"],
            }
        ],
    }


def _poison_blob() -> str:
    return json.dumps(
        {
            "defaults": {"email_to": "poison@example.com"},
            "alerts": [
                "junk",
                None,
                42,
                {
                    "id": "keep_on",
                    "name": "Keep on",
                    "enabled": True,
                    "condition": {
                        "type": "price_threshold",
                        "symbol": "AAPL",
                        "operator": "less_than",
                        "value": 100,
                    },
                },
                {
                    "id": "keep_off",
                    "name": "Keep off",
                    "enabled": False,
                },
            ],
        }
    )


def test_hosted_get_config_skips_nondict_rows_without_dropping_valid_rules(
    client, multi_user_env
) -> None:
    token_a, user_a = _register(client, "nondict-a@example.com")
    token_b, _user_b = _register(client, "nondict-b@example.com")
    headers_a = {"Authorization": f"Bearer {token_a}"}
    headers_b = {"Authorization": f"Bearer {token_b}"}

    saved = client.put(
        "/api/alerts/config",
        headers=headers_b,
        json=_price_payload("sibling-msft", "MSFT"),
    )
    assert saved.status_code == 200

    with get_connection() as conn:
        conn.execute(
            """
            INSERT INTO user_alert_configs (user_id, config_json, updated_at)
            VALUES (?, ?, ?)
            """,
            (user_a, _poison_blob(), "2026-07-24T00:00:00+00:00"),
        )

    poisoned = client.get("/api/alerts/config", headers=headers_a)
    sibling = client.get("/api/alerts/config", headers=headers_b)

    assert poisoned.status_code == 200
    assert [alert["id"] for alert in poisoned.json()["config"]["alerts"]] == [
        "keep_on",
        "keep_off",
    ]
    assert sibling.status_code == 200
    assert [alert["id"] for alert in sibling.json()["config"]["alerts"]] == [
        "sibling-msft",
    ]


def test_hosted_status_skips_nondict_rows_without_dropping_sibling_tenant(
    client, multi_user_env, monkeypatch
) -> None:
    token_a, user_a = _register(client, "status-a@example.com")
    token_b, _user_b = _register(client, "status-b@example.com")
    headers_a = {"Authorization": f"Bearer {token_a}"}
    headers_b = {"Authorization": f"Bearer {token_b}"}

    saved = client.put(
        "/api/alerts/config",
        headers=headers_b,
        json=_price_payload("sibling-msft", "MSFT"),
    )
    assert saved.status_code == 200

    with get_connection() as conn:
        conn.execute(
            """
            INSERT INTO user_alert_configs (user_id, config_json, updated_at)
            VALUES (?, ?, ?)
            """,
            (user_a, _poison_blob(), "2026-07-24T00:00:00+00:00"),
        )

    monkeypatch.setattr(
        "dashboard.backend.api.alerts.get_data_loader",
        lambda: MagicMock(
            get_latest_date=MagicMock(return_value=None),
            load_projections=MagicMock(return_value=MagicMock(empty=True)),
        ),
    )

    poisoned = client.get("/api/alerts/status", headers=headers_a)
    sibling = client.get("/api/alerts/status", headers=headers_b)

    assert poisoned.status_code == 200
    assert poisoned.json()["active_watches"] == 1
    assert sibling.status_code == 200
    assert sibling.json()["active_watches"] == 1

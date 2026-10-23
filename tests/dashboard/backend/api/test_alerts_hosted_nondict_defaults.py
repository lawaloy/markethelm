"""Hosted GET must soft-fail truthy non-dict ``defaults`` instead of 500ing Settings.

``polish_alerts_config`` replaces a list/string/number ``defaults`` with ``{}``,
then ``_normalize_config`` / ``_channel_status`` call ``dict`` / ``.get`` on it.
A hand-edited config object whose ``defaults`` is not a mapping used to be
untested at the HTTP layer: dropping the polish guard would TypeError on GET
``/config`` while a sibling tenant looks healthy. Non-object *rows* are already
locked; this is a valid object with a poison ``defaults`` key.
"""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from dashboard.backend.main import app
from src.storage.database import get_connection, init_database


@pytest.fixture
def multi_user_env(tmp_path, monkeypatch):
    db_path = tmp_path / "hosted-nondict-defaults.db"
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


@pytest.mark.parametrize("bad_defaults", [["x"], "ops@example.com", 1, True])
def test_hosted_get_config_soft_fails_nondict_defaults_without_touching_sibling(
    client, multi_user_env, bad_defaults
) -> None:
    """Poison defaults must empty to {} and must not fake email_recipients."""
    suffix = type(bad_defaults).__name__
    token_a, user_a = _register(client, f"defaults-a-{suffix}@example.com")
    token_b, _user_b = _register(client, f"defaults-b-{suffix}@example.com")
    headers_a = {"Authorization": f"Bearer {token_a}"}
    headers_b = {"Authorization": f"Bearer {token_b}"}

    saved = client.put(
        "/api/alerts/config",
        headers=headers_b,
        json={
            "defaults": {},
            "alerts": [
                _rule(
                    alert_id="sibling-msft",
                    symbol="MSFT",
                    email_to="ops-b@example.com",
                )
            ],
        },
    )
    assert saved.status_code == 200

    with get_connection() as conn:
        conn.execute(
            """
            INSERT INTO user_alert_configs (user_id, config_json, updated_at)
            VALUES (?, ?, ?)
            """,
            (
                user_a,
                json.dumps(
                    {
                        "defaults": bad_defaults,
                        "alerts": [_rule(alert_id="keep-aapl", symbol="AAPL")],
                    }
                ),
                "2026-07-24T00:00:00+00:00",
            ),
        )

    poisoned = client.get("/api/alerts/config", headers=headers_a)
    sibling = client.get("/api/alerts/config", headers=headers_b)

    assert poisoned.status_code == 200
    body = poisoned.json()
    assert body["exists"] is True
    defaults = body["config"]["defaults"]
    assert defaults.get("email_to") in (None, "")
    assert defaults.get("webhook_url") in (None, "")
    assert defaults.get("webhook_format") in (None, "")
    assert [alert["id"] for alert in body["config"]["alerts"]] == ["keep-aapl"]
    assert body["channels"]["email_recipients"] is False
    assert body["channels"]["webhook_url"] is False
    assert "ops-b@example.com" not in json.dumps(body)
    assert "ops@example.com" not in json.dumps(body)

    assert sibling.status_code == 200
    sibling_body = sibling.json()
    assert sibling_body["channels"]["email_recipients"] is True
    assert [alert["id"] for alert in sibling_body["config"]["alerts"]] == ["sibling-msft"]
    assert sibling_body["config"]["alerts"][0].get("email_to") == "ops-b@example.com"

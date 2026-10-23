"""Hosted GET must not treat bundled placeholder rule mailboxes as recipients.

``polish_alerts_config`` drops ``you@example.com`` from rules, then
``_channel_status`` ORs remaining ``email_to`` into ``channels.email_recipients``.
A hand-copied example rule used to be untested on Settings GET: skipping that
strip would look email-ready and leak the placeholder into JSON, while a sibling
with a real mailbox must stay isolated. PUT already polishes before persist;
GET of a stored example row is the remaining HTTP lock.
"""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from dashboard.backend.main import app
from src.storage.database import get_connection, init_database


@pytest.fixture
def multi_user_env(tmp_path, monkeypatch):
    db_path = tmp_path / "hosted-placeholder-email.db"
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


def test_hosted_get_config_strips_placeholder_rule_email_without_touching_sibling(
    client, multi_user_env
) -> None:
    """Stored you@example.com must not flip email_recipients or leak to a sibling."""
    token_a, user_a = _register(client, "placeholder-email-a@example.org")
    token_b, _user_b = _register(client, "placeholder-email-b@example.org")
    headers_a = {"Authorization": f"Bearer {token_a}"}
    headers_b = {"Authorization": f"Bearer {token_b}"}

    sibling = client.put(
        "/api/alerts/config",
        headers=headers_b,
        json={
            "defaults": {},
            "alerts": [
                _rule(
                    alert_id="sibling-msft",
                    symbol="MSFT",
                    email_to="ops-b@example.org",
                )
            ],
        },
    )
    assert sibling.status_code == 200
    assert sibling.json()["channels"]["email_recipients"] is True

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
                        "defaults": {},
                        "alerts": [
                            _rule(
                                alert_id="example-aapl",
                                symbol="AAPL",
                                email_to="you@example.com",
                            )
                        ],
                    }
                ),
                "2026-07-24T00:00:00+00:00",
            ),
        )

    poisoned = client.get("/api/alerts/config", headers=headers_a)
    sibling_got = client.get("/api/alerts/config", headers=headers_b)

    assert poisoned.status_code == 200
    body = poisoned.json()
    assert body["exists"] is True
    assert [alert["id"] for alert in body["config"]["alerts"]] == ["example-aapl"]
    assert not str(body["config"]["alerts"][0].get("email_to") or "").strip()
    assert body["channels"]["email_recipients"] is False
    assert body["channels"]["webhook_url"] is False
    assert "you@example.com" not in json.dumps(body)
    assert "ops-b@example.org" not in json.dumps(body)

    assert sibling_got.status_code == 200
    assert sibling_got.json()["channels"]["email_recipients"] is True
    assert sibling_got.json()["config"]["alerts"][0]["email_to"] == "ops-b@example.org"
    assert [alert["id"] for alert in sibling_got.json()["config"]["alerts"]] == ["sibling-msft"]
    assert "you@example.com" not in json.dumps(sibling_got.json())

"""Hosted GET must not treat bundled placeholder rule webhooks as channel-ready.

``polish_alerts_config`` drops per-rule ``example.com`` / ``your/webhook`` URLs,
then ``_has_webhook_secret`` ORs remaining rule URLs into ``channels.webhook_url``.
PUT already polishes defaults placeholders before persist; a hand-copied example
rule used to be untested on Settings GET. Skipping that strip would look
webhook-ready while a sibling with a real URL must stay isolated. Public JSON
already redacts webhook URLs, so the remaining lock is the readiness flag.
"""

from __future__ import annotations

import json
from urllib.parse import urlparse

import pytest
from fastapi.testclient import TestClient

from dashboard.backend.main import app
from src.storage.database import get_connection, init_database
from src.storage.user_alerts import load_user_alerts_config


@pytest.fixture
def multi_user_env(tmp_path, monkeypatch):
    db_path = tmp_path / "hosted-placeholder-webhook.db"
    monkeypatch.setenv("MARKETHELM_DATABASE_URL", f"sqlite:///{db_path.as_posix()}")
    monkeypatch.setenv("MARKETHELM_AUTH_SECRET", "test-secret-min-16-chars")
    monkeypatch.setenv("ALERT_WEBHOOK_URL", "https://hooks.example/global")
    monkeypatch.setenv("DISCORD_WEBHOOK_URL", "https://discord.com/api/webhooks/global/token")

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


def _rule(*, alert_id: str, symbol: str, webhook_url: str | None = None) -> dict:
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
        "notifications": ["log", "webhook"] if webhook_url else ["log"],
    }
    if webhook_url is not None:
        alert["webhook_url"] = webhook_url
    return alert


def test_hosted_get_config_strips_placeholder_rule_webhook_without_touching_sibling(
    client, multi_user_env
) -> None:
    """Stored hooks.example.com must not flip webhook_url or mark a sibling unready."""
    token_a, user_a = _register(client, "placeholder-hook-a@example.org")
    token_b, _user_b = _register(client, "placeholder-hook-b@example.org")
    headers_a = {"Authorization": f"Bearer {token_a}"}
    headers_b = {"Authorization": f"Bearer {token_b}"}
    sibling_url = "https://hooks.example/sibling-rule"

    sibling = client.put(
        "/api/alerts/config",
        headers=headers_b,
        json={
            "defaults": {},
            "alerts": [
                _rule(
                    alert_id="sibling-msft",
                    symbol="MSFT",
                    webhook_url=sibling_url,
                )
            ],
        },
    )
    assert sibling.status_code == 200
    assert sibling.json()["channels"]["webhook_url"] is True
    assert "sibling-rule" not in json.dumps(sibling.json())

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
                                webhook_url=("https://hooks.example.com/services/T00/B00/xxx"),
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
    assert "webhook_url" not in body["config"]["alerts"][0]
    assert body["channels"]["webhook_url"] is False
    assert body["channels"]["email_recipients"] is False
    assert "hooks.example.com" not in json.dumps(body)
    assert "T00/B00/xxx" not in json.dumps(body)
    assert "sibling-rule" not in json.dumps(body)
    assert "hooks.example/global" not in json.dumps(body)
    assert "global/token" not in json.dumps(body)

    _, raw_b = load_user_alerts_config(_user_b)
    assert raw_b is not None
    assert urlparse(raw_b["alerts"][0]["webhook_url"]).hostname == "hooks.example"

    assert sibling_got.status_code == 200
    assert sibling_got.json()["channels"]["webhook_url"] is True
    assert sibling_got.json()["channels"]["email_recipients"] is False
    assert [alert["id"] for alert in sibling_got.json()["config"]["alerts"]] == ["sibling-msft"]
    assert "hooks.example.com" not in json.dumps(sibling_got.json())
    assert "T00/B00/xxx" not in json.dumps(sibling_got.json())
    assert "sibling-rule" not in json.dumps(sibling_got.json())

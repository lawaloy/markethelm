"""Hosted GET must not treat stored placeholder defaults webhooks as channel-ready.

``_has_webhook_secret`` checks ``defaults.webhook_url`` before per-rule URLs.
PUT already polishes placeholders before persist (see
``test_alerts_defaults_placeholder_webhook``); a hand-copied example
``defaults`` URL used to be untested on Settings GET. Skipping that strip
would look webhook-ready with empty ``alerts`` while a sibling with a real
defaults URL must stay isolated. Public JSON already redacts webhook URLs,
so the remaining lock is the readiness flag. Process-wide webhook env must
not flip either tenant. Per-rule placeholders are locked separately.
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
    db_path = tmp_path / "hosted-placeholder-defaults-webhook.db"
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


def test_hosted_get_config_strips_placeholder_defaults_webhook_without_touching_sibling(
    client, multi_user_env
) -> None:
    """Stored hooks.example.com defaults must not flip webhook_url or a sibling."""

    token_a, user_a = _register(client, "placeholder-defaults-a@example.org")
    token_b, user_b = _register(client, "placeholder-defaults-b@example.org")
    headers_a = {"Authorization": f"Bearer {token_a}"}
    headers_b = {"Authorization": f"Bearer {token_b}"}
    sibling_url = "https://hooks.example/sibling-defaults"
    placeholder = "https://hooks.example.com/services/T00/B00/xxx"

    sibling = client.put(
        "/api/alerts/config",
        headers=headers_b,
        json={"defaults": {"webhook_url": sibling_url}, "alerts": []},
    )
    assert sibling.status_code == 200
    assert sibling.json()["channels"]["webhook_url"] is True
    assert sibling.json()["channels"]["email_recipients"] is False
    assert sibling.json()["config"]["alerts"] == []
    assert not str(sibling.json()["config"]["defaults"].get("webhook_url") or "").strip()
    assert "sibling-defaults" not in json.dumps(sibling.json())

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
                        "defaults": {
                            "webhook_url": placeholder,
                            "webhook_format": "slack",
                        },
                        "alerts": [],
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
    assert body["config"]["alerts"] == []
    assert not str(body["config"]["defaults"].get("webhook_url") or "").strip()
    assert body["config"]["defaults"].get("webhook_format") == "slack"
    assert body["channels"]["webhook_url"] is False
    assert body["channels"]["email_recipients"] is False
    assert "hooks.example.com" not in json.dumps(body)
    assert "T00/B00/xxx" not in json.dumps(body)
    assert "sibling-defaults" not in json.dumps(body)
    assert "hooks.example/global" not in json.dumps(body)
    assert "global/token" not in json.dumps(body)

    _, raw_b = load_user_alerts_config(user_b)
    assert raw_b is not None
    assert urlparse(raw_b["defaults"]["webhook_url"]).hostname == "hooks.example"
    assert raw_b["alerts"] == []

    assert sibling_got.status_code == 200
    assert sibling_got.json()["exists"] is True
    assert sibling_got.json()["channels"]["webhook_url"] is True
    assert sibling_got.json()["channels"]["email_recipients"] is False
    assert sibling_got.json()["config"]["alerts"] == []
    assert "hooks.example.com" not in json.dumps(sibling_got.json())
    assert "T00/B00/xxx" not in json.dumps(sibling_got.json())
    assert "sibling-defaults" not in json.dumps(sibling_got.json())
    assert "hooks.example/global" not in json.dumps(sibling_got.json())
    assert "global/token" not in json.dumps(sibling_got.json())

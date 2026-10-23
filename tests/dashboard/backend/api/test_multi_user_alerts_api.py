"""Tests for per-user alerts API when MARKETHELM_DATABASE_URL is set."""

import json
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from dashboard.backend.main import app
from src.alerts.user_alert_storage import UserAlertStorage
from src.storage.alert_watches import list_enabled_symbols, list_watches_for_symbol
from src.storage.database import get_connection, init_database
from src.storage.user_alerts import load_user_alerts_config
from src.storage.users import authenticate_user


@pytest.fixture
def multi_user_env(tmp_path, monkeypatch):
    db_path = tmp_path / "markethelm.db"
    monkeypatch.setenv("MARKETHELM_DATABASE_URL", f"sqlite:///{db_path.as_posix()}")
    monkeypatch.setenv("MARKETHELM_AUTH_SECRET", "test-secret-min-16-chars")

    init_database()


@pytest.fixture
def client():
    return TestClient(app)


def _register(client, email: str = "alerts@example.com") -> str:
    r = client.post(
        "/api/auth/register",
        json={"email": email, "password": "password123"},
    )
    assert r.status_code == 200
    return r.json()["access_token"]


class TestMultiUserAlertsAPI:
    def test_config_requires_auth(self, client, multi_user_env):
        r = client.get("/api/alerts/config")
        assert r.status_code == 401

    def test_init_put_and_get_config(self, client, multi_user_env):
        token = _register(client)
        headers = {"Authorization": f"Bearer {token}"}

        r = client.post("/api/alerts/init", headers=headers)
        assert r.status_code == 200

        payload = {
            "defaults": {"email_to": "user@example.com"},
            "alerts": [
                {
                    "id": "watch_aapl",
                    "name": "AAPL watch",
                    "enabled": True,
                    "condition": {
                        "type": "price_threshold",
                        "symbol": "AAPL",
                        "operator": "below",
                        "value": 100,
                    },
                }
            ],
        }
        r2 = client.put("/api/alerts/config", json=payload, headers=headers)
        assert r2.status_code == 200
        assert r2.json()["exists"] is True
        assert r2.json()["config"]["defaults"]["email_to"] == "user@example.com"

        r3 = client.get("/api/alerts/config", headers=headers)
        assert r3.status_code == 200
        assert len(r3.json()["config"]["alerts"]) == 1

    def test_invalid_price_rule_is_rejected_without_persisting(self, client, multi_user_env):
        token = _register(client)
        headers = {"Authorization": f"Bearer {token}"}
        payload = {
            "defaults": {"email_to": "user@example.com"},
            "alerts": [
                {
                    "id": "bad-price",
                    "enabled": True,
                    "condition": {
                        "type": "price_threshold",
                        "symbol": "AAPL",
                        "operator": "below",
                        "value": "not-a-number",
                    },
                }
            ],
        }

        response = client.put("/api/alerts/config", json=payload, headers=headers)

        assert response.status_code == 400
        assert "invalid price threshold" in response.json()["detail"]
        saved = client.get("/api/alerts/config", headers=headers)
        assert saved.status_code == 200
        assert saved.json()["exists"] is False
        assert saved.json()["config"]["alerts"] == []

    def test_invalid_update_preserves_existing_config_and_watch_index(self, client, multi_user_env):
        token = _register(client)
        headers = {"Authorization": f"Bearer {token}"}
        valid_payload = {
            "defaults": {"email_to": "user@example.com"},
            "alerts": [
                {
                    "id": "watch_aapl",
                    "enabled": True,
                    "condition": {
                        "type": "price_threshold",
                        "symbol": "AAPL",
                        "operator": "less_than",
                        "value": 100,
                    },
                }
            ],
        }
        saved = client.put("/api/alerts/config", json=valid_payload, headers=headers)
        assert saved.status_code == 200
        assert list_enabled_symbols() == ["AAPL"]

        invalid_payload = json.loads(json.dumps(valid_payload))
        invalid_payload["alerts"][0]["condition"]["value"] = "not-a-number"
        rejected = client.put("/api/alerts/config", json=invalid_payload, headers=headers)

        assert rejected.status_code == 400
        current = client.get("/api/alerts/config", headers=headers).json()
        assert current["config"]["alerts"][0]["condition"]["value"] == 100
        assert list_enabled_symbols() == ["AAPL"]
        indexed = list_watches_for_symbol("AAPL")
        assert len(indexed) == 1
        assert indexed[0]["alert_id"] == "watch_aapl"
        assert indexed[0]["threshold"] == 100

    def test_users_have_isolated_configs(self, client, multi_user_env):
        token_a = _register(client, "a@example.com")
        token_b = _register(client, "b@example.com")

        client.post("/api/alerts/init", headers={"Authorization": f"Bearer {token_a}"})
        client.post("/api/alerts/init", headers={"Authorization": f"Bearer {token_b}"})

        client.put(
            "/api/alerts/config",
            json={"defaults": {"email_to": "a@example.com"}, "alerts": []},
            headers={"Authorization": f"Bearer {token_a}"},
        )
        client.put(
            "/api/alerts/config",
            json={"defaults": {"email_to": "b@example.com"}, "alerts": []},
            headers={"Authorization": f"Bearer {token_b}"},
        )

        cfg_a = client.get(
            "/api/alerts/config", headers={"Authorization": f"Bearer {token_a}"}
        ).json()
        cfg_b = client.get(
            "/api/alerts/config", headers={"Authorization": f"Bearer {token_b}"}
        ).json()
        assert cfg_a["config"]["defaults"]["email_to"] == "a@example.com"
        assert cfg_b["config"]["defaults"]["email_to"] == "b@example.com"

    def test_run_requires_auth(self, client, multi_user_env):
        r = client.post("/api/alerts/run")

        assert r.status_code == 401

    def test_run_uses_user_scoped_check(self, client, multi_user_env):
        token = _register(client, "runner@example.com")
        headers = {"Authorization": f"Bearer {token}"}

        with patch("src.alerts.alert_worker.run_user_check") as mock_check:
            mock_check.return_value = {
                "triggered": 2,
                "last_data_date": "2026-06-09",
                "events": [],
                "message": None,
            }
            r = client.post("/api/alerts/run", headers=headers)

        assert r.status_code == 200
        assert r.json()["triggered"] == 2
        assert r.json()["last_data_date"] == "2026-06-09"
        mock_check.assert_called_once()
        assert mock_check.call_args.args[0]

    def test_run_only_evaluates_authenticated_users_watches(self, client, multi_user_env):
        token_a = _register(client, "runner-a@example.com")
        token_b = _register(client, "runner-b@example.com")
        headers_a = {"Authorization": f"Bearer {token_a}"}
        headers_b = {"Authorization": f"Bearer {token_b}"}
        payload = {
            "defaults": {},
            "alerts": [
                {
                    "id": "aapl-low",
                    "name": "AAPL low",
                    "enabled": True,
                    "cooldown_minutes": 60,
                    "condition": {
                        "type": "price_threshold",
                        "symbol": "AAPL",
                        "operator": "less_than",
                        "value": 200,
                    },
                    "notifications": ["log"],
                }
            ],
        }
        saved_a = client.put("/api/alerts/config", json=payload, headers=headers_a)
        saved_b = client.put("/api/alerts/config", json=payload, headers=headers_b)
        assert saved_a.status_code == 200
        assert saved_b.status_code == 200

        with patch(
            "src.alerts.market_snapshot.load_market_snapshot",
            return_value=(
                "2026-06-09",
                {"AAPL": 150.0},
                [{"symbol": "AAPL", "close": 150.0}],
            ),
        ):
            with patch("src.alerts.alert_engine.LogNotifier.send", return_value=True) as send:
                first = client.post("/api/alerts/run", headers=headers_a)
                second = client.post("/api/alerts/run", headers=headers_a)

        assert first.status_code == 200
        assert first.json()["triggered"] == 1
        assert second.status_code == 200
        assert second.json()["triggered"] == 0
        assert send.call_count == 1
        with get_connection() as conn:
            triggered_users = conn.execute(
                "SELECT user_id FROM alert_trigger_state ORDER BY user_id"
            ).fetchall()
            user_b = conn.execute(
                "SELECT id FROM users WHERE email = ?",
                ("runner-b@example.com",),
            ).fetchone()
            user_a = conn.execute(
                "SELECT id FROM users WHERE email = ?",
                ("runner-a@example.com",),
            ).fetchone()
        assert len(triggered_users) == 1
        assert triggered_users[0]["user_id"] == user_a["id"]
        assert triggered_users[0]["user_id"] != user_b["id"]

    def test_webhook_secret_is_user_scoped_and_write_only(
        self, client, multi_user_env, tmp_path, monkeypatch
    ):
        user_config_dir = tmp_path / "user-config"
        monkeypatch.setattr("src.alerts.alert_paths.user_config_dir", lambda: user_config_dir)
        monkeypatch.setenv("DISCORD_WEBHOOK_URL", "https://discord.com/api/webhooks/global/token")

        token = _register(client, "webhook@example.com")
        headers = {"Authorization": f"Bearer {token}"}
        client.post("/api/alerts/init", headers=headers)

        payload = {
            "defaults": {
                "webhook_url": "https://discord.com/api/webhooks/user/token",
                "webhook_format": "discord",
                "notify_webhook": True,
            },
            "alerts": [
                {
                    "id": "aapl_drop",
                    "enabled": True,
                    "condition": {
                        "type": "price_threshold",
                        "symbol": "AAPL",
                        "operator": "less_than",
                        "value": 150,
                    },
                    "notifications": ["webhook"],
                }
            ],
        }

        first = client.put("/api/alerts/config", json=payload, headers=headers)

        assert first.status_code == 200
        assert first.json()["channels"]["webhook_url"] is True
        serialized = json.dumps(first.json())
        assert "user/token" not in serialized
        assert "global/token" not in serialized
        assert not (user_config_dir / ".env").exists()

        # The frontend sends later edits without the write-only URL; keep the
        # saved per-user secret instead of falling back to the global env var.
        payload["defaults"].pop("webhook_url")
        payload["defaults"]["email_to"] = "webhook@example.com"
        second = client.put("/api/alerts/config", json=payload, headers=headers)

        assert second.status_code == 200
        assert second.json()["channels"]["webhook_url"] is True
        assert "user/token" not in json.dumps(second.json())
        assert second.json()["config"]["alerts"][0]["id"] == "aapl_drop"

        saved_user = authenticate_user("webhook@example.com", "password123")
        assert saved_user is not None
        _, raw = load_user_alerts_config(saved_user["id"])
        assert raw is not None
        assert raw["defaults"]["webhook_url"].endswith("/user/token")

    def test_test_send_records_status_for_authenticated_user_only(self, client, multi_user_env):
        token_a = _register(client, "status-a@example.com")
        token_b = _register(client, "status-b@example.com")
        headers_a = {"Authorization": f"Bearer {token_a}"}
        headers_b = {"Authorization": f"Bearer {token_b}"}
        payload = {
            "defaults": {"email_to": "a@example.com", "notify_email": True},
            "alerts": [
                {
                    "id": "price_watch",
                    "name": "Price watch",
                    "enabled": True,
                    "notifications": ["email"],
                    "condition": {
                        "type": "price_threshold",
                        "symbol": "AAPL",
                        "operator": "less_than",
                        "value": 200,
                    },
                }
            ],
        }

        assert client.post("/api/alerts/init", headers=headers_a).status_code == 200
        assert client.put("/api/alerts/config", json=payload, headers=headers_a).status_code == 200
        assert client.post("/api/alerts/init", headers=headers_b).status_code == 200

        class EmailNotifier:
            def send(self, _event):
                return True

        with patch(
            "src.cli.alerts_commands.AlertEngine._build_notifiers",
            return_value=[EmailNotifier()],
        ):
            sent = client.post(
                "/api/alerts/test",
                json={"id": "price_watch", "dry_run": False},
                headers=headers_a,
            )

        assert sent.status_code == 200
        assert sent.json()["status"] == "sent"

        status_a = client.get("/api/alerts/status", headers=headers_a)
        status_b = client.get("/api/alerts/status", headers=headers_b)

        assert status_a.status_code == 200
        assert status_b.status_code == 200
        deliveries_a = status_a.json()["latest_deliveries"]
        assert len(deliveries_a) == 1
        assert deliveries_a[0]["alert_id"] == "price_watch"
        assert deliveries_a[0]["channel"] == "email"
        assert deliveries_a[0]["success"] is True
        assert deliveries_a[0]["test"] is True
        assert deliveries_a[0]["error"] is None
        assert status_b.json()["latest_deliveries"] == []

    def test_global_webhook_env_does_not_mark_hosted_channel_ready(
        self, client, multi_user_env, monkeypatch
    ):
        """Hosted mode must ignore global DISCORD_WEBHOOK_URL for channel readiness."""
        monkeypatch.setenv("DISCORD_WEBHOOK_URL", "https://discord.com/api/webhooks/global/token")
        monkeypatch.delenv("ALERT_WEBHOOK_URL", raising=False)

        token = _register(client, "global-webhook@example.com")
        headers = {"Authorization": f"Bearer {token}"}
        assert client.post("/api/alerts/init", headers=headers).status_code == 200

        response = client.get("/api/alerts/config", headers=headers)
        assert response.status_code == 200
        body = response.json()
        assert body["exists"] is True
        assert body["channels"]["webhook_url"] is False
        assert "global/token" not in json.dumps(body)

        without_secret = {
            "defaults": {"notify_webhook": True, "webhook_format": "discord"},
            "alerts": [],
        }
        put = client.put("/api/alerts/config", json=without_secret, headers=headers)
        assert put.status_code == 200
        assert put.json()["channels"]["webhook_url"] is False

    def test_global_email_env_does_not_mark_hosted_recipients_ready(
        self, client, multi_user_env, monkeypatch
    ):
        """Hosted mode must ignore process-wide ALERT_EMAIL_TO for recipient readiness."""
        monkeypatch.setenv("ALERT_EMAIL_TO", "global-shared@example.com")

        token = _register(client, "global-email@example.com")
        headers = {"Authorization": f"Bearer {token}"}
        assert client.post("/api/alerts/init", headers=headers).status_code == 200

        response = client.get("/api/alerts/config", headers=headers)
        assert response.status_code == 200
        body = response.json()
        assert body["exists"] is True
        assert body["config"]["defaults"].get("email_to") in (None, "")
        assert body["channels"]["email_recipients"] is False
        assert "global-shared@example.com" not in json.dumps(body)

        without_recipients = {
            "defaults": {"notify_email": True},
            "alerts": [],
        }
        put = client.put("/api/alerts/config", json=without_recipients, headers=headers)
        assert put.status_code == 200
        assert put.json()["channels"]["email_recipients"] is False
        assert put.json()["config"]["defaults"].get("email_to") in (None, "")

        with_tenant_email = {
            "defaults": {"email_to": "tenant@example.com", "notify_email": True},
            "alerts": [],
        }
        put2 = client.put("/api/alerts/config", json=with_tenant_email, headers=headers)
        assert put2.status_code == 200
        assert put2.json()["channels"]["email_recipients"] is True
        assert put2.json()["config"]["defaults"]["email_to"] == "tenant@example.com"

    def test_status_active_watches_and_last_triggered_are_user_scoped(self, client, multi_user_env):
        token_a = _register(client, "watches-a@example.com")
        token_b = _register(client, "watches-b@example.com")
        headers_a = {"Authorization": f"Bearer {token_a}"}
        headers_b = {"Authorization": f"Bearer {token_b}"}

        payload_a = {
            "defaults": {"email_to": "a@example.com"},
            "alerts": [
                {
                    "id": "aapl_watch",
                    "enabled": True,
                    "condition": {
                        "type": "price_threshold",
                        "symbol": "AAPL",
                        "operator": "less_than",
                        "value": 150,
                    },
                },
                {
                    "id": "msft_watch",
                    "enabled": True,
                    "condition": {
                        "type": "price_threshold",
                        "symbol": "MSFT",
                        "operator": "greater_than",
                        "value": 400,
                    },
                },
                {
                    "id": "disabled_watch",
                    "enabled": False,
                    "condition": {
                        "type": "price_threshold",
                        "symbol": "GOOG",
                        "operator": "less_than",
                        "value": 100,
                    },
                },
            ],
        }
        payload_b = {
            "defaults": {"email_to": "b@example.com"},
            "alerts": [
                {
                    "id": "meta_watch",
                    "enabled": True,
                    "condition": {
                        "type": "price_threshold",
                        "symbol": "META",
                        "operator": "less_than",
                        "value": 200,
                    },
                }
            ],
        }

        assert client.post("/api/alerts/init", headers=headers_a).status_code == 200
        assert (
            client.put("/api/alerts/config", json=payload_a, headers=headers_a).status_code == 200
        )
        assert client.post("/api/alerts/init", headers=headers_b).status_code == 200
        assert (
            client.put("/api/alerts/config", json=payload_b, headers=headers_b).status_code == 200
        )

        user_a = authenticate_user("watches-a@example.com", "password123")
        assert user_a is not None
        UserAlertStorage(user_a["id"]).record_event(
            {"alert_id": "aapl_watch", "timestamp": "2026-07-24T15:30:00Z"}
        )

        status_a = client.get("/api/alerts/status", headers=headers_a)
        status_b = client.get("/api/alerts/status", headers=headers_b)

        assert status_a.status_code == 200
        assert status_b.status_code == 200
        assert status_a.json()["active_watches"] == 2
        assert status_a.json()["last_triggered_at"] == "2026-07-24T15:30:00Z"
        assert status_b.json()["active_watches"] == 1
        assert status_b.json()["last_triggered_at"] is None

    def test_init_conflict_and_test_requires_existing_config(self, client, multi_user_env):
        token = _register(client, "init-conflict@example.com")
        headers = {"Authorization": f"Bearer {token}"}

        first = client.post("/api/alerts/init", headers=headers)
        assert first.status_code == 200

        payload = {
            "defaults": {"email_to": "init@example.com"},
            "alerts": [
                {
                    "id": "keep_me",
                    "enabled": True,
                    "condition": {
                        "type": "price_threshold",
                        "symbol": "AAPL",
                        "operator": "less_than",
                        "value": 100,
                    },
                }
            ],
        }
        assert client.put("/api/alerts/config", json=payload, headers=headers).status_code == 200

        conflict = client.post("/api/alerts/init", headers=headers)
        assert conflict.status_code == 409
        assert "force=true" in conflict.json()["detail"]

        saved = client.get("/api/alerts/config", headers=headers)
        assert saved.status_code == 200
        assert len(saved.json()["config"]["alerts"]) == 1
        assert saved.json()["config"]["alerts"][0]["id"] == "keep_me"

        forced = client.post("/api/alerts/init?force=true", headers=headers)
        assert forced.status_code == 200
        reset = client.get("/api/alerts/config", headers=headers)
        assert reset.status_code == 200
        assert reset.json()["exists"] is True
        assert reset.json()["config"]["alerts"] == []

        other = _register(client, "no-config-yet@example.com")
        missing = client.post(
            "/api/alerts/test",
            json={"id": "missing", "dry_run": True},
            headers={"Authorization": f"Bearer {other}"},
        )
        assert missing.status_code == 404
        assert missing.json()["detail"] == "No alerts config for this user."

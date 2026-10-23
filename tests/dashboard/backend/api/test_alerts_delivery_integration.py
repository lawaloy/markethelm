"""Integration tests for alert delivery status (API + storage, not mocked at handler level)."""

import json
from pathlib import Path
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from dashboard.backend.main import app
from src.alerts import alert_storage


class EmailNotifier:
    def __init__(self, result: bool = True):
        self._result = result

    def send(self, _event):
        return self._result


class WebhookNotifier:
    def __init__(self, result: bool = True):
        self._result = result

    def send(self, _event):
        return self._result


@pytest.fixture
def alerts_config_dir(tmp_path: Path, monkeypatch):
    """Isolated alerts config directory."""
    config_dir = tmp_path / "markethelm"
    config_dir.mkdir()
    config_path = config_dir / "alerts.json"
    monkeypatch.setenv("MARKETHELM_ALERTS_CONFIG", str(config_path))
    return config_dir


@pytest.fixture
def alert_history_dir(tmp_path: Path, monkeypatch):
    """Isolated alert history (delivery log) directory."""

    history_dir = tmp_path / "data"
    history_dir.mkdir()
    real_storage = alert_storage.AlertStorage

    def storage_factory(data_dir=None):
        return real_storage(data_dir=history_dir)

    monkeypatch.setattr(alert_storage, "AlertStorage", storage_factory)
    monkeypatch.setattr("src.cli.alerts_commands.AlertStorage", storage_factory)
    return history_dir


@pytest.fixture
def client():
    return TestClient(app)


class TestAlertsDeliveryIntegration:
    def test_status_returns_seeded_delivery_log(self, client, alerts_config_dir, alert_history_dir):
        history_path = alert_history_dir / "alerts_history.json"
        history_path.write_text(
            json.dumps(
                {
                    "last_triggered": {},
                    "events": [],
                    "delivery_log": [
                        {
                            "alert_id": "rule1",
                            "channel": "email",
                            "success": True,
                            "test": True,
                            "timestamp": "2026-06-21T12:00:00",
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )

        response = client.get("/api/alerts/status")

        assert response.status_code == 200
        deliveries = response.json()["latest_deliveries"]
        assert len(deliveries) == 1
        assert deliveries[0]["channel"] == "email"
        assert deliveries[0]["success"] is True
        assert deliveries[0]["test"] is True

    def test_test_send_records_delivery_and_status_reflects_it(
        self, client, alerts_config_dir, alert_history_dir
    ):
        payload = {
            "defaults": {"email_to": "user@example.com", "notify_email": True},
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
        save = client.put("/api/alerts/config", json=payload)
        assert save.status_code == 200

        email_notifier = EmailNotifier(result=True)

        with patch(
            "src.cli.alerts_commands.AlertEngine._build_notifiers",
            return_value=[email_notifier],
        ):
            test_response = client.post(
                "/api/alerts/test",
                json={"id": "price_watch", "dry_run": False},
            )

        assert test_response.status_code == 200
        assert test_response.json()["status"] == "sent"

        status = client.get("/api/alerts/status")
        assert status.status_code == 200
        deliveries = status.json()["latest_deliveries"]
        assert any(
            item["channel"] == "email" and item["success"] is True and item["test"] is True
            for item in deliveries
        )

        history = json.loads(
            (alert_history_dir / "alerts_history.json").read_text(encoding="utf-8")
        )
        assert history["delivery_log"][-1]["alert_id"] == "price_watch"
        assert history["delivery_log"][-1]["channel"] == "email"

    def test_failed_test_send_records_failure(self, client, alerts_config_dir, alert_history_dir):
        client.put(
            "/api/alerts/config",
            json={
                "defaults": {"notify_webhook": True},
                "alerts": [
                    {
                        "id": "hook_watch",
                        "name": "Hook watch",
                        "enabled": True,
                        "notifications": ["webhook"],
                        "webhook_url": "https://example.com/hook",
                        "condition": {
                            "type": "price_threshold",
                            "symbol": "AAPL",
                            "operator": "less_than",
                            "value": 200,
                        },
                    }
                ],
            },
        )

        webhook = WebhookNotifier(result=False)

        with patch(
            "src.cli.alerts_commands.AlertEngine._build_notifiers",
            return_value=[webhook],
        ):
            response = client.post(
                "/api/alerts/test",
                json={"id": "hook_watch", "dry_run": False},
            )

        assert response.status_code == 400
        assert "delivered" in response.json()["detail"].lower()

        status = client.get("/api/alerts/status")
        deliveries = status.json()["latest_deliveries"]
        assert any(
            item["channel"] == "webhook" and item["success"] is False and item["test"] is True
            for item in deliveries
        )

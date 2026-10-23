"""Tests for dashboard alerts settings API."""

import asyncio
import json
import threading
import time
from pathlib import Path
from unittest.mock import MagicMock

import pandas as pd
import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from httpx import ASGITransport, AsyncClient

from dashboard.backend.api.alerts import _normalize_config
from dashboard.backend.main import app
from src.alerts.alert_storage import AlertStorage


@pytest.fixture
def alerts_config_dir(tmp_path: Path, monkeypatch):
    """Isolated alerts config directory."""
    config_dir = tmp_path / "markethelm"
    config_dir.mkdir()
    monkeypatch.setenv("MARKETHELM_ALERTS_CONFIG", str(config_dir / "alerts.json"))
    return config_dir


@pytest.fixture
def client():
    return TestClient(app)


class TestAlertsConfigAPI:
    def test_get_status_soft_fails_when_projections_loader_raises(
        self, client, alerts_config_dir, monkeypatch
    ):
        class BoomLoader:
            def get_latest_date(self):
                raise ValueError("no data available")

            def load_projections(self):
                raise RuntimeError("corrupt projections")

        monkeypatch.setattr(
            "dashboard.backend.api.alerts.get_data_loader",
            lambda: BoomLoader(),
        )
        path = alerts_config_dir / "alerts.json"
        path.write_text(
            json.dumps(
                {
                    "alerts": [
                        {"id": "a1", "enabled": True},
                        {"id": "a2", "enabled": False},
                    ]
                }
            ),
            encoding="utf-8",
        )

        r = client.get("/api/alerts/status")
        assert r.status_code == 200
        data = r.json()
        assert data["tracked_symbols"] == []
        assert data["last_data_date"] is None
        assert data["active_watches"] == 1

    def test_get_status_soft_fails_when_delivery_storage_raises(
        self, client, alerts_config_dir, monkeypatch
    ):
        class BoomStorage:
            def latest_event_timestamp(self):
                raise RuntimeError("disk error")

        monkeypatch.setattr(
            "src.alerts.alert_storage.AlertStorage",
            lambda data_dir=None: BoomStorage(),
        )

        r = client.get("/api/alerts/status")
        assert r.status_code == 200
        data = r.json()
        assert data["last_triggered_at"] is None
        assert data["latest_deliveries"] == []

    def test_get_symbols_soft_fails_tracked_when_projections_raise(self, client, monkeypatch):
        monkeypatch.setattr(
            "dashboard.backend.api.alerts.build_symbol_catalog",
            lambda: (["AAPL"], {"AAPL": "Apple Inc."}),
        )
        monkeypatch.setattr(
            "dashboard.backend.api.alerts.prices_from_saved_daily_data",
            lambda: {"AAPL": 180.0},
        )

        class BoomLoader:
            def load_projections(self):
                raise ValueError("missing projections")

        monkeypatch.setattr(
            "dashboard.backend.api.alerts.get_data_loader",
            lambda: BoomLoader(),
        )

        r = client.get("/api/alerts/symbols")
        assert r.status_code == 200
        data = r.json()
        assert data["symbols"] == ["AAPL"]
        assert data["tracked_symbols"] == []
        assert data["prices"]["AAPL"] == 180.0

    def test_get_config_when_missing_returns_empty(self, client, alerts_config_dir):
        r = client.get("/api/alerts/config")
        assert r.status_code == 200
        data = r.json()
        assert data["exists"] is False
        assert data["config"]["alerts"] == []

    def test_get_config_never_returns_webhook_url(self, client, alerts_config_dir, monkeypatch):
        config_path = alerts_config_dir / "alerts.json"
        monkeypatch.setenv("MARKETHELM_ALERTS_CONFIG", str(config_path))
        monkeypatch.setenv("DISCORD_WEBHOOK_URL", "https://discord.com/api/webhooks/secret/token")
        config_path.write_text(
            json.dumps(
                {
                    "defaults": {"webhook_url": "https://discord.com/api/webhooks/leaked/token"},
                    "alerts": [],
                }
            ),
            encoding="utf-8",
        )
        r = client.get("/api/alerts/config")
        assert r.status_code == 200
        data = r.json()
        assert data["config"]["defaults"].get("webhook_url") is None
        assert data["channels"]["webhook_url"] is True
        assert "secret" not in json.dumps(data)

    def test_init_creates_config(self, client, alerts_config_dir):
        r = client.post("/api/alerts/init")
        assert r.status_code == 200
        assert (alerts_config_dir / "alerts.json").exists()

        r2 = client.get("/api/alerts/config")
        assert r2.status_code == 200
        assert r2.json()["exists"] is True
        assert r2.json()["config"]["alerts"] == []

    def test_init_conflict_without_force(self, client, alerts_config_dir):
        path = alerts_config_dir / "alerts.json"
        path.write_text('{"alerts": []}', encoding="utf-8")
        r = client.post("/api/alerts/init")
        assert r.status_code == 409

    def test_put_and_get_config(self, client, alerts_config_dir):
        payload = {
            "defaults": {"email_to": "user@example.com"},
            "alerts": [
                {
                    "id": "aapl_drop",
                    "name": "AAPL drop",
                    "enabled": True,
                    "condition": {
                        "type": "price_threshold",
                        "symbol": "AAPL",
                        "operator": "less_than",
                        "value": 150,
                    },
                    "notifications": ["log", "email"],
                    "cooldown_minutes": 60,
                }
            ],
        }
        r = client.put("/api/alerts/config", json=payload)
        assert r.status_code == 200
        data = r.json()
        assert data["exists"] is True
        assert data["config"]["defaults"]["email_to"] == "user@example.com"
        assert data["config"]["alerts"][0]["id"] == "aapl_drop"

        saved = json.loads((alerts_config_dir / "alerts.json").read_text(encoding="utf-8"))
        assert saved["alerts"][0]["enabled"] is True

    def test_put_config_persists_webhook_secret_only_to_user_env(
        self, client, alerts_config_dir, tmp_path, monkeypatch
    ):
        user_config_dir = tmp_path / "user-config"
        monkeypatch.setattr("src.alerts.alert_paths.user_config_dir", lambda: user_config_dir)
        monkeypatch.delenv("DISCORD_WEBHOOK_URL", raising=False)
        monkeypatch.delenv("ALERT_WEBHOOK_FORMAT", raising=False)
        payload = {
            "defaults": {
                "webhook_url": " https://discord.com/api/webhooks/secret/token ",
                "webhook_format": "DISCORD",
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
                    "webhook_url": "https://discord.com/api/webhooks/rule/secret",
                }
            ],
        }

        r = client.put("/api/alerts/config", json=payload)

        assert r.status_code == 200
        data = r.json()
        serialized_response = json.dumps(data)
        assert "secret/token" not in serialized_response
        assert "rule/secret" not in serialized_response
        assert data["channels"]["webhook_url"] is True
        assert data["config"]["defaults"]["webhook_format"] == "discord"
        assert data["config"]["defaults"].get("webhook_url") is None
        assert "webhook_url" not in data["config"]["alerts"][0]

        saved = json.loads((alerts_config_dir / "alerts.json").read_text(encoding="utf-8"))
        assert saved["defaults"]["webhook_format"] == "discord"
        assert "webhook_url" not in saved["defaults"]
        assert "webhook_url" not in saved["alerts"][0]

        env_file = user_config_dir / ".env"
        assert env_file.read_text(encoding="utf-8").splitlines() == [
            "DISCORD_WEBHOOK_URL=https://discord.com/api/webhooks/secret/token",
            "ALERT_WEBHOOK_FORMAT=discord",
        ]

    def test_test_alert_dry_run(self, client, alerts_config_dir):
        payload = {
            "defaults": {"email_to": "user@example.com"},
            "alerts": [
                {
                    "id": "test_rule",
                    "name": "Test rule",
                    "enabled": True,
                    "condition": {
                        "type": "price_threshold",
                        "symbol": "AAPL",
                        "operator": "less_than",
                        "value": 1,
                    },
                    "notifications": ["log"],
                }
            ],
        }
        client.put("/api/alerts/config", json=payload)
        r = client.post("/api/alerts/test", json={"id": "test_rule", "dry_run": True})
        assert r.status_code == 200
        data = r.json()
        assert data["status"] == "dry_run"
        assert data["notifiers"] == []

    def test_test_alert_not_found(self, client, alerts_config_dir):
        r = client.post("/api/alerts/test", json={"id": "missing", "dry_run": True})
        assert r.status_code == 404

    def test_get_symbols_includes_index_catalog(self, client, monkeypatch):
        monkeypatch.setattr(
            "dashboard.backend.api.alerts.build_symbol_catalog",
            lambda: (
                ["AAPL", "MSFT", "ZZZZ"],
                {"AAPL": "Apple Inc.", "MSFT": "Microsoft", "ZZZZ": "ZZZZ"},
            ),
        )
        r = client.get("/api/alerts/symbols")
        assert r.status_code == 200
        data = r.json()
        assert data["count"] == 3
        assert "AAPL" in data["symbols"]
        assert data["names"]["AAPL"] == "Apple Inc."
        assert "prices" in data

    def test_post_quotes(self, client, monkeypatch):
        monkeypatch.setattr(
            "dashboard.backend.api.alerts.resolve_symbol_prices",
            lambda symbols, fetch_missing=True: {"AAPL": 180.0},
        )
        r = client.post("/api/alerts/quotes", json={"symbols": ["AAPL", "MSFT"]})
        assert r.status_code == 200
        assert r.json()["prices"]["AAPL"] == 180.0

    def test_post_quotes_caps_symbols_before_resolving(self, client, monkeypatch):
        captured = {}

        def fake_resolve(symbols, fetch_missing=True):
            captured["symbols"] = symbols
            captured["fetch_missing"] = fetch_missing
            return {}

        monkeypatch.setattr(
            "dashboard.backend.api.alerts.resolve_symbol_prices",
            fake_resolve,
        )

        r = client.post(
            "/api/alerts/quotes",
            json={"symbols": [f"SYM{i}" for i in range(20)]},
        )

        assert r.status_code == 200
        assert captured["symbols"] == [f"SYM{i}" for i in range(15)]
        assert captured["fetch_missing"] is True

    def test_get_quotes(self, client, monkeypatch):
        monkeypatch.setattr(
            "dashboard.backend.api.alerts.resolve_symbol_prices",
            lambda symbols, fetch_missing=True: {"AON": 412.5, "APH": 88.0},
        )
        r = client.get("/api/alerts/quotes", params={"symbols": "AON,APH"})
        assert r.status_code == 200
        assert r.json()["prices"]["AON"] == 412.5

    def test_get_quotes_strips_whitespace_before_resolve(self, client, monkeypatch):
        captured = {}

        def fake_resolve(symbols, fetch_missing=True):
            captured["symbols"] = symbols
            return {"AAPL": 180.0}

        monkeypatch.setattr(
            "dashboard.backend.api.alerts.resolve_symbol_prices",
            fake_resolve,
        )
        r = client.get("/api/alerts/quotes", params={"symbols": " AAPL , msft "})
        assert r.status_code == 200
        assert captured["symbols"] == ["AAPL", "MSFT"]

    def test_post_quotes_strips_whitespace_before_resolve(self, client, monkeypatch):
        captured = {}

        def fake_resolve(symbols, fetch_missing=True):
            captured["symbols"] = symbols
            return {"AAPL": 180.0}

        monkeypatch.setattr(
            "dashboard.backend.api.alerts.resolve_symbol_prices",
            fake_resolve,
        )
        r = client.post("/api/alerts/quotes", json={"symbols": [" AAPL ", "  ", "msft"]})
        assert r.status_code == 200
        assert captured["symbols"] == ["AAPL", "MSFT"]

    def test_alert_symbol_catalog_skips_invalid_tracked_tokens(self, client, monkeypatch):
        monkeypatch.setattr(
            "dashboard.backend.api.alerts.build_symbol_catalog",
            lambda: (["AAPL"], {"AAPL": "Apple Inc."}),
        )
        monkeypatch.setattr(
            "dashboard.backend.api.alerts.prices_from_saved_daily_data",
            lambda: {"AAPL": 180.0},
        )
        loader = MagicMock()
        loader.load_projections.return_value = pd.DataFrame(
            {
                "symbol": ["AAPL", None, float("nan"), "  ", "msft"],
            }
        )
        monkeypatch.setattr(
            "dashboard.backend.api.alerts.get_data_loader",
            lambda: loader,
        )
        r = client.get("/api/alerts/symbols")
        assert r.status_code == 200
        assert r.json()["tracked_symbols"] == ["AAPL", "MSFT"]

    def test_get_status_normalizes_tracked_symbols(self, client, alerts_config_dir, monkeypatch):
        """Status tracked list strips padding and drops None/NaN sentinels."""

        loader = MagicMock()
        loader.get_latest_date.return_value = "2026-01-15"
        loader.load_projections.return_value = pd.DataFrame(
            {
                "symbol": [" aapl ", None, float("nan"), "  ", "msft", "AAPL"],
            }
        )
        monkeypatch.setattr(
            "dashboard.backend.api.alerts.get_data_loader",
            lambda: loader,
        )
        monkeypatch.setattr(
            "src.alerts.alert_storage.AlertStorage",
            lambda data_dir=None: MagicMock(
                latest_event_timestamp=MagicMock(return_value=None),
            ),
        )
        monkeypatch.setattr(
            "src.alerts.delivery_status.latest_deliveries_by_channel",
            lambda storage: [],
        )

        r = client.get("/api/alerts/status")
        assert r.status_code == 200
        assert r.json()["tracked_symbols"] == ["AAPL", "MSFT"]

    def test_get_status(self, client, alerts_config_dir, tmp_path, monkeypatch):
        storage = AlertStorage(data_dir=tmp_path)
        storage.record_delivery(
            alert_id="rule1",
            channel="email",
            success=True,
            test=True,
            timestamp="2026-06-21T12:00:00",
        )
        monkeypatch.setattr(
            "src.alerts.alert_storage.AlertStorage",
            lambda data_dir=None: storage,
        )

        r = client.get("/api/alerts/status")
        assert r.status_code == 200
        data = r.json()
        assert data["checks_on_fetch"] is True
        assert "tracked_symbols" in data
        assert "active_watches" in data
        assert data["latest_deliveries"]
        assert data["latest_deliveries"][0]["channel"] == "email"
        assert data["latest_deliveries"][0]["success"] is True

    def test_get_status_preserves_timestamp_when_deliveries_raise(
        self, client, alerts_config_dir, monkeypatch
    ):
        """Delivery channel lookup failure must not wipe last_triggered_at."""

        class PartialStorage:
            def latest_event_timestamp(self):
                return "2026-07-24T10:00:00"

        monkeypatch.setattr(
            "src.alerts.alert_storage.AlertStorage",
            lambda data_dir=None: PartialStorage(),
        )
        monkeypatch.setattr(
            "src.alerts.delivery_status.latest_deliveries_by_channel",
            lambda _storage: (_ for _ in ()).throw(RuntimeError("delivery index corrupt")),
        )

        r = client.get("/api/alerts/status")
        assert r.status_code == 200
        data = r.json()
        assert data["last_triggered_at"] == "2026-07-24T10:00:00"
        assert data["latest_deliveries"] == []

    def test_run_without_config(self, client, alerts_config_dir):
        r = client.post("/api/alerts/run")
        assert r.status_code == 200
        assert r.json()["triggered"] == 0

    def test_put_config_rejects_alert_without_id(self, client, alerts_config_dir):
        payload = {
            "defaults": {},
            "alerts": [
                {
                    "name": "missing id",
                    "enabled": True,
                    "condition": {
                        "type": "price_threshold",
                        "symbol": "AAPL",
                        "operator": "less_than",
                        "value": 150,
                    },
                    "notifications": ["log"],
                }
            ],
        }
        r = client.put("/api/alerts/config", json=payload)
        assert r.status_code == 400
        assert "id" in r.json()["detail"].lower()

    def test_get_config_scrubs_webhook_secrets_on_disk(
        self, client, alerts_config_dir, monkeypatch
    ):
        """Legacy on-disk webhook URLs must be rewritten without secrets."""
        config_path = alerts_config_dir / "alerts.json"
        monkeypatch.setenv("MARKETHELM_ALERTS_CONFIG", str(config_path))
        config_path.write_text(
            json.dumps(
                {
                    "defaults": {
                        "webhook_url": "https://discord.com/api/webhooks/leaked/token",
                        "webhook_format": "discord",
                    },
                    "alerts": [],
                }
            ),
            encoding="utf-8",
        )

        r = client.get("/api/alerts/config")
        assert r.status_code == 200
        assert r.json()["config"]["defaults"].get("webhook_url") is None

        saved = json.loads(config_path.read_text(encoding="utf-8"))
        assert "webhook_url" not in saved.get("defaults", {})
        assert "leaked/token" not in config_path.read_text(encoding="utf-8")

    def test_get_config_seeds_webhook_format_from_env(self, client, alerts_config_dir, monkeypatch):
        config_path = alerts_config_dir / "alerts.json"
        monkeypatch.setenv("MARKETHELM_ALERTS_CONFIG", str(config_path))
        monkeypatch.setenv("ALERT_WEBHOOK_FORMAT", "slack")
        config_path.write_text(
            json.dumps({"defaults": {}, "alerts": []}),
            encoding="utf-8",
        )

        r = client.get("/api/alerts/config")
        assert r.status_code == 200
        assert r.json()["config"]["defaults"]["webhook_format"] == "slack"

    def test_run_maps_no_market_data_to_404(self, client, alerts_config_dir, monkeypatch):
        monkeypatch.setattr(
            "src.alerts.alert_worker.run_check_once",
            lambda: {"message": "No market data available.", "triggered": 0},
        )
        r = client.post("/api/alerts/run")
        assert r.status_code == 404
        assert "No market data available" in r.json()["detail"]

    def test_run_maps_unexpected_failure_to_500(self, client, alerts_config_dir, monkeypatch):
        def boom():
            raise RuntimeError("worker crashed")

        monkeypatch.setattr("src.alerts.alert_worker.run_check_once", boom)
        r = client.post("/api/alerts/run")
        assert r.status_code == 500
        assert r.json()["detail"] == "Alert check failed."

    def test_normalize_config_rejects_non_list_alerts_and_normalizes_format(self):
        with pytest.raises(HTTPException) as exc_info:
            _normalize_config({"defaults": {}, "alerts": {"id": "not-a-list"}})
        assert exc_info.value.status_code == 400
        assert "alerts" in str(exc_info.value.detail).lower()

        normalized = _normalize_config({"defaults": {"webhook_format": " Discord "}, "alerts": []})
        assert normalized["defaults"]["webhook_format"] == "discord"
        assert _normalize_config(None) == {"defaults": {}, "alerts": []}


@pytest.mark.parametrize("method", ["get", "post"])
def test_slow_quote_lookup_does_not_block_other_requests(monkeypatch, method):
    """Live quote latency must not stall the dashboard's async request loop."""

    started = threading.Event()
    release = threading.Event()

    def slow_resolver(symbols, fetch_missing=True):
        started.set()
        release.wait(timeout=8)
        return {"AAPL": 180.0}

    monkeypatch.setattr("dashboard.backend.api.alerts.resolve_symbol_prices", slow_resolver)

    async def exercise():
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://testserver"
        ) as client:
            if method == "get":
                quote_task = asyncio.create_task(
                    client.get("/api/alerts/quotes", params={"symbols": "AAPL"})
                )
            else:
                quote_task = asyncio.create_task(
                    client.post("/api/alerts/quotes", json={"symbols": ["AAPL"]})
                )
            try:
                assert await asyncio.to_thread(started.wait, 5)
                before = time.monotonic()
                health = await client.get("/api/alerts/health")
                assert time.monotonic() - before < 3.5
                assert health.status_code == 200
            finally:
                release.set()
                quote = await quote_task
            assert quote.status_code == 200
            assert quote.json()["prices"] == {"AAPL": 180.0}

    asyncio.run(exercise())

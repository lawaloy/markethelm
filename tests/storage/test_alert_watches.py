"""Unit tests for normalized alert watches."""

import json
from datetime import datetime, timedelta, timezone

import pytest

from src.storage.alert_watches import (
    MAX_DELIVERY_LOG,
    InvalidAlertWatchConfig,
    latest_deliveries_for_user,
    list_enabled_symbols,
    list_watches_for_symbol,
    record_delivery,
    validate_watches_config,
)
from src.storage.database import get_connection, init_database
from src.storage.user_alerts import save_user_alerts_config
from src.storage.users import create_user


@pytest.fixture
def db_user(tmp_path, monkeypatch):
    db_path = tmp_path / "watches.db"
    monkeypatch.setenv("MARKETHELM_DATABASE_URL", f"sqlite:///{db_path.as_posix()}")
    init_database()
    user = create_user("watches@example.com", "password123")
    return user["id"]


def _sample_config():
    return {
        "defaults": {"email_to": "user@example.com"},
        "alerts": [
            {
                "id": "aapl-drop",
                "name": "AAPL drop",
                "enabled": True,
                "cooldown_minutes": 30,
                "condition": {
                    "type": "price_threshold",
                    "symbol": "AAPL",
                    "operator": "less_than",
                    "value": 200,
                },
                "notifiers": [{"type": "console"}],
            },
            {
                "id": "msft-disabled",
                "enabled": False,
                "condition": {
                    "type": "price_threshold",
                    "symbol": "MSFT",
                    "operator": "greater_than",
                    "value": 100,
                },
            },
        ],
    }


class TestAlertWatches:
    def test_sync_on_save(self, db_user):
        save_user_alerts_config(db_user, _sample_config())
        assert list_enabled_symbols() == ["AAPL"]
        watches = list_watches_for_symbol("AAPL")
        assert len(watches) == 1
        assert watches[0]["user_id"] == db_user
        assert watches[0]["alert_id"] == "aapl-drop"

    def test_symbol_index_dedupes_symbols_but_keeps_each_user_watch(self, db_user):
        other_user = create_user("other-watches@example.com", "password123")["id"]
        save_user_alerts_config(db_user, _sample_config())
        save_user_alerts_config(
            other_user,
            {
                "defaults": {"email_to": "other@example.com"},
                "alerts": [
                    {
                        "id": "aapl-rise",
                        "enabled": True,
                        "cooldown_minutes": 5,
                        "condition": {
                            "type": "price_threshold",
                            "symbol": "aapl",
                            "operator": "greater_than",
                            "value": 150,
                        },
                    },
                    {
                        "id": "goog-disabled",
                        "enabled": False,
                        "condition": {
                            "type": "price_threshold",
                            "symbol": "GOOG",
                            "operator": "greater_than",
                            "value": 100,
                        },
                    },
                ],
            },
        )

        assert list_enabled_symbols() == ["AAPL"]

        watches = list_watches_for_symbol("aapl")
        assert {(watch["user_id"], watch["alert_id"]) for watch in watches} == {
            (db_user, "aapl-drop"),
            (other_user, "aapl-rise"),
        }

    def test_sync_replaces_rows(self, db_user):
        save_user_alerts_config(db_user, _sample_config())
        save_user_alerts_config(db_user, {"defaults": {}, "alerts": []})
        assert list_enabled_symbols() == []
        with get_connection() as conn:
            count = conn.execute(
                "SELECT COUNT(*) AS n FROM alert_watches WHERE user_id = ?",
                (db_user,),
            ).fetchone()["n"]
        assert count == 0

    def test_backfill_on_init_database(self, db_user, tmp_path, monkeypatch):
        save_user_alerts_config(db_user, _sample_config())
        init_database()
        assert list_enabled_symbols() == ["AAPL"]

    def test_backfill_skips_invalid_config_rows(self, db_user):
        bad_config = {
            "defaults": {},
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
        with get_connection() as conn:
            conn.execute(
                """
                INSERT INTO user_alert_configs (user_id, config_json, updated_at)
                VALUES (?, ?, ?)
                """,
                (db_user, json.dumps(bad_config), "2026-07-12T00:00:00+00:00"),
            )

        init_database()

        assert list_enabled_symbols() == []

    def test_validate_rejects_non_finite_price_threshold(self, db_user):
        """NaN/Inf thresholds must fail closed so watches cannot store corrupt prices."""
        for bad in (float("nan"), float("inf"), float("-inf")):
            with pytest.raises(InvalidAlertWatchConfig, match="invalid price threshold"):
                validate_watches_config(
                    db_user,
                    {
                        "defaults": {},
                        "alerts": [
                            {
                                "id": "bad-finite",
                                "enabled": True,
                                "condition": {
                                    "type": "price_threshold",
                                    "symbol": "AAPL",
                                    "operator": "less_than",
                                    "value": bad,
                                },
                            }
                        ],
                    },
                )

    def test_sync_normalizes_padded_symbols(self, db_user):
        """Watch index keys strip whitespace so quote lookups match."""
        save_user_alerts_config(
            db_user,
            {
                "defaults": {},
                "alerts": [
                    {
                        "id": "padded",
                        "enabled": True,
                        "condition": {
                            "type": "price_threshold",
                            "symbol": " aapl ",
                            "operator": "less_than",
                            "value": 200,
                        },
                    },
                ],
            },
        )

        assert list_enabled_symbols() == ["AAPL"]
        assert len(list_watches_for_symbol(" AAPL ")) == 1
        with get_connection() as conn:
            row = conn.execute(
                "SELECT symbol FROM alert_watches WHERE user_id = ? AND alert_id = ?",
                (db_user, "padded"),
            ).fetchone()
        assert row["symbol"] == "AAPL"


class TestAlertDeliveryLog:
    def test_record_delivery_prunes_to_max_per_user(self, db_user):
        """Oldest rows are deleted once a user exceeds MAX_DELIVERY_LOG."""

        other = create_user("other-delivery@example.com", "password123")["id"]
        base = datetime(2026, 7, 1, tzinfo=timezone.utc)
        for i in range(MAX_DELIVERY_LOG + 5):
            record_delivery(
                db_user,
                "aapl-drop",
                "email",
                success=True,
                timestamp=(base + timedelta(minutes=i)).isoformat(),
            )
        # Peer tenant must not be pruned away by this user's growth.
        record_delivery(
            other,
            "peer",
            "email",
            success=True,
            timestamp="2026-06-01T00:00:00+00:00",
        )

        with get_connection() as conn:
            n_user = conn.execute(
                "SELECT COUNT(*) AS n FROM alert_delivery_log WHERE user_id = ?",
                (db_user,),
            ).fetchone()["n"]
            n_other = conn.execute(
                "SELECT COUNT(*) AS n FROM alert_delivery_log WHERE user_id = ?",
                (other,),
            ).fetchone()["n"]
            oldest = conn.execute(
                """
                SELECT timestamp FROM alert_delivery_log
                WHERE user_id = ?
                ORDER BY timestamp ASC, id ASC
                LIMIT 1
                """,
                (db_user,),
            ).fetchone()["timestamp"]

        assert n_user == MAX_DELIVERY_LOG
        assert n_other == 1
        # First five minute offsets (0..4) should be gone.
        assert oldest == (base + timedelta(minutes=5)).isoformat()

    def test_latest_deliveries_for_user_keeps_newest_per_channel(self, db_user):
        record_delivery(
            db_user,
            "a1",
            "email",
            success=False,
            error="old",
            timestamp="2026-07-24T10:00:00+00:00",
        )
        record_delivery(
            db_user,
            "a1",
            "email",
            success=True,
            timestamp="2026-07-24T11:00:00+00:00",
        )
        record_delivery(
            db_user,
            "a2",
            "webhook",
            success=False,
            error="timeout",
            timestamp="2026-07-24T11:30:00+00:00",
        )

        latest = {row["channel"]: row for row in latest_deliveries_for_user(db_user)}
        assert set(latest) == {"email", "webhook"}
        assert latest["email"]["success"] is True
        assert latest["email"]["error"] is None
        assert latest["webhook"]["success"] is False
        assert latest["webhook"]["error"] == "timeout"
        assert latest["webhook"]["alert_id"] == "a2"

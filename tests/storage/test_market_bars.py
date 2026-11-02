"""Tests for durable market_bars storage (CSV daily_data removed)."""

from datetime import date

import pytest

from src.storage.data_storage import DataStorage
from src.storage.database import LATEST_SCHEMA_VERSION, get_connection, init_database
from src.storage.market_bars import (
    default_sidecar_path,
    list_market_bar_dates,
    load_market_bars,
    upsert_market_bars,
)


@pytest.fixture
def app_db(tmp_path, monkeypatch):
    db_path = tmp_path / "app.db"
    monkeypatch.setenv("MARKETHELM_DATABASE_URL", f"sqlite:///{db_path.as_posix()}")
    init_database()
    return db_path


def test_migration_seven_creates_projections_and_summaries(app_db):
    with get_connection() as conn:
        latest = conn.execute("SELECT MAX(version) AS version FROM schema_migrations").fetchone()
        assert latest["version"] == LATEST_SCHEMA_VERSION
        row = conn.execute("SELECT name FROM schema_migrations WHERE version = 7").fetchone()
        assert row["name"] == "projections_and_summaries"
        proj_cols = {
            item["name"] for item in conn.execute("PRAGMA table_info(projections)").fetchall()
        }
        summary_cols = {
            item["name"] for item in conn.execute("PRAGMA table_info(daily_summaries)").fetchall()
        }
    assert "run_date" in proj_cols
    assert "symbol" in proj_cols
    assert "summary_date" in summary_cols
    assert "payload_json" in summary_cols


def test_upsert_and_load_via_app_database(app_db):
    written = upsert_market_bars(
        [
            {"symbol": "AAPL", "name": "Apple", "close": 150.5, "volume": 1_000},
            {"symbol": "msft", "close": 400.0},
            {"symbol": "BAD", "close": float("nan")},
        ],
        date(2026, 9, 18),
    )
    assert written == 2
    rows = load_market_bars("2026-09-18")
    assert [row["symbol"] for row in rows] == ["AAPL", "MSFT"]
    assert rows[0]["close"] == 150.5
    assert list_market_bar_dates() == ["2026-09-18"]


def test_upsert_replaces_same_trade_date_symbol(app_db):
    upsert_market_bars([{"symbol": "AAPL", "close": 10.0}], "2026-09-18")
    upsert_market_bars([{"symbol": "AAPL", "close": 11.0}], "2026-09-18")
    rows = load_market_bars("2026-09-18")
    assert len(rows) == 1
    assert rows[0]["close"] == 11.0


def test_sidecar_sqlite_when_database_disabled(tmp_path, monkeypatch):
    monkeypatch.delenv("MARKETHELM_DATABASE_URL", raising=False)
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    written = upsert_market_bars(
        [{"symbol": "AAPL", "close": 12.0}],
        "2026-09-18",
        data_dir=data_dir,
    )
    assert written == 1
    assert default_sidecar_path(data_dir).is_file()
    rows = load_market_bars("2026-09-18", data_dir=data_dir)
    assert rows[0]["close"] == 12.0


def test_save_daily_data_writes_bars_not_csv(tmp_path, monkeypatch):
    monkeypatch.delenv("MARKETHELM_DATABASE_URL", raising=False)
    data_dir = tmp_path / "data"
    storage = DataStorage(data_dir=str(data_dir))
    location = storage.save_daily_data(
        [{"symbol": "AAPL", "name": "Apple", "close": 99.0, "volume": 10}],
        date=date(2026, 9, 18),
    )
    assert location == "market_bars:2026-09-18"
    assert list(data_dir.glob("daily_data_*.csv")) == []
    rows = load_market_bars("2026-09-18", data_dir=data_dir)
    assert len(rows) == 1
    assert rows[0]["close"] == 99.0
    loaded = storage.load_daily_data(trade_date=date(2026, 9, 18))
    assert loaded is not None
    assert float(loaded.iloc[0]["close"]) == 99.0


def test_save_daily_data_raises_when_no_valid_bars(tmp_path, monkeypatch):
    monkeypatch.delenv("MARKETHELM_DATABASE_URL", raising=False)
    storage = DataStorage(data_dir=str(tmp_path))
    with pytest.raises(ValueError, match="No valid market bars"):
        storage.save_daily_data([{"symbol": "AAPL", "close": float("nan")}])


def test_upsert_accepts_price_alias_and_outcome_final_text(app_db):
    written = upsert_market_bars(
        [
            "not-a-dict",
            {"symbol": " aapl ", "price": 150.0, "outcome_final": "yes"},
            {"symbol": "MSFT", "close": 400.0, "outcome_final": "no"},
        ],
        "2026-09-18",
    )
    assert written == 2
    rows = {row["symbol"]: row for row in load_market_bars("2026-09-18")}
    assert rows["AAPL"]["close"] == 150.0
    assert rows["AAPL"]["outcome_final"] == 1
    assert rows["MSFT"]["outcome_final"] == 0


def test_upsert_rejects_invalid_trade_date(app_db):
    with pytest.raises(ValueError, match="Invalid trade_date"):
        upsert_market_bars([{"symbol": "AAPL", "close": 1.0}], "not-a-date")


def test_load_market_bars_returns_empty_for_invalid_trade_date(app_db):
    upsert_market_bars([{"symbol": "AAPL", "close": 1.0}], "2026-09-18")
    assert load_market_bars("not-a-date") == []
    assert load_market_bars("") == []

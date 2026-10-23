"""Fetch/CLI writes must land in DATA_DIR so the dashboard and worker can read them."""

from datetime import date
from pathlib import Path

from dashboard.backend.services.data_loader import _default_data_dir
from src.storage.data_storage import DataStorage
from src.storage.projections_store import load_daily_summary, load_projections


def test_datastorage_uses_data_dir_env_when_unspecified(monkeypatch, tmp_path):
    target = tmp_path / "persistent-market-data"
    target.mkdir()
    monkeypatch.setenv("DATA_DIR", str(target))

    storage = DataStorage()

    assert storage.data_dir == target


def test_explicit_data_dir_wins_over_env(monkeypatch, tmp_path):
    env_dir = tmp_path / "from-env"
    explicit = tmp_path / "explicit"
    env_dir.mkdir()
    explicit.mkdir()
    monkeypatch.setenv("DATA_DIR", str(env_dir))

    storage = DataStorage(data_dir=str(explicit))

    assert storage.data_dir == explicit


def test_datastorage_defaults_to_local_data_without_env(monkeypatch, tmp_path):
    monkeypatch.delenv("DATA_DIR", raising=False)
    monkeypatch.chdir(tmp_path)

    storage = DataStorage()

    assert storage.data_dir == Path("data")
    assert (tmp_path / "data").is_dir()


def test_fetch_writes_and_dashboard_reads_the_same_data_dir(monkeypatch, tmp_path):
    target = tmp_path / "var-lib-markethelm-data"
    target.mkdir()
    monkeypatch.setenv("DATA_DIR", str(target))

    storage = DataStorage()
    saved = storage.save_daily_data(
        [{"symbol": "AAPL", "name": "Apple", "close": 150.0}],
    )

    assert saved == f"market_bars:{saved.split(':', 1)[1]}"
    assert saved.startswith("market_bars:")
    assert _default_data_dir() == target.resolve()
    assert (target / "market_bars.sqlite").is_file()
    assert list(target.glob("daily_data_*.csv")) == []
    loaded = storage.load_daily_data()
    assert loaded is not None
    assert float(loaded.iloc[0]["close"]) == 150.0


def test_blank_data_dir_env_falls_back_to_local_data(monkeypatch, tmp_path):
    """Empty DATA_DIR must not write to '' — same falsy fallback the dashboard uses."""
    monkeypatch.setenv("DATA_DIR", "")
    monkeypatch.chdir(tmp_path)

    storage = DataStorage()

    assert storage.data_dir == Path("data")
    assert (tmp_path / "data").is_dir()


def test_summary_and_projections_write_to_data_dir(monkeypatch, tmp_path):
    """Fetch New also persists summary and projections beside market bars."""

    target = tmp_path / "var-lib-markethelm-data"
    target.mkdir()
    monkeypatch.setenv("DATA_DIR", str(target))

    storage = DataStorage()
    stamp = date(2026, 8, 28)
    summary = storage.save_summary({"total_stocks": 1}, date=stamp)
    projections = storage.save_projections(
        {
            "AAPL": {
                "symbol": "AAPL",
                "name": "Apple",
                "current_price": 150.0,
                "target_low": 145.0,
                "target_mid": 155.0,
                "target_high": 160.0,
                "expected_change_percent": 3.3,
                "recommendation": "BUY",
                "confidence": 80,
                "trend": "Bullish",
                "momentum_score": 0.5,
                "volatility_score": 0.2,
                "risk_level": "Low",
                "reason": "momentum",
                "projection_date": "2026-09-02",
                "generated_at": "2026-08-28T12:00:00",
            }
        },
        date=stamp,
    )

    assert summary == "summary:2026-08-28"
    assert projections == "projections:2026-08-28"
    assert list(target.glob("summary_*.json")) == []
    assert list(target.glob("projections_*.csv")) == []
    assert load_daily_summary("2026-08-28", data_dir=target)["total_stocks"] == 1
    assert load_projections("2026-08-28", data_dir=target)[0]["symbol"] == "AAPL"
    assert (target / "projections_2026-08-28.md").is_file()
    assert _default_data_dir() == target.resolve()

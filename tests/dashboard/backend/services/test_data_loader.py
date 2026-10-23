"""Tests for dashboard data loader service."""

import shutil
import tempfile
from datetime import date, timedelta
from pathlib import Path

import pandas as pd
import pytest

import dashboard.backend.services.data_loader as dl
from dashboard.backend.services.data_loader import DataLoader, _is_weekday
from tests.helpers.market_bars import (
    seed_daily_bars,
    seed_projections,
    seed_simple_bars,
    seed_summary,
)


@pytest.fixture
def temp_data_dir():
    """Create temp data directory."""
    tmp = tempfile.mkdtemp()
    yield Path(tmp)
    shutil.rmtree(tmp, ignore_errors=True)


@pytest.fixture
def loader(temp_data_dir, monkeypatch):
    """Create DataLoader with temp directory (file-mode market_bars sidecar)."""
    monkeypatch.delenv("MARKETHELM_DATABASE_URL", raising=False)

    return DataLoader(data_dir=temp_data_dir)


class TestDataLoader:
    """Test DataLoader class."""

    def test_raises_for_missing_data_dir(self):
        """DataLoader raises when data directory does not exist."""

        with pytest.raises(ValueError, match="Data directory not found"):
            DataLoader(data_dir=Path("/nonexistent/path"))

    def test_get_latest_date_returns_none_when_empty(self, loader):
        """get_latest_date returns None when no daily data files."""
        assert loader.get_latest_date() is None

    def test_load_daily_data_raises_when_no_files(self, loader):
        """load_daily_data raises when no files exist."""
        with pytest.raises(ValueError, match="No daily data found"):
            loader.load_daily_data()

    def test_load_summary_raises_when_no_files(self, loader):
        """load_summary raises when no summary files exist."""
        with pytest.raises(ValueError, match="No summary files found"):
            loader.load_summary()

    def test_load_daily_data_returns_dataframe(self, loader, temp_data_dir):
        """load_daily_data returns correct DataFrame."""
        seed_daily_bars(
            temp_data_dir,
            "2026-01-15",
            [
                {"symbol": "AAPL", "close": 150.0, "change_percent": 1.0},
                {"symbol": "GOOGL", "close": 2800.0, "change_percent": -0.5},
            ],
        )

        result = loader.load_daily_data()
        assert isinstance(result, pd.DataFrame)
        assert len(result) == 2
        assert "symbol" in result.columns

    def test_load_summary_returns_dict(self, loader, temp_data_dir):
        """load_summary returns correct dict."""
        seed_summary(temp_data_dir, "2026-01-15", {"date": "2026-01-15", "analysis": {}})

        result = loader.load_summary()
        assert isinstance(result, dict)
        assert result["date"] == "2026-01-15"

    def test_load_summary_raises_value_error_when_missing(self, loader, temp_data_dir):
        """Missing summary dates raise ValueError so APIs can map to 404."""
        with pytest.raises(ValueError, match="No summary files found"):
            loader.load_summary()

    def test_load_daily_data_raises_when_date_missing(self, loader, temp_data_dir):
        """Missing trade date raises ValueError so APIs can map to 404."""
        seed_simple_bars(temp_data_dir, "2026-01-15", close=100.0)
        with pytest.raises(ValueError, match="Daily data not found for date"):
            loader.load_daily_data("2099-01-01")

    def test_get_latest_date_returns_date_string(self, loader, temp_data_dir):
        """get_latest_date returns date from most recent file by filename date."""
        seed_simple_bars(temp_data_dir, "2026-01-20", close=100.0)
        seed_simple_bars(temp_data_dir, "2026-01-15", close=100.0)

        result = loader.get_latest_date()
        assert result == "2026-01-20"

    def test_get_latest_date_uses_trade_date_order(self, loader, temp_data_dir):
        """get_latest_date uses trade_date, not insert order."""
        seed_simple_bars(temp_data_dir, "2026-01-20", close=100.0)
        seed_simple_bars(temp_data_dir, "2026-01-15", close=100.0)

        result = loader.get_latest_date()
        assert result == "2026-01-20"

    def test_get_available_dates_returns_sorted_list(self, loader, temp_data_dir):
        """get_available_dates returns sorted list of dates."""
        seed_simple_bars(temp_data_dir, "2026-01-10", close=100.0)
        seed_simple_bars(temp_data_dir, "2026-01-15", close=100.0)

        result = loader.get_available_dates()
        assert len(result) == 2
        assert result == sorted(result, reverse=True)

    def test_load_daily_data_loads_latest_trade_date(self, loader, temp_data_dir):
        """load_daily_data loads the newest trade_date by default."""
        seed_daily_bars(temp_data_dir, "2026-01-20", [{"symbol": "NEW", "close": 100.0}])
        seed_daily_bars(temp_data_dir, "2026-01-15", [{"symbol": "OLD", "close": 50.0}])

        result = loader.load_daily_data()
        assert result.iloc[0]["symbol"] == "NEW"

    def test_compute_projection_accuracy_matches_actual(self, loader, temp_data_dir):
        """Reports exact-session error, direction, band, and calibration metrics."""
        seed_daily_bars(temp_data_dir, "2026-01-05", [{"symbol": "AAPL", "close": 100.0}])
        seed_daily_bars(temp_data_dir, "2026-01-12", [{"symbol": "AAPL", "close": 105.0}])
        seed_projections(
            temp_data_dir,
            "2026-01-05",
            [
                {
                    "symbol": "AAPL",
                    "current_price": 100.0,
                    "target_low": 104.0,
                    "target_mid": 110.0,
                    "target_high": 112.0,
                    "recommendation": "BUY",
                    "confidence": 70,
                }
            ],
        )

        out = loader.compute_projection_accuracy(days=90)
        summary = out["summary"]

        assert summary["calendar"] == "XNYS"
        assert summary["horizonSessions"] == 5
        assert summary["sampleCount"] == 1
        assert summary["meanAbsErrorPct"] == 4.762
        assert summary["directionalAccuracyPct"] == 100.0
        assert summary["bandCoveragePct"] == 100.0
        assert summary["calibrationGapPct"] == -30.0
        assert summary["evaluationCoveragePct"] == 100.0
        assert out["samples"][0]["targetDate"] == "2026-01-12"
        assert out["samples"][0]["actualDate"] == "2026-01-12"
        assert out["samples"][0]["symbol"] == "AAPL"
        assert "BUY" in summary["byRecommendation"]
        assert "70-79" in summary["byConfidenceBand"]

    def test_compute_projection_accuracy_does_not_roll_missing_close_forward(
        self, loader, temp_data_dir
    ):
        """A later close cannot turn an exact-session gap into a variable-horizon score."""
        seed_daily_bars(temp_data_dir, "2026-01-05", [{"symbol": "AAPL", "close": 100.0}])
        seed_daily_bars(temp_data_dir, "2026-01-13", [{"symbol": "AAPL", "close": 108.0}])
        seed_projections(
            temp_data_dir,
            "2026-01-05",
            [{"symbol": "AAPL", "current_price": 100.0, "target_mid": 110.0}],
        )

        out = loader.compute_projection_accuracy(days=90)

        assert out["summary"]["sampleCount"] == 0
        assert out["summary"]["missingActualCount"] == 1
        assert out["summary"]["evaluationCoveragePct"] == 0.0
        assert out["samples"] == []

    def test_compute_projection_accuracy_counts_pending_and_invalid(self, loader, temp_data_dir):
        """Unmatured and malformed projections remain visible in report coverage."""
        seed_daily_bars(temp_data_dir, "2026-01-05", [{"symbol": "AAPL", "close": 100.0}])
        seed_projections(
            temp_data_dir,
            "2026-01-05",
            [
                {"symbol": "AAPL", "target_mid": 120.0, "recommendation": "BUY"},
                {"symbol": "MSFT", "target_mid": None, "recommendation": "HOLD"},
            ],
        )

        out = loader.compute_projection_accuracy(days=90)

        assert out["summary"]["projectionCount"] == 2
        assert out["summary"]["validProjectionCount"] == 1
        assert out["summary"]["pendingCount"] == 1
        assert out["summary"]["invalidCount"] == 1
        assert out["summary"]["sampleCount"] == 0

    def test_load_projections_raises_value_error_when_missing(self, loader, temp_data_dir):
        """Missing projections raise ValueError (API 404 mapping)."""
        with pytest.raises(ValueError, match="No projection files found"):
            loader.load_projections()

    def test_compute_projection_accuracy_normalizes_padded_and_skips_sentinels(
        self, loader, temp_data_dir
    ):
        """Padded symbols match daily rows; None/NaN never become NONE/NAN samples."""
        seed_daily_bars(
            temp_data_dir,
            "2026-01-12",
            [
                {"symbol": "AAPL", "close": 100.0, "change_percent": 0.0},
                {"symbol": "  msft  ", "close": 200.0, "change_percent": 0.0},
            ],
        )
        # Invalid/blank symbols are rejected by the store; only AAPL/MSFT persist.
        seed_projections(
            temp_data_dir,
            "2026-01-05",
            [
                {"symbol": " aapl ", "target_mid": 110.0, "recommendation": "BUY"},
                {"symbol": None, "target_mid": 50.0, "recommendation": "HOLD"},
                {"symbol": float("nan"), "target_mid": 60.0, "recommendation": "HOLD"},
                {"symbol": "  ", "target_mid": 70.0, "recommendation": "HOLD"},
                {"symbol": "MSFT", "target_mid": 210.0, "recommendation": "SELL"},
            ],
        )
        seed_daily_bars(
            temp_data_dir,
            "2026-01-05",
            [
                {"symbol": "AAPL", "close": 95.0, "change_percent": 0.0},
            ],
        )

        out = loader.compute_projection_accuracy(days=90)

        assert out["summary"]["sampleCount"] == 2
        symbols = {s["symbol"] for s in out["samples"]}
        assert symbols == {"AAPL", "MSFT"}
        assert "NONE" not in symbols
        assert "NAN" not in symbols
        by_sym = {s["symbol"]: s for s in out["samples"]}
        assert by_sym["AAPL"]["absErrorPct"] == 10.0
        assert by_sym["MSFT"]["absErrorPct"] == 5.0
        assert out["summary"]["invalidCount"] == 0

    def test_load_historical_data_matches_padded_symbols_and_skips_sentinels(
        self, loader, temp_data_dir
    ):
        """Historical lookup must normalize symbols and reject blank/sentinel keys."""

        recent = (date.today() - timedelta(days=1)).isoformat()
        seed_daily_bars(
            temp_data_dir,
            recent,
            [
                {
                    "symbol": " AAPL ",
                    "close": 155.0,
                    "change_percent": 0.5,
                    "volume": 1_000,
                },
            ],
        )
        seed_projections(
            temp_data_dir,
            recent,
            [
                {
                    "symbol": "aapl",
                    "target_mid": 165.0,
                    "confidence": 70,
                    "recommendation": "BUY",
                    "expected_change_percent": 3.0,
                }
            ],
        )

        rows = loader.load_historical_data(" aapl ", days=7)
        assert len(rows) == 1
        assert rows[0]["date"] == recent
        assert rows[0]["close"] == 155.0
        assert rows[0]["projection"]["target_price"] == 165.0

        assert loader.load_historical_data(None, days=7) == []
        assert loader.load_historical_data(float("nan"), days=7) == []
        assert loader.load_historical_data("NONE", days=7) == []

    def test_get_latest_date_falls_back_when_only_weekends(self, loader, temp_data_dir):
        """If every bar date is a weekend, still return the newest one."""
        seed_simple_bars(temp_data_dir, "2026-01-17", symbol="A", close=1.0)
        seed_simple_bars(temp_data_dir, "2026-01-18", symbol="A", close=1.0)

        assert loader.get_latest_date() == "2026-01-18"

    def test_get_latest_date_skips_weekend_files(self, loader, temp_data_dir):
        """Prefer Friday over newer Saturday/Sunday bar dates."""
        seed_simple_bars(temp_data_dir, "2026-01-16", symbol="A", close=1.0)  # Fri
        seed_simple_bars(temp_data_dir, "2026-01-17", symbol="A", close=1.0)  # Sat
        seed_simple_bars(temp_data_dir, "2026-01-18", symbol="A", close=1.0)  # Sun

        assert loader.get_latest_date() == "2026-01-16"
        loaded = loader.load_daily_data()
        assert list(loaded["symbol"]) == ["A"]

    def test_get_most_recent_trading_day_weekend_rolls_to_friday(self, monkeypatch):
        """Saturday/Sunday map to the prior Friday."""

        class _Sat:
            @classmethod
            def now(cls):
                class _N:
                    @staticmethod
                    def date():
                        return date(2026, 1, 17)  # Saturday

                return _N()

        class _Sun:
            @classmethod
            def now(cls):
                class _N:
                    @staticmethod
                    def date():
                        return date(2026, 1, 18)  # Sunday

                return _N()

        monkeypatch.setattr(dl, "datetime", _Sat)
        assert dl.get_most_recent_trading_day() == "2026-01-16"
        monkeypatch.setattr(dl, "datetime", _Sun)
        assert dl.get_most_recent_trading_day() == "2026-01-16"

    def test_is_weekday_and_unparseable_dates(self):
        """Weekdays are open; weekends closed; unparseable dates stay kept."""

        assert _is_weekday("2026-01-16") is True  # Friday
        assert _is_weekday("2026-01-17") is False  # Saturday
        assert _is_weekday("2026-01-18") is False  # Sunday
        assert _is_weekday("not-a-date") is True

    def test_load_historical_data_merges_projection_and_skips_gaps(
        self, loader, temp_data_dir, monkeypatch
    ):
        """Attach projection fields when present; skip missing/broken dates."""

        class _Now:
            @classmethod
            def now(cls):
                class _N:
                    @staticmethod
                    def date():
                        return date(2026, 1, 20)

                    def __sub__(self, other):
                        return date(2026, 1, 20) - other

                return _N()

        monkeypatch.setattr(dl, "datetime", _Now)

        seed_daily_bars(
            temp_data_dir,
            "2026-01-16",
            [
                {"symbol": "AAPL", "close": 100.0, "change_percent": 0.0},
            ],
        )
        seed_daily_bars(
            temp_data_dir,
            "2026-01-15",
            [
                {"symbol": "MSFT", "close": 200.0, "change_percent": 0.0},
            ],
        )
        # Dates with no bars for AAPL are skipped (gap days)
        seed_projections(
            temp_data_dir,
            "2026-01-16",
            [
                {
                    "symbol": "AAPL",
                    "target_mid": 110.0,
                    "confidence": 70,
                    "recommendation": "BUY",
                    "expected_change_percent": 5.0,
                }
            ],
        )

        rows = loader.load_historical_data("AAPL", days=30)
        assert len(rows) == 1
        assert rows[0]["date"] == "2026-01-16"
        assert rows[0]["projection"]["target_price"] == 110.0
        assert rows[0]["projection"]["recommendation"] == "BUY"

    def test_needs_fetch_for_latest_trading_day(self, loader, temp_data_dir, monkeypatch):
        """True when latest trading day is missing; false when present."""

        class _Fri:
            @classmethod
            def now(cls):
                class _N:
                    @staticmethod
                    def date():
                        return date(2026, 1, 16)

                return _N()

        monkeypatch.setattr(dl, "datetime", _Fri)
        seed_simple_bars(temp_data_dir, "2026-01-15", symbol="A", close=1.0)
        assert loader.needs_fetch_for_latest_trading_day() is True

        seed_simple_bars(temp_data_dir, "2026-01-16", symbol="A", close=1.0)
        assert loader.needs_fetch_for_latest_trading_day() is False

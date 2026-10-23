"""Tests for storage module."""

import shutil
import tempfile
import unittest
import unittest.mock
from datetime import date, datetime
from pathlib import Path
from unittest.mock import patch

import pandas as pd

from src.storage.data_storage import DataStorage, _data_date_for_filename
from src.storage.projections_store import load_daily_summary, load_projections, projections_frame


class TestDataDateForFilename:
    """Write-path collection date used when save callers omit an explicit date."""

    def test_weekday_uses_today(self):
        # 2026-07-22 is a Wednesday.
        with patch("src.storage.data_storage.datetime") as mock_dt:
            mock_dt.now.return_value = datetime(2026, 7, 22, 12, 0, 0)
            assert _data_date_for_filename() == date(2026, 7, 22)

    def test_saturday_keeps_collection_date(self):
        with patch("src.storage.data_storage.datetime") as mock_dt:
            mock_dt.now.return_value = datetime(2026, 7, 25, 9, 0, 0)
            assert _data_date_for_filename() == date(2026, 7, 25)

    def test_sunday_keeps_collection_date(self):
        with patch("src.storage.data_storage.datetime") as mock_dt:
            mock_dt.now.return_value = datetime(2026, 7, 26, 9, 0, 0)
            assert _data_date_for_filename() == date(2026, 7, 26)

    def test_save_daily_data_uses_trade_date_key(self, tmp_path):
        storage = DataStorage(data_dir=str(tmp_path))
        with patch(
            "src.storage.data_storage._data_date_for_filename",
            return_value=date(2026, 7, 24),
        ):
            path = storage.save_daily_data([{"symbol": "AAPL", "name": "Apple", "close": 150.0}])
        assert path == "market_bars:2026-07-24"

    def test_save_daily_data_empty_returns_none(self, tmp_path):
        storage = DataStorage(data_dir=str(tmp_path))
        assert storage.save_daily_data([]) is None
        assert list(tmp_path.glob("daily_data_*.csv")) == []
        assert list(tmp_path.glob("market_bars.sqlite")) == []


class TestDataStorage(unittest.TestCase):
    """Test cases for data storage."""

    def setUp(self):
        """Set up test fixtures."""
        self.test_data_dir = tempfile.mkdtemp()
        self.storage = DataStorage(data_dir=self.test_data_dir)
        self.sample_df = pd.DataFrame(
            {"symbol": ["AAPL", "GOOGL"], "close": [150.0, 2800.0], "volume": [50000000, 30000000]}
        )
        self.sample_summary = {"total_stocks": 2, "date": str(date.today())}

    def tearDown(self):
        """Clean up test fixtures."""
        shutil.rmtree(self.test_data_dir, ignore_errors=True)

    def test_storage_initialization(self):
        """Test that storage initializes correctly."""
        self.assertIsInstance(self.storage, DataStorage)
        self.assertTrue(Path(self.test_data_dir).exists())

    def test_save_daily_data(self):
        """Test saving daily data to market_bars."""
        data_list = self.sample_df.to_dict("records")
        location = self.storage.save_daily_data(data_list)
        self.assertTrue(str(location).startswith("market_bars:"))
        self.assertEqual(list(Path(self.test_data_dir).glob("daily_data_*.csv")), [])

    def test_save_summary(self):
        """Test saving summary to durable daily_summaries storage."""

        location = self.storage.save_summary(self.sample_summary, date=date(2026, 1, 15))
        self.assertEqual(location, "summary:2026-01-15")
        self.assertEqual(list(Path(self.test_data_dir).glob("summary_*.json")), [])
        loaded = load_daily_summary("2026-01-15", data_dir=self.test_data_dir)
        self.assertIsInstance(loaded, dict)
        self.assertEqual(loaded["total_stocks"], 2)

    def test_load_daily_data(self):
        """Test loading daily data from market_bars."""
        data_list = self.sample_df.to_dict("records")
        self.storage.save_daily_data(data_list)
        loaded_df = self.storage.load_daily_data()
        self.assertIsInstance(loaded_df, pd.DataFrame)
        self.assertEqual(len(loaded_df), 2)
        self.assertIn("symbol", loaded_df.columns)

    def test_load_summary(self):
        """Test loading summary from durable storage after save."""

        self.storage.save_summary(self.sample_summary, date=date(2026, 1, 15))
        self.assertEqual(list(Path(self.test_data_dir).glob("summary_*.json")), [])
        loaded_summary = load_daily_summary("2026-01-15", data_dir=self.test_data_dir)
        self.assertIsInstance(loaded_summary, dict)
        self.assertEqual(loaded_summary["total_stocks"], 2)

    def test_save_projections_writes_db_and_markdown(self):
        """Projections land in DB (no CSV) and still emit optional markdown."""

        projections = {
            "AAPL": {
                "symbol": "AAPL",
                "name": "Apple",
                "current_price": 180.0,
                "target_low": 175.0,
                "target_mid": 185.0,
                "target_high": 195.0,
                "expected_change_percent": 2.5,
                "recommendation": "BUY",
                "confidence": 80,
                "trend": "up",
                "momentum_score": 0.6,
                "volatility_score": 0.2,
                "risk_level": "medium",
                "reason": "momentum",
                "projection_date": "2026-05-25",
                "projection_horizon_sessions": 5,
                "projection_calendar": "XNYS",
                "generated_at": "2026-05-20T12:00:00",
                "extra_ignored": True,
            }
        }

        location = self.storage.save_projections(projections, date=date(2026, 5, 20))
        self.assertEqual(location, "projections:2026-05-20")
        self.assertEqual(list(Path(self.test_data_dir).glob("projections_*.csv")), [])
        rows = load_projections("2026-05-20", data_dir=self.test_data_dir)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["symbol"], "AAPL")
        df = projections_frame("2026-05-20", data_dir=self.test_data_dir)
        for col in (
            "symbol",
            "name",
            "current_price",
            "target_low",
            "target_mid",
            "target_high",
            "expected_change_percent",
            "recommendation",
            "confidence",
            "trend",
            "momentum_score",
            "volatility_score",
            "risk_level",
            "reason",
            "projection_date",
            "projection_horizon_sessions",
            "projection_calendar",
            "generated_at",
        ):
            self.assertIn(col, df.columns)
        md_path = Path(self.test_data_dir) / "projections_2026-05-20.md"
        self.assertTrue(md_path.exists())
        self.assertIn("Stock Market Projections Report", md_path.read_text(encoding="utf-8"))

    def test_save_projections_still_returns_key_when_markdown_fails(self):
        """Markdown report failures must not block durable projection persistence."""

        projections = {
            "AAPL": {
                "symbol": "AAPL",
                "current_price": 180.0,
                "expected_change_percent": 1.0,
                "recommendation": "HOLD",
                "confidence": 50,
                "trend": "flat",
            }
        }

        with unittest.mock.patch.object(
            DataStorage,
            "_generate_projection_markdown",
            side_effect=RuntimeError("markdown boom"),
        ):
            location = self.storage.save_projections(projections, date=date(2026, 5, 21))

        self.assertEqual(location, "projections:2026-05-21")
        self.assertFalse((Path(self.test_data_dir) / "projections_2026-05-21.md").exists())
        self.assertEqual(list(Path(self.test_data_dir).glob("projections_*.csv")), [])
        self.assertEqual(
            load_projections("2026-05-21", data_dir=self.test_data_dir)[0]["symbol"],
            "AAPL",
        )

    def test_save_projections_empty_returns_none(self):
        self.assertIsNone(self.storage.save_projections({}))

    def test_load_daily_data_missing_date_returns_none(self):
        """Absent trade date soft-fails to None instead of raising."""
        self.assertIsNone(self.storage.load_daily_data(trade_date=date(2099, 1, 1)))

    def test_load_daily_data_empty_store_returns_none(self):
        """Empty market_bars store soft-fails to None."""
        self.assertIsNone(self.storage.load_daily_data())


if __name__ == "__main__":
    unittest.main()

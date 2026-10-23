"""Projection date coercion and missing-projection soft-fails for DataLoader."""

import shutil
import tempfile
from datetime import date, timedelta
from pathlib import Path

import pytest

from dashboard.backend.services.data_loader import DataLoader
from tests.helpers.market_bars import seed_daily_bars, seed_projections, seed_simple_bars


@pytest.fixture
def temp_data_dir(monkeypatch):
    monkeypatch.delenv("MARKETHELM_DATABASE_URL", raising=False)
    tmp = tempfile.mkdtemp()
    yield Path(tmp)
    shutil.rmtree(tmp, ignore_errors=True)


@pytest.fixture
def loader(temp_data_dir):
    return DataLoader(data_dir=temp_data_dir)


class TestProjectionAccuracyInvalidDates:
    def test_compute_projection_accuracy_ignores_legacy_bad_projection_date(
        self, loader, temp_data_dir
    ):
        """The shared evaluator derives the exact session instead of trusting a dirty field."""
        seed_daily_bars(temp_data_dir, "2026-01-05", [{"symbol": "AAPL", "close": 100.0}])
        seed_daily_bars(temp_data_dir, "2026-01-12", [{"symbol": "AAPL", "close": 110.0}])
        seed_projections(
            temp_data_dir,
            "2026-01-05",
            [
                {
                    "symbol": "AAPL",
                    "target_mid": 100.0,
                    "recommendation": "HOLD",
                    "projection_date": "not-a-date",
                }
            ],
        )

        out = loader.compute_projection_accuracy(days=90)

        assert out["summary"]["sampleCount"] == 1
        sample = out["samples"][0]
        assert sample["targetDate"] == "2026-01-12"
        assert sample["actualDate"] == "2026-01-12"
        assert sample["absErrorPct"] == 9.091


class TestProjectionAccuracyRecommendationSentinels:
    def test_nan_recommendation_becomes_unknown(self, loader, temp_data_dir):
        """NaN recommendation cells must not create a \"nan\" accuracy bucket."""
        seed_daily_bars(temp_data_dir, "2026-01-05", [{"symbol": "AAPL", "close": 100.0}])
        seed_daily_bars(temp_data_dir, "2026-01-12", [{"symbol": "AAPL", "close": 110.0}])
        seed_projections(
            temp_data_dir,
            "2026-01-05",
            [
                {
                    "symbol": "AAPL",
                    "target_mid": 100.0,
                    "recommendation": float("nan"),
                    "projection_date": "2026-01-12",
                }
            ],
        )

        out = loader.compute_projection_accuracy(days=90)

        assert out["summary"]["sampleCount"] == 1
        assert out["samples"][0]["recommendation"] == "UNKNOWN"
        assert "nan" not in {k.lower() for k in out["summary"]["byRecommendation"]}
        assert "UNKNOWN" in out["summary"]["byRecommendation"]
        assert out["summary"]["byRecommendation"]["UNKNOWN"]["count"] == 1


class TestHistoricalMissingProjections:
    def test_load_historical_data_keeps_daily_when_projections_missing(self, loader, temp_data_dir):
        """Missing projections for a bar date must not hide valid daily history."""
        recent = (date.today() - timedelta(days=1)).isoformat()
        seed_simple_bars(
            temp_data_dir,
            recent,
            close=155.0,
            change_percent=0.5,
            volume=1_000,
        )

        rows = loader.load_historical_data("AAPL", days=7)

        assert len(rows) == 1
        assert rows[0]["date"] == recent
        assert rows[0]["close"] == 155.0
        assert "projection" not in rows[0]

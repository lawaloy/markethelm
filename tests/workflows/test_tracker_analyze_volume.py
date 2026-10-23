"""Tests for tracker index comparison volume/change finite guards."""

from unittest.mock import MagicMock, patch

import pytest

from src.workflows.tracker import StockTrackerWorkflow


@pytest.fixture
def workflow():
    """StockTrackerWorkflow with heavy deps mocked out."""
    with (
        patch("src.workflows.tracker.get_indices_to_track", return_value=["S&P 500"]),
        patch("src.workflows.tracker.StockDataFetcher"),
        patch("src.workflows.tracker.DataStorage"),
        patch("src.workflows.tracker.AlertEngine") as mock_alert,
    ):
        mock_alert.from_config.return_value = None

        instance = StockTrackerWorkflow(include_profile=False)
        instance.analyzer.analyze_daily_data = MagicMock(return_value={"ok": True})
        yield instance


class TestAnalyzeDataVolumeGuards:
    def test_inf_volume_does_not_wipe_index_comparison(self, workflow):
        """Inf volume previously OverflowError'd int() and emptied analysis."""
        result = workflow._analyze_data(
            all_data=[{"symbol": "AAPL", "close": 150.0, "volume": 1_000_000}],
            index_data={
                "S&P 500": [
                    {
                        "symbol": "AAPL",
                        "change_percent": 1.5,
                        "volume": 1_000_000,
                    },
                    {
                        "symbol": "BAD",
                        "change_percent": 2.0,
                        "volume": float("inf"),
                    },
                ]
            },
        )

        assert result["analysis"] == {"ok": True}
        comparison = result["index_comparison"]["S&P 500"]
        assert comparison["total_volume"] == 1_000_000
        assert comparison["stock_count"] == 2
        assert comparison["average_change_percent"] == 1.75
        assert comparison["gainers"] == 2
        assert comparison["losers"] == 0

    def test_dirty_volume_and_nan_change_keep_finite_peers(self, workflow):
        result = workflow._analyze_data(
            all_data=[],
            index_data={
                "NASDAQ-100": [
                    {
                        "symbol": "MSFT",
                        "change_percent": -1.0,
                        "volume": 2_000_000,
                    },
                    {
                        "symbol": "BAD",
                        "change_percent": float("nan"),
                        "volume": "not-a-number",
                    },
                ]
            },
        )

        comparison = result["index_comparison"]["NASDAQ-100"]
        assert comparison["total_volume"] == 2_000_000
        assert comparison["average_change_percent"] == -1.0
        assert comparison["gainers"] == 0
        assert comparison["losers"] == 1

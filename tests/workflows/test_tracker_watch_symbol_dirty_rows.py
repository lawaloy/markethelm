"""Watch-symbol inclusion must ignore non-dict fetch rows without top-N."""

from unittest.mock import MagicMock, patch

import pytest

from src.workflows.tracker import StockTrackerWorkflow


@pytest.fixture
def fetch_workflow():
    with (
        patch("src.workflows.tracker.get_indices_to_track", return_value=["S&P 500"]),
        patch("src.workflows.tracker.StockDataFetcher"),
        patch("src.workflows.tracker.DataStorage"),
        patch("src.workflows.tracker.AlertEngine") as mock_alert,
    ):
        mock_alert.from_config.return_value = None

        workflow = StockTrackerWorkflow(include_profile=False)
        yield workflow


def test_fetch_watch_symbols_survives_non_dict_rows_without_top_n(
    fetch_workflow,
) -> None:
    """Without top-N, dirty rows stay in all_data and previously AttributeError'd."""
    mock_fetcher = MagicMock()
    mock_fetcher.fetch_all_indices.return_value = {
        "S&P 500": [
            "poison-row",
            {
                "symbol": "KEEP",
                "close": 10.0,
                "volume": 9_000,
                "index_name": "S&P 500",
            },
        ]
    }
    mock_fetcher.fetch_symbol_data.return_value = {
        "symbol": "WATCH",
        "close": 11.0,
        "volume": 100,
        "index_name": "WATCH",
    }
    fetch_workflow.fetcher = mock_fetcher

    with patch(
        "src.alerts.alert_paths.get_enabled_watch_symbols",
        return_value=["WATCH"],
    ):
        result = fetch_workflow._fetch_data(use_screener=False, top_n_stocks=None)

    assert result["success"] is True
    symbols = [row["symbol"] for row in result["data"] if isinstance(row, dict)]
    assert "KEEP" in symbols
    assert "WATCH" in symbols
    mock_fetcher.fetch_symbol_data.assert_called_once_with("WATCH")

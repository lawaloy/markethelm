"""Top-N fetch ranking must soft-fail dirty / non-finite volumes."""

from unittest.mock import MagicMock, patch

import pytest

from src.workflows.tracker import StockTrackerWorkflow, _volume_sort_key


@pytest.fixture
def fetch_workflow():
    with (
        patch("src.workflows.tracker.get_indices_to_track", return_value=["S&P 500"]),
        patch("src.workflows.tracker.StockDataFetcher"),
        patch("src.workflows.tracker.DataStorage"),
        patch("src.workflows.tracker.AlertEngine") as mock_alert,
        patch(
            "src.alerts.alert_paths.get_enabled_watch_symbols",
            return_value=[],
        ),
    ):
        mock_alert.from_config.return_value = None

        workflow = StockTrackerWorkflow(include_profile=False)
        yield workflow


def test_volume_sort_key_ranks_finite_and_demotes_poison() -> None:
    assert _volume_sort_key({"volume": 1_000}) == 1_000.0
    assert _volume_sort_key({"volume": None}) == float("-inf")
    assert _volume_sort_key({"volume": float("inf")}) == float("-inf")
    assert _volume_sort_key({"volume": float("nan")}) == float("-inf")
    assert _volume_sort_key({"volume": "bad"}) == float("-inf")
    assert _volume_sort_key("not-a-row") == float("-inf")


def test_fetch_top_n_skips_none_and_inf_volumes(fetch_workflow) -> None:
    """None volumes previously TypeError'd sorted(); Inf poisoned top-N picks."""
    mock_fetcher = MagicMock()
    mock_fetcher.fetch_all_indices.return_value = {
        "S&P 500": [
            {"symbol": "LOW", "close": 10.0, "volume": 100, "index_name": "S&P 500"},
            {
                "symbol": "NONE",
                "close": 11.0,
                "volume": None,
                "index_name": "S&P 500",
            },
            {
                "symbol": "INF",
                "close": 12.0,
                "volume": float("inf"),
                "index_name": "S&P 500",
            },
            {
                "symbol": "HIGH",
                "close": 13.0,
                "volume": 50_000,
                "index_name": "S&P 500",
            },
            {
                "symbol": "MID",
                "close": 14.0,
                "volume": 5_000,
                "index_name": "S&P 500",
            },
            {
                "symbol": "STR",
                "close": 15.0,
                "volume": "n/a",
                "index_name": "S&P 500",
            },
        ]
    }
    fetch_workflow.fetcher = mock_fetcher

    result = fetch_workflow._fetch_data(use_screener=False, top_n_stocks=2)

    assert result["success"] is True
    symbols = [row["symbol"] for row in result["data"]]
    assert symbols == ["HIGH", "MID"]


def test_fetch_top_n_survives_non_dict_rows(fetch_workflow) -> None:
    mock_fetcher = MagicMock()
    mock_fetcher.fetch_all_indices.return_value = {
        "S&P 500": [
            "poison-row",
            {"symbol": "KEEP", "close": 10.0, "volume": 9_000, "index_name": "S&P 500"},
            {"symbol": "DROP", "close": 11.0, "volume": 10, "index_name": "S&P 500"},
        ]
    }
    fetch_workflow.fetcher = mock_fetcher

    result = fetch_workflow._fetch_data(use_screener=False, top_n_stocks=1)

    assert result["success"] is True
    assert [row["symbol"] for row in result["data"]] == ["KEEP"]

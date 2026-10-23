"""Tracker must ignore non-positive top_n so negative slices cannot invert volume rank."""

from unittest.mock import MagicMock

import pytest

from src.workflows.tracker import StockTrackerWorkflow


@pytest.fixture
def fetch_workflow(monkeypatch):
    monkeypatch.setenv("FINNHUB_API_KEY", "test-key")
    monkeypatch.setattr(
        "src.workflows.tracker.get_indices_to_track",
        lambda: ["INDEX"],
    )
    monkeypatch.setattr(
        "src.alerts.alert_paths.get_enabled_watch_symbols",
        lambda: [],
    )

    workflow = StockTrackerWorkflow.__new__(StockTrackerWorkflow)
    workflow.fetcher = MagicMock()
    return workflow


@pytest.mark.parametrize("top_n", [0, -1, -5])
def test_nonpositive_top_n_keeps_full_universe(fetch_workflow, top_n):
    rows = [
        {"symbol": "LOW", "volume": 100},
        {"symbol": "HIGH", "volume": 9_000},
        {"symbol": "MID", "volume": 1_000},
    ]
    fetch_workflow.fetcher.fetch_all_indices.return_value = {"INDEX": rows}

    result = fetch_workflow._fetch_data(use_screener=False, top_n_stocks=top_n)

    assert result["success"] is True
    symbols = [row["symbol"] for row in result["data"]]
    assert symbols == ["LOW", "HIGH", "MID"]
    # Negative limits must not become per-index fetch caps either.
    kwargs = fetch_workflow.fetcher.fetch_all_indices.call_args.kwargs
    assert kwargs.get("max_symbols_per_index") is None

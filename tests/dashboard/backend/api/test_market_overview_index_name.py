"""Overview must skip blank/NaN index_name cells instead of 500ing."""

from unittest.mock import MagicMock, patch

import pandas as pd
from fastapi.testclient import TestClient

import dashboard.backend.api.market
from dashboard.backend.api.market import _overview_index_key
from dashboard.backend.main import app


def test_overview_index_key_skips_blank_and_nonfinite() -> None:
    assert _overview_index_key("S&P 500") == "S&P500"
    assert _overview_index_key(None) is None
    assert _overview_index_key(float("nan")) is None
    assert _overview_index_key(float("inf")) is None
    assert _overview_index_key("   ") is None
    assert _overview_index_key("nan") is None


def test_market_overview_skips_nan_and_none_index_names() -> None:
    """NaN/None index_name previously AttributeError'd on .replace → HTTP 500."""

    mock_loader = MagicMock()
    mock_loader.get_latest_date.return_value = "2026-01-15"
    mock_loader.load_daily_data.return_value = pd.DataFrame(
        {
            "symbol": ["AAPL", "MSFT", "GOOG"],
            "change_percent": [1.5, -0.5, 0.25],
            "index_name": ["S&P 500", float("nan"), None],
        }
    )
    with patch.object(dashboard.backend.api.market, "get_data_loader", return_value=mock_loader):
        client = TestClient(app)
        response = client.get("/api/market/overview")

    assert response.status_code == 200
    data = response.json()
    assert data["totalStocks"] == 3
    assert "S&P500" in data["indices"]
    assert data["indices"]["S&P500"]["stocks"] == 1
    # Corrupt index labels are omitted, not turned into "nan"/None keys.
    assert "nan" not in data["indices"]
    assert "None" not in data["indices"]

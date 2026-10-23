"""Market movers must soft-fail dirty CSV name cells (NaN/None → Pydantic 500)."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pandas as pd
import pytest
from fastapi.testclient import TestClient

import dashboard.backend.api.market
from dashboard.backend.main import app


@pytest.fixture
def client():
    return TestClient(app)


def test_market_movers_falls_back_when_name_is_nan(client) -> None:
    """float('nan') name previously failed StockMover str validation → 500."""

    mock_loader = MagicMock()
    mock_loader.load_daily_data.return_value = pd.DataFrame(
        {
            "symbol": ["AAPL", "MSFT"],
            "name": [float("nan"), None],
            "close": [150.0, 300.0],
            "change": [1.5, -3.0],
            "change_percent": [1.5, -2.0],
            "volume": [1_000, 2_000],
            "index_name": ["S&P 500", "S&P 500"],
        }
    )

    with patch.object(dashboard.backend.api.market, "get_data_loader", return_value=mock_loader):
        gainers = client.get("/api/market/movers", params={"type": "gainers", "limit": 10})
        losers = client.get("/api/market/movers", params={"type": "losers", "limit": 10})

    assert gainers.status_code == 200
    assert losers.status_code == 200
    gainer = gainers.json()["data"][0]
    loser = losers.json()["data"][0]
    assert gainer["symbol"] == "AAPL"
    assert gainer["name"] == "AAPL"
    assert loser["symbol"] == "MSFT"
    assert loser["name"] == "MSFT"

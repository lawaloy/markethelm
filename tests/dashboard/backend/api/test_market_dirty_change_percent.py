"""Market overview/movers must soft-fail non-numeric change_percent cells."""

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


def _dirty_daily() -> pd.DataFrame:
    return pd.DataFrame(
        {
            "symbol": ["AAPL", "MSFT", "BAD", "ZERO"],
            "name": ["Apple", "Microsoft", "Bad", "Zero"],
            "close": [150.0, 300.0, 10.0, 20.0],
            "change": [1.5, -3.0, 0.0, 0.0],
            "change_percent": [1.5, -2.0, "bad", "0"],
            "volume": [1_000, 2_000, 3_000, 4_000],
            "index_name": ["S&P 500", "S&P 500", "NASDAQ-100", "NASDAQ-100"],
        }
    )


def test_market_overview_skips_non_numeric_change_percent(client) -> None:
    """Object-dtype change_percent previously TypeError'd comparisons → 500."""

    mock_loader = MagicMock()
    mock_loader.get_latest_date.return_value = "2026-01-15"
    mock_loader.load_daily_data.return_value = _dirty_daily()

    with patch.object(dashboard.backend.api.market, "get_data_loader", return_value=mock_loader):
        r = client.get("/api/market/overview")

    assert r.status_code == 200
    data = r.json()
    assert data["totalStocks"] == 4
    assert data["gainers"] == 1
    assert data["losers"] == 1
    assert data["unchanged"] == 1
    # Mean over finite cells only: (1.5 + -2.0 + 0) / 3
    assert data["averageChange"] == pytest.approx((-0.5) / 3, abs=0.01)
    assert "S&P500" in data["indices"]
    assert data["indices"]["S&P500"]["gainers"] == 1
    assert data["indices"]["S&P500"]["losers"] == 1


def test_market_movers_skips_non_numeric_change_percent(client) -> None:
    """nlargest on object-dtype change_percent previously TypeError'd → 500."""

    mock_loader = MagicMock()
    mock_loader.load_daily_data.return_value = _dirty_daily()

    with patch.object(dashboard.backend.api.market, "get_data_loader", return_value=mock_loader):
        gainers = client.get("/api/market/movers", params={"type": "gainers", "limit": 10})
        losers = client.get("/api/market/movers", params={"type": "losers", "limit": 10})

    assert gainers.status_code == 200
    assert losers.status_code == 200
    gainer_symbols = [row["symbol"] for row in gainers.json()["data"]]
    loser_symbols = [row["symbol"] for row in losers.json()["data"]]
    assert gainer_symbols == ["AAPL"]
    assert loser_symbols == ["MSFT"]
    assert "BAD" not in gainer_symbols + loser_symbols

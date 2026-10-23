"""Stock detail must soft-fail dirty name and missing projection labels."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pandas as pd
import pytest
from fastapi.testclient import TestClient

import dashboard.backend.api.stocks
from dashboard.backend.main import app


@pytest.fixture
def client():
    return TestClient(app)


def test_stock_detail_falls_back_when_name_is_nan(client) -> None:
    """float('nan') name previously failed StockDetail str validation → 500."""

    mock_loader = MagicMock()
    mock_loader.get_latest_date.return_value = "2026-01-15"
    mock_loader.load_daily_data.return_value = pd.DataFrame(
        {
            "symbol": ["AAPL"],
            "name": [float("nan")],
            "close": [150.0],
            "change": [1.0],
            "change_percent": [0.7],
            "volume": [1_000],
        }
    )
    mock_loader.load_projections.return_value = pd.DataFrame()

    with patch.object(dashboard.backend.api.stocks, "get_data_loader", return_value=mock_loader):
        r = client.get("/api/stocks/AAPL")

    assert r.status_code == 200
    data = r.json()
    assert data["symbol"] == "AAPL"
    assert data["name"] == "AAPL"
    assert data["projection"] is None


def test_stock_detail_defaults_missing_projection_labels(client) -> None:
    """Missing risk/trend/recommendation must keep projection, not drop it."""

    mock_loader = MagicMock()
    mock_loader.get_latest_date.return_value = "2026-01-15"
    mock_loader.load_daily_data.return_value = pd.DataFrame(
        {
            "symbol": ["AAPL"],
            "name": ["Apple"],
            "close": [150.0],
            "change": [1.0],
            "change_percent": [0.7],
            "volume": [1_000],
        }
    )
    mock_loader.load_projections.return_value = pd.DataFrame(
        {
            "symbol": ["AAPL"],
            "target_mid": [160.0],
            "expected_change_percent": [5.0],
            "confidence": [90],
            "momentum_score": [1.2],
            "volatility_score": [0.3],
        }
    )

    with patch.object(dashboard.backend.api.stocks, "get_data_loader", return_value=mock_loader):
        r = client.get("/api/stocks/AAPL")

    assert r.status_code == 200
    data = r.json()
    assert data["name"] == "Apple"
    assert data["projection"] is not None
    assert data["projection"]["recommendation"] == "HOLD"
    assert data["projection"]["risk"] == "Unknown"
    assert data["projection"]["trend"] == "Neutral"
    assert data["projection"]["targetPrice"] == 160.0
    assert data["technical"] is not None
    assert data["technical"]["momentum"] == pytest.approx(1.2)

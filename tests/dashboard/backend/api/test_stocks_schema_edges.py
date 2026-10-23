"""Stock detail must 404 on malformed daily schema instead of KeyError→500."""

from unittest.mock import MagicMock, patch

import pandas as pd
import pytest
from fastapi.testclient import TestClient

import dashboard.backend.api.stocks
from dashboard.backend.main import app


@pytest.fixture
def client():
    return TestClient(app)


def test_stock_detail_404_when_daily_symbol_column_missing(client) -> None:
    mock_loader = MagicMock()
    mock_loader.get_latest_date.return_value = "2026-01-15"
    mock_loader.load_daily_data.return_value = pd.DataFrame(
        {
            "close": [150.0],
            "change": [1.0],
            "change_percent": [0.7],
            "volume": [1_000],
        }
    )

    with patch.object(dashboard.backend.api.stocks, "get_data_loader", return_value=mock_loader):
        r = client.get("/api/stocks/AAPL")

    assert r.status_code == 404
    assert r.json()["detail"] == "Stock not found."


def test_stock_detail_404_when_daily_frame_empty(client) -> None:
    mock_loader = MagicMock()
    mock_loader.get_latest_date.return_value = "2026-01-15"
    mock_loader.load_daily_data.return_value = pd.DataFrame()

    with patch.object(dashboard.backend.api.stocks, "get_data_loader", return_value=mock_loader):
        r = client.get("/api/stocks/AAPL")

    assert r.status_code == 404
    assert r.json()["detail"] == "Stock not found."

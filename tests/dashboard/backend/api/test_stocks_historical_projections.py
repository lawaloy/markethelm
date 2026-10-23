"""Historical series must soft-fail nested projections with non-finite numerics."""

from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

import dashboard.backend.api.stocks
from dashboard.backend.main import app


@pytest.fixture
def client():
    return TestClient(app)


def test_stock_historical_omits_projection_when_target_is_nan(client) -> None:
    """Finite price points must still return when nested projection numerics are NaN."""

    mock_loader = MagicMock()
    mock_loader.load_historical_data.return_value = [
        {
            "date": "2026-07-23",
            "close": 150.0,
            "change_percent": 1.2,
            "volume": 1_000,
            "projection": {
                "target_price": float("nan"),
                "confidence": 80,
                "recommendation": "BUY",
                "expected_change": 2.5,
            },
        },
        {
            "date": "2026-07-22",
            "close": 148.0,
            "change_percent": -0.5,
            "volume": 900,
            "projection": {
                "target_price": 160.0,
                "confidence": float("inf"),
                "recommendation": "BUY",
                "expected_change": 3.0,
            },
        },
        {
            "date": "2026-07-21",
            "close": 147.0,
            "change_percent": 0.4,
            "volume": 800,
            "projection": {
                "target_price": 155.0,
                "confidence": 70,
                "recommendation": "HOLD",
                "expected_change": 1.5,
            },
        },
    ]

    with patch.object(dashboard.backend.api.stocks, "get_data_loader", return_value=mock_loader):
        response = client.get("/api/stocks/AAPL/historical", params={"days": 7})

    assert response.status_code == 200
    body = response.json()
    assert body["symbol"] == "AAPL"
    by_date = {point["date"]: point for point in body["data"]}
    assert by_date["2026-07-23"]["projection"] is None
    assert by_date["2026-07-22"]["projection"] is None
    assert by_date["2026-07-21"]["projection"]["targetPrice"] == 155.0
    assert by_date["2026-07-21"]["projection"]["expectedChange"] == 1.5
    assert by_date["2026-07-21"]["projection"]["confidence"] == 70.0
    assert by_date["2026-07-21"]["projection"]["recommendation"] == "HOLD"
    # Response must be strict JSON (no NaN/Infinity literals from Pydantic null leakage).
    raw = response.text
    assert "NaN" not in raw
    assert "Infinity" not in raw

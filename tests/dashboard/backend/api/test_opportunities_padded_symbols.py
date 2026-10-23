"""Opportunities must join daily prices via normalize_ticker (padded symbols)."""

from __future__ import annotations

import shutil
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

import dashboard.backend.api.projections
from dashboard.backend.main import app
from dashboard.backend.services.data_loader import DataLoader
from tests.helpers.market_bars import seed_daily_bars, seed_projections


@pytest.fixture
def temp_data_dir(monkeypatch):
    monkeypatch.delenv("MARKETHELM_DATABASE_URL", raising=False)
    tmp = tempfile.mkdtemp()
    yield Path(tmp)
    shutil.rmtree(tmp, ignore_errors=True)


@pytest.fixture
def client(temp_data_dir):
    loader = DataLoader(data_dir=temp_data_dir)
    with patch.object(dashboard.backend.api.projections, "get_data_loader", return_value=loader):
        yield TestClient(app)


def test_opportunities_joins_padded_mixed_case_daily_symbols(client, temp_data_dir):
    """Projection AAPL must still resolve daily close/volume when symbol was padded."""
    seed_daily_bars(
        temp_data_dir,
        "2026-01-15",
        [
            {
                "symbol": " aapl ",
                "name": "Apple",
                "close": 151.25,
                "change": 1.0,
                "change_percent": 0.7,
                "volume": 42_000,
            }
        ],
    )
    seed_projections(
        temp_data_dir,
        "2026-01-15",
        [
            {
                "symbol": "AAPL",
                "name": "Apple",
                "target_mid": 160.0,
                "expected_change_percent": 5.0,
                "confidence": 90,
                "recommendation": "STRONG BUY",
                "risk_level": "Low",
                "trend": "Bullish",
                "momentum_score": 1.2,
                "volatility_score": 0.3,
                "reason": "momentum",
            }
        ],
    )

    r = client.get(
        "/api/projections/opportunities",
        params={"type": "STRONG_BUY", "limit": 10},
    )
    assert r.status_code == 200
    data = r.json()
    assert data["count"] == 1
    row = data["opportunities"][0]
    assert row["symbol"] == "AAPL"
    assert row["currentPrice"] == 151.25
    assert row["volume"] == 42_000


def test_opportunities_skips_sentinel_projection_symbols(client, temp_data_dir):
    """Blank/sentinel projection tickers must not appear as opportunity cards."""
    seed_daily_bars(
        temp_data_dir,
        "2026-01-15",
        [
            {
                "symbol": "AAPL",
                "name": "Apple",
                "close": 150.0,
                "change": 1.0,
                "change_percent": 0.7,
                "volume": 1_000,
            }
        ],
    )
    seed_projections(
        temp_data_dir,
        "2026-01-15",
        [
            {
                "symbol": "nan",
                "name": "Bogus",
                "target_mid": 160.0,
                "expected_change_percent": 5.0,
                "confidence": 99,
                "recommendation": "STRONG BUY",
                "risk_level": "Low",
                "trend": "Bullish",
                "momentum_score": 1.0,
                "volatility_score": 0.2,
                "reason": "x",
            },
            {
                "symbol": "AAPL",
                "name": "Apple",
                "target_mid": 160.0,
                "expected_change_percent": 4.0,
                "confidence": 80,
                "recommendation": "STRONG BUY",
                "risk_level": "Low",
                "trend": "Bullish",
                "momentum_score": 1.0,
                "volatility_score": 0.2,
                "reason": "y",
            },
        ],
    )

    r = client.get(
        "/api/projections/opportunities",
        params={"type": "STRONG_BUY", "limit": 10},
    )
    assert r.status_code == 200
    data = r.json()
    symbols = [row["symbol"] for row in data["opportunities"]]
    assert symbols == ["AAPL"]
    assert "NAN" not in symbols
    assert None not in symbols

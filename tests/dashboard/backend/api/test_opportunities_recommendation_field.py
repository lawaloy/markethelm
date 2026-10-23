"""Opportunities must expose recommendation separately from trend."""

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


def _write_fixtures(temp_data_dir: Path) -> None:
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
            },
            {
                "symbol": "MSFT",
                "name": "Microsoft",
                "close": 400.0,
                "change": -1.0,
                "change_percent": -0.2,
                "volume": 2_000,
            },
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
            },
            {
                "symbol": "MSFT",
                "name": "Microsoft",
                "target_mid": 390.0,
                "expected_change_percent": -2.5,
                "confidence": 70,
                "recommendation": "SELL",
                "risk_level": "High",
                "trend": "Bearish",
                "momentum_score": -0.4,
                "volatility_score": 0.8,
                "reason": "weakness",
            },
        ],
    )


def test_opportunities_include_recommendation_matching_filter_type(client, temp_data_dir) -> None:
    """Table filters need recommendation; trend alone is Bullish/Bearish."""
    _write_fixtures(temp_data_dir)

    r = client.get(
        "/api/projections/opportunities",
        params={"type": "STRONG_BUY", "limit": 10},
    )
    assert r.status_code == 200
    data = r.json()
    assert data["count"] == 1
    row = data["opportunities"][0]
    assert row["symbol"] == "AAPL"
    assert row["recommendation"] == "STRONG BUY"
    assert row["trend"] == "Bullish"


def test_opportunities_sell_bucket_keeps_trend_independent(client, temp_data_dir) -> None:
    _write_fixtures(temp_data_dir)

    r = client.get(
        "/api/projections/opportunities",
        params={"type": "SELL", "limit": 10},
    )
    assert r.status_code == 200
    row = r.json()["opportunities"][0]
    assert row["symbol"] == "MSFT"
    assert row["recommendation"] == "SELL"
    assert row["trend"] == "Bearish"

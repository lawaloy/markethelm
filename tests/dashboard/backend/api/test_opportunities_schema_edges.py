"""Opportunities must soft-fail missing daily symbol / risk / trend columns."""

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


def _write_projection(temp_data_dir: Path, **overrides) -> None:
    row = {
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
    row.update(overrides)
    seed_projections(temp_data_dir, "2026-01-15", [row])


def test_opportunities_defaults_when_daily_bars_missing(client, temp_data_dir) -> None:
    """Unmatched daily symbols must soft-default price/volume instead of 500ing."""
    # Bars exist for the trade date (so get_latest_date works) but not for AAPL.
    seed_daily_bars(
        temp_data_dir,
        "2026-01-15",
        [{"symbol": "MSFT", "close": 400.0, "volume": 2_000}],
    )
    _write_projection(temp_data_dir)

    r = client.get(
        "/api/projections/opportunities",
        params={"type": "STRONG_BUY", "limit": 10},
    )
    assert r.status_code == 200
    data = r.json()
    assert data["count"] == 1
    row = data["opportunities"][0]
    assert row["symbol"] == "AAPL"
    assert row["currentPrice"] == 0.0
    assert row["volume"] == 0


def test_opportunities_defaults_missing_risk_and_trend(client, temp_data_dir) -> None:
    """Legacy projection rows without risk_level/trend must still list cards."""
    seed_daily_bars(
        temp_data_dir,
        "2026-01-15",
        [{"symbol": "AAPL", "close": 150.0, "volume": 1_000}],
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
    row = r.json()["opportunities"][0]
    assert row["risk"] == "Unknown"
    assert row["trend"] == "Neutral"
    assert row["currentPrice"] == 150.0

"""Opportunities must soft-fail dirty confidence cells before nlargest ranking."""

from __future__ import annotations

import math
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


def _write_fixtures(temp_data_dir: Path, confidence_values) -> None:
    n = len(confidence_values)
    symbols = [f"T{i}" for i in range(n)]
    seed_projections(
        temp_data_dir,
        "2026-01-15",
        [
            {
                "symbol": symbols[i],
                "name": f"Ticker {i}",
                "target_mid": 160.0,
                "expected_change_percent": 5.0,
                "confidence": confidence_values[i],
                "recommendation": "BUY",
                "risk_level": "Low",
                "trend": "Bullish",
                "momentum_score": 1.2,
                "volatility_score": 0.3,
                "reason": "momentum",
            }
            for i in range(n)
        ],
    )
    seed_daily_bars(
        temp_data_dir,
        "2026-01-15",
        [{"symbol": s, "close": 150.0, "volume": 10_000} for s in symbols],
    )


def test_opportunities_soft_fails_string_confidence_column(client, temp_data_dir) -> None:
    """Object/str confidence previously TypeError'd nlargest → 500 the bucket."""
    _write_fixtures(temp_data_dir, ["high", "90", "bad", "40"])

    r = client.get(
        "/api/projections/opportunities",
        params={"type": "BUY", "limit": 10},
    )
    assert r.status_code == 200
    data = r.json()
    assert data["count"] == 2
    symbols = [row["symbol"] for row in data["opportunities"]]
    assert symbols == ["T1", "T3"]
    assert data["opportunities"][0]["confidence"] == 90
    assert data["opportunities"][1]["confidence"] == 40


def test_opportunities_skips_nonfinite_confidence_before_ranking(client, temp_data_dir) -> None:
    """Inf/NaN confidence must not win nlargest slots or abort the endpoint."""
    _write_fixtures(temp_data_dir, [math.inf, float("nan"), 70.0, -math.inf])

    r = client.get(
        "/api/projections/opportunities",
        params={"type": "BUY", "limit": 10},
    )
    assert r.status_code == 200
    data = r.json()
    assert data["count"] == 1
    assert data["opportunities"][0]["symbol"] == "T2"
    assert data["opportunities"][0]["confidence"] == 70

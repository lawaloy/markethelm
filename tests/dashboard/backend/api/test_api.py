"""Tests for dashboard backend API endpoints."""

import shutil
import tempfile
from datetime import date, datetime, timedelta
from pathlib import Path
from unittest.mock import MagicMock, patch

import pandas as pd
import pytest
from fastapi.testclient import TestClient

import dashboard.backend.api.history
import dashboard.backend.api.market
import dashboard.backend.api.projections
import dashboard.backend.api.stocks
from dashboard.backend.main import app
from dashboard.backend.services.data_loader import DataLoader
from tests.helpers.market_bars import (
    seed_daily_bars,
    seed_projections,
    seed_simple_bars,
    seed_summary,
)


@pytest.fixture
def temp_data_dir(monkeypatch):
    """Create temp data directory with sample files."""
    monkeypatch.delenv("MARKETHELM_DATABASE_URL", raising=False)
    tmp = tempfile.mkdtemp()
    yield Path(tmp)
    shutil.rmtree(tmp, ignore_errors=True)


@pytest.fixture
def sample_daily_data(temp_data_dir):
    """Seed sample daily quotes into market_bars."""
    seed_daily_bars(
        temp_data_dir,
        "2026-01-15",
        [
            {
                "symbol": "AAPL",
                "name": "Apple",
                "close": 150.0,
                "change": 1.5,
                "change_percent": 1.0,
                "volume": 50_000_000,
                "index_name": "S&P 500",
            },
            {
                "symbol": "GOOGL",
                "name": "Alphabet",
                "close": 2800.0,
                "change": -28.0,
                "change_percent": -1.0,
                "volume": 2_000_000,
                "index_name": "NASDAQ-100",
            },
            {
                "symbol": "MSFT",
                "name": "Microsoft",
                "close": 350.0,
                "change": 2.1,
                "change_percent": 0.6,
                "volume": 25_000_000,
                "index_name": "S&P 500",
            },
        ],
    )
    return temp_data_dir


@pytest.fixture
def sample_summary(temp_data_dir):
    """Seed sample summary into durable daily_summaries storage."""
    summary = {
        "date": "2026-01-15",
        "analysis": {
            "date": "2026-01-15",
            "summary": {
                "total_stocks": 3,
                "gainers": 2,
                "losers": 1,
                "average_change_percent": 0.2,
            },
            "top_gainers": [
                {"symbol": "AAPL", "change_percent": 1.0},
                {"symbol": "MSFT", "change_percent": 0.6},
            ],
            "top_losers": [
                {"symbol": "GOOGL", "change_percent": -1.0},
            ],
        },
        "exchange_comparison": {
            "S&P 500": {"average_change_percent": 0.8, "gainers": 2, "losers": 1},
            "NASDAQ-100": {"average_change_percent": -0.2, "gainers": 1, "losers": 1},
        },
    }
    seed_summary(temp_data_dir, "2026-01-15", summary)
    return summary


@pytest.fixture
def mock_data_loader(temp_data_dir, sample_daily_data, sample_summary):
    """Create a real DataLoader with temp data."""

    return DataLoader(data_dir=temp_data_dir)


@pytest.fixture
def client(mock_data_loader):
    """Create TestClient with patched data loader.

    Import API modules first so they exist in the package namespace, then patch
    get_data_loader where it is used. Patches must be where the name is looked up
    (in the using module), not where it is defined.
    """

    with patch.object(
        dashboard.backend.api.market, "get_data_loader", return_value=mock_data_loader
    ):
        with patch.object(
            dashboard.backend.api.projections, "get_data_loader", return_value=mock_data_loader
        ):
            with patch.object(
                dashboard.backend.api.stocks, "get_data_loader", return_value=mock_data_loader
            ):
                with patch.object(
                    dashboard.backend.api.history, "get_data_loader", return_value=mock_data_loader
                ):
                    yield TestClient(app)


class TestDashboardHealth:
    """Test health and root endpoints."""

    def test_root_returns_spa_or_health_json(self, client):
        """Root serves bundled SPA (HTML) or JSON when SPA is not built."""
        r = client.get("/")
        assert r.status_code == 200
        ct = r.headers.get("content-type", "")
        if "application/json" in ct:
            data = r.json()
            assert data["status"] == "healthy"
            assert "MarketHelm" in data["service"]
        else:
            assert "text/html" in ct

    def test_health_returns_healthy(self, client):
        """Health endpoint returns healthy."""
        r = client.get("/health")
        assert r.status_code == 200
        assert r.json() == {"status": "healthy"}


class TestMarketAPI:
    """Test market API endpoints."""

    def test_market_overview_returns_data(self, client):
        """GET /api/market/overview returns market stats."""
        r = client.get("/api/market/overview")
        assert r.status_code == 200
        data = r.json()
        assert data["totalStocks"] == 3
        assert data["gainers"] == 2
        assert data["losers"] == 1
        assert "date" in data

    def test_market_overview_strips_spaces_from_index_keys(self, client):
        """Index keys drop spaces only so 'S&P 500' becomes 'S&P500'."""
        r = client.get("/api/market/overview")
        assert r.status_code == 200
        indices = r.json()["indices"]
        assert set(indices) == {"S&P500", "NASDAQ-100"}
        assert indices["S&P500"]["stocks"] == 2
        assert indices["S&P500"]["gainers"] == 2
        assert indices["S&P500"]["losers"] == 0
        assert indices["NASDAQ-100"]["stocks"] == 1
        assert indices["NASDAQ-100"]["losers"] == 1

    def test_market_movers_gainers(self, client):
        """GET /api/market/movers?type=gainers returns top gainers."""
        r = client.get("/api/market/movers", params={"type": "gainers", "limit": 5})
        assert r.status_code == 200
        data = r.json()
        assert data["type"] == "gainers"
        assert len(data["data"]) >= 1
        assert all(row["changePercent"] > 0 for row in data["data"])
        percents = [row["changePercent"] for row in data["data"]]
        assert percents == sorted(percents, reverse=True)

    def test_market_movers_losers(self, client):
        """GET /api/market/movers?type=losers returns top losers."""
        r = client.get("/api/market/movers", params={"type": "losers", "limit": 5})
        assert r.status_code == 200
        data = r.json()
        assert data["type"] == "losers"
        assert len(data["data"]) == 1
        assert all(row["changePercent"] < 0 for row in data["data"])
        assert data["data"][0]["symbol"] == "GOOGL"

    def test_market_movers_do_not_backfill_opposite_sign(self, client):
        """Large limit must not mix gainers into losers (or vice versa)."""
        losers = client.get("/api/market/movers", params={"type": "losers", "limit": 50})
        assert losers.status_code == 200
        loser_rows = losers.json()["data"]
        assert len(loser_rows) == 1
        assert all(row["changePercent"] < 0 for row in loser_rows)

        gainers = client.get("/api/market/movers", params={"type": "gainers", "limit": 50})
        assert gainers.status_code == 200
        gainer_rows = gainers.json()["data"]
        assert len(gainer_rows) == 2
        assert all(row["changePercent"] > 0 for row in gainer_rows)
        assert [row["symbol"] for row in gainer_rows] == ["AAPL", "MSFT"]

    def test_market_overview_404_when_no_latest_date(self, temp_data_dir):
        """No latest date must stay 404 (not swallowed into 500)."""

        mock_loader = MagicMock()
        mock_loader.get_latest_date.return_value = None
        with patch.object(
            dashboard.backend.api.market, "get_data_loader", return_value=mock_loader
        ):
            client = TestClient(app)
            r = client.get("/api/market/overview")

        assert r.status_code == 404
        assert "No data available" in r.json()["detail"]

    def test_market_overview_404_when_daily_empty(self, temp_data_dir):
        """Empty daily frame is treated as no usable overview data."""

        mock_loader = MagicMock()
        mock_loader.get_latest_date.return_value = "2026-01-15"
        mock_loader.load_daily_data.return_value = pd.DataFrame(
            {"change_percent": pd.Series(dtype=float)}
        )
        with patch.object(
            dashboard.backend.api.market, "get_data_loader", return_value=mock_loader
        ):
            client = TestClient(app)
            r = client.get("/api/market/overview")

        assert r.status_code == 404

    def test_market_overview_404_when_change_percent_missing(self, temp_data_dir):
        """Missing change_percent column must 404 instead of KeyError→500."""

        mock_loader = MagicMock()
        mock_loader.get_latest_date.return_value = "2026-01-15"
        mock_loader.load_daily_data.return_value = pd.DataFrame({"symbol": ["AAPL"]})
        with patch.object(
            dashboard.backend.api.market, "get_data_loader", return_value=mock_loader
        ):
            client = TestClient(app)
            r = client.get("/api/market/overview")

        assert r.status_code == 404

    def test_market_overview_coerces_all_nan_change_percent(self, temp_data_dir):
        """All-NaN change_percent must yield finite 0.0 stats, not null JSON floats."""

        mock_loader = MagicMock()
        mock_loader.get_latest_date.return_value = "2026-01-15"
        mock_loader.load_daily_data.return_value = pd.DataFrame(
            {
                "symbol": ["AAPL", "MSFT"],
                "change_percent": [float("nan"), float("nan")],
                "index_name": ["S&P 500", "S&P 500"],
            }
        )
        with patch.object(
            dashboard.backend.api.market, "get_data_loader", return_value=mock_loader
        ):
            client = TestClient(app)
            r = client.get("/api/market/overview")

        assert r.status_code == 200
        data = r.json()
        assert data["totalStocks"] == 2
        assert data["averageChange"] == 0.0
        assert data["maxChange"] == 0.0
        assert data["minChange"] == 0.0
        assert data["indices"]["S&P500"]["avgChange"] == 0.0


class TestSummaryAPI:
    """Test summary API endpoint."""

    def test_summary_returns_data(self, client):
        """GET /api/summary returns summary with date and source."""
        r = client.get("/api/summary")
        assert r.status_code == 200
        data = r.json()
        assert "date" in data
        assert "summary" in data
        assert data["source"] in ("ai", "demo")
        assert len(data["summary"]) > 0

    def test_summary_uses_ai_when_non_blank(self, client, temp_data_dir, sample_summary):
        """Whitespace-only AI text falls back to demo; real AI text is preferred."""
        payload = dict(sample_summary)
        payload["ai_summary"] = "   "
        seed_summary(temp_data_dir, "2026-01-15", payload)
        blank = client.get("/api/summary").json()
        assert blank["source"] == "demo"
        assert "gainers" in blank["summary"]

        payload["ai_summary"] = "  Markets advanced on strong tech leadership.  "
        seed_summary(temp_data_dir, "2026-01-15", payload)
        ai = client.get("/api/summary").json()
        assert ai["source"] == "ai"
        assert ai["summary"] == "Markets advanced on strong tech leadership."

    def test_summary_demo_tolerates_partial_legacy_fields(
        self, client, temp_data_dir, sample_summary
    ):
        """Partial/legacy summary payloads must soft-fail to a demo string, not 500."""
        payload = dict(sample_summary)
        payload.pop("ai_summary", None)
        payload["analysis"] = {
            "summary": {
                "gainers": "2",
                "losers": None,
                "average_change_percent": "not-a-number",
            },
            "top_gainers": [{"symbol": "AAPL"}],  # missing change_percent
            "top_losers": [{"change_percent": -1.5}],  # missing symbol
        }
        payload["exchange_comparison"] = {
            "NYSE": {"average_change_percent": None},
            "NASDAQ": "legacy-non-dict",
        }
        seed_summary(temp_data_dir, "2026-01-15", payload)

        r = client.get("/api/summary")
        assert r.status_code == 200
        data = r.json()
        assert data["source"] == "demo"
        assert "sentiment" in data["summary"]
        assert "AAPL led" not in data["summary"]
        assert "declined" not in data["summary"]


@pytest.fixture
def sample_projections(temp_data_dir, sample_daily_data):
    """Seed sample projections aligned with daily fixtures."""
    rows = [
        {
            "symbol": "AAPL",
            "name": "Apple",
            "target_mid": 160.0,
            "expected_change_percent": 2.5,
            "confidence": 80,
            "recommendation": "STRONG BUY",
            "risk_level": "Low",
            "trend": "Bullish",
            "reason": "momentum",
            "momentum_score": 1.2,
            "volatility_score": 0.4,
        },
        {
            "symbol": "GOOGL",
            "name": "Alphabet",
            "target_mid": 2700.0,
            "expected_change_percent": -1.5,
            "confidence": 70,
            "recommendation": "SELL",
            "risk_level": "High",
            "trend": "Bearish",
            "reason": "weakness",
            "momentum_score": -0.8,
            "volatility_score": 0.9,
        },
        {
            "symbol": "MSFT",
            "name": "Microsoft",
            "target_mid": 360.0,
            "expected_change_percent": 0.2,
            "confidence": 60,
            "recommendation": "HOLD",
            "risk_level": "Medium",
            "trend": "Neutral",
            "reason": "range",
            "momentum_score": 0.1,
            "volatility_score": 0.3,
        },
        {
            "symbol": "ORPHAN",
            "name": "Orphan Co",
            "target_mid": 50.0,
            "expected_change_percent": 3.0,
            "confidence": 90,
            "recommendation": "STRONG BUY",
            "risk_level": "Medium",
            "trend": "Bullish",
            "reason": "breakout",
            "momentum_score": 1.5,
            "volatility_score": 0.5,
        },
    ]
    seed_projections(temp_data_dir, "2026-01-15", rows)
    return rows


class TestStocksAPI:
    """Stock detail and historical endpoints."""

    def test_stock_detail_uppercases_symbol_and_includes_projection(
        self, client, sample_projections
    ):
        r = client.get("/api/stocks/aapl")
        assert r.status_code == 200
        data = r.json()
        assert data["symbol"] == "AAPL"
        assert data["name"] == "Apple"
        assert data["currentData"]["price"] == 150.0
        assert data["projection"]["recommendation"] == "STRONG BUY"
        assert data["projection"]["targetPrice"] == 160.0
        assert data["technical"]["momentum"] == 1.2

    def test_stock_detail_404_for_unknown_symbol(self, client):
        r = client.get("/api/stocks/ZZZZ")
        assert r.status_code == 404
        assert r.json()["detail"] == "Stock not found."

    def test_stock_detail_404_when_price_fields_missing(self, temp_data_dir):
        """Missing close/change/change_percent must 404 instead of KeyError→500."""

        mock_loader = MagicMock()
        mock_loader.get_latest_date.return_value = "2026-01-15"
        mock_loader.load_daily_data.return_value = pd.DataFrame(
            {"symbol": ["AAPL"], "name": ["Apple"]}
        )
        with patch.object(
            dashboard.backend.api.stocks, "get_data_loader", return_value=mock_loader
        ):
            client = TestClient(app)
            r = client.get("/api/stocks/AAPL")

        assert r.status_code == 404
        assert r.json()["detail"] == "Stock not found."

    def test_stock_detail_404_when_price_is_nan(self, temp_data_dir):
        """Non-finite close/change/change_percent must 404 (not null JSON numbers)."""

        mock_loader = MagicMock()
        mock_loader.get_latest_date.return_value = "2026-01-15"
        mock_loader.load_daily_data.return_value = pd.DataFrame(
            {
                "symbol": ["AAPL"],
                "name": ["Apple"],
                "close": [float("nan")],
                "change": [1.0],
                "change_percent": [0.5],
                "volume": [1_000],
            }
        )
        with patch.object(
            dashboard.backend.api.stocks, "get_data_loader", return_value=mock_loader
        ):
            client = TestClient(app)
            r = client.get("/api/stocks/AAPL")

        assert r.status_code == 404
        assert r.json()["detail"] == "Stock not found."

    def test_stock_detail_omits_projection_when_target_is_nan(
        self, client, mock_data_loader, temp_data_dir
    ):
        """Finite daily prices with NaN projection fields soft-fail projection to null."""
        seed_projections(
            temp_data_dir,
            "2026-01-15",
            [
                {
                    "symbol": "AAPL",
                    "name": "Apple",
                    "target_mid": float("nan"),
                    "expected_change_percent": 2.5,
                    "confidence": 80,
                    "recommendation": "BUY",
                    "risk_level": "Low",
                    "trend": "Bullish",
                }
            ],
        )

        r = client.get("/api/stocks/AAPL")
        assert r.status_code == 200
        data = r.json()
        assert data["currentData"]["price"] == 150.0
        assert data["projection"] is None
        assert data["technical"] is None

    def test_stock_historical_skips_nan_days(self, client, mock_data_loader, temp_data_dir):
        """Days without usable change_percent are omitted; valid siblings still return 200."""

        recent = datetime.now().strftime("%Y-%m-%d")
        prior = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")
        # Non-finite close is rejected at upsert; missing change_percent is stored
        # as NULL and skipped by the historical API response builder.
        seed_daily_bars(
            temp_data_dir,
            recent,
            [{"symbol": "AAPL", "name": "Apple", "close": 149.0, "volume": 1}],
        )
        seed_daily_bars(
            temp_data_dir,
            prior,
            [
                {
                    "symbol": "AAPL",
                    "name": "Apple",
                    "close": 150.0,
                    "change": 1.0,
                    "change_percent": 0.7,
                    "volume": 2_000,
                }
            ],
        )

        r = client.get("/api/stocks/AAPL/historical", params={"days": 7})
        assert r.status_code == 200
        data = r.json()
        assert [point["date"] for point in data["data"]] == [prior]
        assert data["data"][0]["close"] == 150.0

    def test_stock_historical_404_when_all_points_invalid(
        self, client, mock_data_loader, temp_data_dir
    ):
        """If every day lacks finite change_percent, historical returns 404."""

        recent = datetime.now().strftime("%Y-%m-%d")
        seed_daily_bars(
            temp_data_dir,
            recent,
            [{"symbol": "AAPL", "name": "Apple", "close": 150.0, "volume": 1}],
        )

        r = client.get("/api/stocks/AAPL/historical", params={"days": 7})
        assert r.status_code == 404
        assert r.json()["detail"] == "No historical data found."

    def test_stock_historical_returns_points(self, client, mock_data_loader, temp_data_dir):
        recent = datetime.now().strftime("%Y-%m-%d")
        older = (datetime.now() - timedelta(days=2)).strftime("%Y-%m-%d")
        seed_daily_bars(
            temp_data_dir,
            recent,
            [
                {
                    "symbol": "AAPL",
                    "name": "Apple",
                    "close": 155.0,
                    "change": 5.0,
                    "change_percent": 3.3,
                    "volume": 40_000_000,
                }
            ],
        )
        seed_projections(
            temp_data_dir,
            recent,
            [
                {
                    "symbol": "AAPL",
                    "name": "Apple",
                    "target_mid": 165.0,
                    "expected_change_percent": 2.0,
                    "confidence": 75,
                    "recommendation": "BUY",
                }
            ],
        )
        # Stale bar outside the requested window should be ignored.
        seed_daily_bars(
            temp_data_dir,
            older,
            [
                {
                    "symbol": "AAPL",
                    "name": "Apple",
                    "close": 100.0,
                    "change": 0.0,
                    "change_percent": 0.0,
                    "volume": 1,
                }
            ],
        )

        r = client.get("/api/stocks/AAPL/historical", params={"days": 1})
        assert r.status_code == 200
        data = r.json()
        assert data["symbol"] == "AAPL"
        assert [point["date"] for point in data["data"]] == [recent]
        point = data["data"][0]
        assert point["close"] == 155.0
        assert point["projection"]["targetPrice"] == 165.0
        assert point["projection"]["recommendation"] == "BUY"

    def test_stock_detail_matches_padded_daily_and_projection_symbols(
        self, client, mock_data_loader, temp_data_dir
    ):
        """Padded symbols must still resolve via normalize_ticker matching."""
        seed_daily_bars(
            temp_data_dir,
            "2026-01-15",
            [
                {
                    "symbol": " AAPL ",
                    "name": "Apple",
                    "close": 151.0,
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
                    "symbol": " aapl ",
                    "name": "Apple",
                    "target_mid": 160.0,
                    "expected_change_percent": 2.0,
                    "confidence": 80,
                    "recommendation": "BUY",
                    "risk_level": "Low",
                    "trend": "Bullish",
                    "momentum_score": 1.1,
                    "volatility_score": 0.4,
                }
            ],
        )

        r = client.get("/api/stocks/aapl")
        assert r.status_code == 200
        data = r.json()
        assert data["symbol"] == "AAPL"
        assert data["currentData"]["price"] == 151.0
        assert data["projection"]["targetPrice"] == 160.0
        assert data["projection"]["recommendation"] == "BUY"

    def test_stock_historical_matches_padded_csv_symbols(
        self, client, mock_data_loader, temp_data_dir
    ):
        """Historical series must find padded daily/projection rows for the path symbol."""

        recent = (date.today() - timedelta(days=1)).isoformat()
        seed_daily_bars(
            temp_data_dir,
            recent,
            [
                {
                    "symbol": " AAPL ",
                    "name": "Apple",
                    "close": 155.0,
                    "change": 1.0,
                    "change_percent": 0.5,
                    "volume": 2_000,
                }
            ],
        )
        seed_projections(
            temp_data_dir,
            recent,
            [
                {
                    "symbol": "aapl",
                    "target_mid": 165.0,
                    "confidence": 70,
                    "recommendation": "BUY",
                    "expected_change_percent": 3.0,
                }
            ],
        )

        r = client.get("/api/stocks/AAPL/historical", params={"days": 7})
        assert r.status_code == 200
        data = r.json()
        assert data["symbol"] == "AAPL"
        assert len(data["data"]) >= 1
        by_date = {point["date"]: point for point in data["data"]}
        assert by_date[recent]["close"] == 155.0
        assert by_date[recent]["projection"]["targetPrice"] == 165.0


class TestProjectionsAPI:
    """Projections summary and opportunities endpoints."""

    def test_projections_summary_maps_recommendations_and_sentiment(
        self, client, sample_projections
    ):
        r = client.get("/api/projections/summary")
        assert r.status_code == 200
        data = r.json()
        assert data["date"] == "2026-01-15"
        assert data["targetDate"] == "2026-01-23"
        assert data["totalProjections"] == 4
        assert data["sentiment"] == "Bullish"
        assert data["recommendations"]["STRONG_BUY"] == 2
        assert data["recommendations"]["SELL"] == 1
        assert data["recommendations"]["HOLD"] == 1
        assert data["trends"]["Bullish"] == 2
        assert data["riskProfile"]["Medium"] == 2

    def test_opportunities_filters_and_defaults_missing_daily_price(
        self, client, sample_projections
    ):
        r = client.get(
            "/api/projections/opportunities",
            params={"type": "STRONG_BUY", "limit": 10},
        )
        assert r.status_code == 200
        data = r.json()
        assert data["type"] == "STRONG_BUY"
        assert data["count"] == 2
        symbols = [row["symbol"] for row in data["opportunities"]]
        assert symbols == ["ORPHAN", "AAPL"]
        by_symbol = {row["symbol"]: row for row in data["opportunities"]}
        assert by_symbol["AAPL"]["currentPrice"] == 150.0
        assert by_symbol["ORPHAN"]["currentPrice"] == 0
        assert by_symbol["ORPHAN"]["volume"] == 0


@pytest.mark.parametrize("endpoint", ["/api/projections/summary", "/api/stocks/AAPL"])
@pytest.mark.parametrize(
    ("run_date", "metadata", "expected_target"),
    [
        ("2026-01-15", {}, "2026-01-23"),
        ("2026-07-02", {}, "2026-07-10"),
        ("2026-07-06", {"generated_at": "2026-07-06T12:00:00+00:00"}, "2026-07-10"),
        ("2026-07-06", {"generated_at": "2026-07-06T15:00:00+00:00"}, "2026-07-10"),
        ("2026-07-06", {"generated_at": "2026-07-06T21:00:00+00:00"}, "2026-07-13"),
        (
            "2026-07-06",
            {
                "projection_date": "2026-07-10",
                "generated_at": "2026-07-06T21:00:00+00:00",
            },
            "2026-07-10",
        ),
    ],
)
def test_projection_endpoints_preserve_trading_session_targets(
    client, temp_data_dir, endpoint, run_date, metadata, expected_target
):
    """Both APIs must preserve stored targets and derive legacy dates on XNYS sessions."""
    seed_daily_bars(
        temp_data_dir,
        run_date,
        [{"symbol": "AAPL", "close": 150.0, "change": 1.5, "change_percent": 1.0}],
    )
    seed_projections(
        temp_data_dir,
        run_date,
        [
            {
                "symbol": "AAPL",
                "target_mid": 160.0,
                "confidence": 80,
                "expected_change_percent": 2.5,
                **metadata,
            }
        ],
    )

    response = client.get(endpoint)

    assert response.status_code == 200
    data = response.json()
    projection = data["projection"] if endpoint == "/api/stocks/AAPL" else data
    assert projection["targetDate"] == expected_target


class TestStocksAPIEdges:
    """Stock detail/historical error and soft-fail paths."""

    def test_stock_detail_404_when_no_latest_date(self, temp_data_dir):
        mock_loader = MagicMock()
        mock_loader.get_latest_date.return_value = None
        with patch.object(
            dashboard.backend.api.stocks, "get_data_loader", return_value=mock_loader
        ):
            client = TestClient(app)
            r = client.get("/api/stocks/AAPL")

        assert r.status_code == 404
        assert r.json()["detail"] == "No data available"

    def test_stock_detail_survives_projection_load_failure(self, client, mock_data_loader):
        mock_data_loader.load_projections = MagicMock(
            side_effect=FileNotFoundError("projections missing")
        )
        r = client.get("/api/stocks/AAPL")
        assert r.status_code == 200
        data = r.json()
        assert data["symbol"] == "AAPL"
        assert data["currentData"]["price"] == 150.0
        assert data["projection"] is None
        assert data["technical"] is None

    def test_stock_historical_404_when_empty(self, client, mock_data_loader):
        mock_data_loader.load_historical_data = MagicMock(return_value=[])
        r = client.get("/api/stocks/AAPL/historical", params={"days": 7})
        assert r.status_code == 404
        assert r.json()["detail"] == "No historical data found."


class TestProjectionsSentimentBands:
    """Projections summary sentiment thresholds (±1.0)."""

    def _write_projections(self, temp_data_dir, changes):
        seed_projections(
            temp_data_dir,
            "2026-01-15",
            [
                {
                    "symbol": f"S{i}",
                    "name": f"Stock {i}",
                    "target_mid": 100.0,
                    "expected_change_percent": change,
                    "confidence": 50,
                    "recommendation": "HOLD",
                    "risk_level": "Medium",
                    "trend": "Neutral",
                }
                for i, change in enumerate(changes)
            ],
        )

    def test_sentiment_neutral_and_bearish_bands(self, client, temp_data_dir):
        self._write_projections(temp_data_dir, [0.5, -0.5])
        neutral = client.get("/api/projections/summary").json()
        assert neutral["sentiment"] == "Neutral"
        assert neutral["expectedMarketMove"] == 0.0

        self._write_projections(temp_data_dir, [-2.0, -1.5])
        bearish = client.get("/api/projections/summary").json()
        assert bearish["sentiment"] == "Bearish"
        assert bearish["expectedMarketMove"] == -1.75

    def test_projections_summary_404_when_no_date(self, temp_data_dir):
        mock_loader = MagicMock()
        mock_loader.get_latest_date.return_value = None
        with patch.object(
            dashboard.backend.api.projections, "get_data_loader", return_value=mock_loader
        ):
            client = TestClient(app)
            r = client.get("/api/projections/summary")

        assert r.status_code == 404
        assert r.json()["detail"] == "No data available"


class TestHistoryAccuracyAPI:
    """Projection accuracy endpoint."""

    def test_accuracy_returns_summary(self, client, mock_data_loader):
        """GET /api/history/accuracy returns summary and samples."""
        mock_data_loader.compute_projection_accuracy = MagicMock(
            return_value={
                "summary": {
                    "schemaVersion": 1,
                    "calendar": "XNYS",
                    "horizonSessions": 5,
                    "projectionCount": 2,
                    "validProjectionCount": 2,
                    "sampleCount": 1,
                    "invalidCount": 0,
                    "pendingCount": 0,
                    "missingActualCount": 1,
                    "evaluationCoveragePct": 50.0,
                    "meanAbsErrorPct": 3.0,
                    "medianAbsErrorPct": 3.0,
                    "directionalAccuracyPct": 100.0,
                    "bandCoveragePct": 100.0,
                    "meanConfidence": 70.0,
                    "calibrationGapPct": -30.0,
                    "byRecommendation": {
                        "HOLD": {
                            "count": 1,
                            "meanAbsErrorPct": 3.0,
                            "medianAbsErrorPct": 3.0,
                            "directionalAccuracyPct": 100.0,
                            "bandCoveragePct": 100.0,
                            "meanConfidence": 70.0,
                            "calibrationGapPct": -30.0,
                        },
                    },
                    "byConfidenceBand": {
                        "70-79": {
                            "count": 1,
                            "meanAbsErrorPct": 3.0,
                            "medianAbsErrorPct": 3.0,
                            "directionalAccuracyPct": 100.0,
                            "bandCoveragePct": 100.0,
                            "meanConfidence": 70.0,
                            "calibrationGapPct": -30.0,
                        }
                    },
                },
                "samples": [
                    {
                        "symbol": "AAPL",
                        "runDate": "2026-01-10",
                        "targetDate": "2026-01-15",
                        "actualDate": "2026-01-15",
                        "current": 99.0,
                        "predicted": 100.0,
                        "actual": 103.0,
                        "absErrorPct": 3.0,
                        "signedErrorPct": -3.0,
                        "directionCorrect": True,
                        "bandHit": True,
                        "confidence": 70.0,
                        "confidenceBand": "70-79",
                        "recommendation": "HOLD",
                    }
                ],
                "samplesTruncated": False,
            }
        )
        r = client.get("/api/history/accuracy", params={"days": 30})
        assert r.status_code == 200
        data = r.json()
        assert data["summary"]["sampleCount"] == 1
        assert data["summary"]["meanAbsErrorPct"] == 3.0
        assert data["summary"]["directionalAccuracyPct"] == 100.0
        assert data["summary"]["byConfidenceBand"]["70-79"]["count"] == 1
        assert len(data["samples"]) == 1
        assert data["samples"][0]["symbol"] == "AAPL"

    def test_accuracy_valueerror_maps_to_404(self, client, mock_data_loader):
        """GET /api/history/accuracy maps loader ValueError to 404 (not 500)."""
        mock_data_loader.compute_projection_accuracy = MagicMock(
            side_effect=ValueError("No projection files found")
        )
        r = client.get("/api/history/accuracy", params={"days": 30})
        assert r.status_code == 404
        assert r.json()["detail"] == "No data available."

    def test_history_dates_and_symbols_valueerror_maps_to_404(self, client, mock_data_loader):
        """GET /api/history/dates and /symbols map loader ValueError to 404."""
        mock_data_loader.get_available_dates = MagicMock(
            side_effect=ValueError("Data directory not found")
        )
        dates = client.get("/api/history/dates")
        assert dates.status_code == 404
        assert dates.json()["detail"] == "No data available."

        mock_data_loader.get_latest_date = MagicMock(
            side_effect=ValueError("No projection files found")
        )
        symbols = client.get("/api/history/symbols")
        assert symbols.status_code == 404
        assert symbols.json()["detail"] == "No data available."


class TestMarketAPIErrors:
    """Test API error handling."""

    def test_data_info_404_when_data_dir_missing(self):
        """GET /api/data-info returns 404 when DATA_DIR/user data path does not exist (not 500)."""
        with patch(
            "dashboard.backend.services.data_loader.get_data_loader",
            side_effect=ValueError("Data directory not found: /nonexistent"),
        ):
            client = TestClient(app)
            r = client.get("/api/data-info")

        assert r.status_code == 404
        assert r.json()["detail"] == "No data available."

    def test_data_info_happy_path(self, client, mock_data_loader, temp_data_dir):
        """GET /api/data-info reports latest date, trading-day target, and needs_fetch."""
        with patch(
            "dashboard.backend.services.data_loader.get_data_loader",
            return_value=mock_data_loader,
        ):
            with patch(
                "dashboard.backend.services.data_loader.get_most_recent_trading_day",
                return_value="2026-01-16",
            ):
                r = client.get("/api/data-info")

        assert r.status_code == 200
        data = r.json()
        assert data["data_dir"] == str(temp_data_dir)
        assert data["latest_date"] == "2026-01-15"
        assert data["target_trading_day"] == "2026-01-16"
        assert data["needs_fetch"] is True
        assert "2026-01-15" in data["available_dates"]

    def test_summary_404_when_no_data(self, temp_data_dir):
        """Summary returns 404 when no summary files exist."""

        mock_loader = MagicMock()
        mock_loader.load_summary.side_effect = ValueError("No summary files found")
        with patch.object(
            dashboard.backend.api.market, "get_data_loader", return_value=mock_loader
        ):
            client = TestClient(app)
            r = client.get("/api/summary")

        assert r.status_code == 404

    def test_summary_404_when_missing(self, client, mock_data_loader, temp_data_dir):
        """Missing summary rows must 404 via ValueError mapping, not generic 500."""
        # sample_summary seeded a row; overwrite loader to simulate empty summaries.
        mock_data_loader.load_summary = MagicMock(side_effect=ValueError("No summary files found"))

        r = client.get("/api/summary")
        assert r.status_code == 404
        assert r.json()["detail"] == "No data available."


class TestHistorySummaryAPI:
    """Historical projections summary endpoint."""

    def test_run_projections_returns_saved_rows_for_requested_date(self, client, temp_data_dir):
        seed_projections(
            temp_data_dir,
            "2026-01-15",
            [
                {
                    "symbol": "AAPL",
                    "name": "Apple Inc.",
                    "current_price": 100.0,
                    "target_mid": 105.0,
                    "target_low": 102.0,
                    "target_high": 108.0,
                    "confidence": 75.0,
                    "expected_change_percent": 5.0,
                    "recommendation": "BUY",
                    "risk_level": "Low",
                    "reason": "Positive momentum",
                },
                {
                    "symbol": "MSFT",
                    "name": "Microsoft",
                    "current_price": 200.0,
                    "target_mid": 198.0,
                    "confidence": float("nan"),
                    "expected_change_percent": -1.0,
                    "recommendation": "SELL",
                },
            ],
        )

        response = client.get("/api/history/runs/2026-01-15/projections")

        assert response.status_code == 200
        body = response.json()
        assert body["date"] == "2026-01-15"
        assert body["totalProjections"] == 2
        assert {row["symbol"] for row in body["projections"]} == {"AAPL", "MSFT"}
        apple = next(row for row in body["projections"] if row["symbol"] == "AAPL")
        assert apple["targetPrice"] == 105.0
        assert apple["targetLow"] == 102.0
        assert apple["targetHigh"] == 108.0
        assert apple["reason"] == "Positive momentum"
        microsoft = next(row for row in body["projections"] if row["symbol"] == "MSFT")
        assert microsoft["confidence"] is None

    def test_run_projections_missing_or_invalid_date(self, client):
        assert client.get("/api/history/runs/2026-01-14/projections").status_code == 404
        assert client.get("/api/history/runs/not-a-date/projections").status_code == 422

    def test_history_summary_coerces_all_nan_means(self, client, mock_data_loader, temp_data_dir):
        """All-NaN confidence/expected means must serialize as finite 0.0 + Neutral."""
        seed_projections(
            temp_data_dir,
            "2026-01-15",
            [
                {
                    "symbol": "AAPL",
                    "name": "Apple",
                    "target_mid": 160.0,
                    "expected_change_percent": float("nan"),
                    "confidence": float("nan"),
                    "recommendation": "HOLD",
                    "risk_level": "Medium",
                    "trend": "Neutral",
                },
                {
                    "symbol": "MSFT",
                    "name": "Microsoft",
                    "target_mid": 360.0,
                    "expected_change_percent": float("nan"),
                    "confidence": float("nan"),
                    "recommendation": "HOLD",
                    "risk_level": "Medium",
                    "trend": "Neutral",
                },
            ],
        )

        r = client.get("/api/history/summary", params={"days": 7})
        assert r.status_code == 200
        data = r.json()
        assert len(data["data"]) == 1
        point = data["data"][0]
        assert point["averageConfidence"] == 0.0
        assert point["expectedMarketMove"] == 0.0
        assert point["sentiment"] == "Neutral"

    def test_dates_and_symbols_endpoints(self, client, mock_data_loader, temp_data_dir):
        """GET /api/history/dates and /symbols return catalog data."""
        seed_projections(
            temp_data_dir,
            "2026-01-15",
            [
                {
                    "symbol": "AAPL",
                    "name": "Apple Inc.",
                    "confidence": 50,
                    "expected_change_percent": 0.5,
                    "recommendation": "HOLD",
                    "target_mid": 100.0,
                },
                {
                    "symbol": "ZZZZ",
                    "name": "Custom Co",
                    "confidence": 60,
                    "expected_change_percent": -0.2,
                    "recommendation": "BUY",
                    "target_mid": 10.0,
                },
            ],
        )

        with patch(
            "dashboard.backend.api.history.load_index_symbol_names",
            return_value={"MSFT": "Microsoft"},
        ):
            dates = client.get("/api/history/dates")
            symbols = client.get("/api/history/symbols")

        assert dates.status_code == 200
        assert "2026-01-15" in dates.json()["dates"]
        assert symbols.status_code == 200
        body = symbols.json()
        assert body["date"] == "2026-01-15"
        assert "MSFT" in body["symbols"]
        assert "ZZZZ" in body["symbols"]
        assert body["names"]["ZZZZ"] == "Custom Co"

    def test_summary_skips_bad_dates_and_sets_sentiment(
        self, client, mock_data_loader, temp_data_dir
    ):
        """Historical summary skips unloadable dates and classifies sentiment bands."""
        seed_projections(
            temp_data_dir,
            "2026-01-16",
            [
                {
                    "symbol": "AAPL",
                    "name": "Apple",
                    "confidence": 80,
                    "expected_change_percent": 2.5,
                    "recommendation": "BUY",
                }
            ],
        )
        seed_projections(
            temp_data_dir,
            "2026-01-15",
            [
                {
                    "symbol": "AAPL",
                    "confidence": 40,
                    "expected_change_percent": -1.5,
                    "recommendation": "SELL",
                }
            ],
        )
        # Bars so get_available_dates includes these days (no projections for 01-14)
        for d in ("2026-01-16", "2026-01-15", "2026-01-14"):
            seed_simple_bars(temp_data_dir, d, close=1.0, change_percent=0.0)

        with patch(
            "dashboard.backend.api.history._resolve_company_names",
            side_effect=lambda syms: {s: s for s in syms},
        ):
            r = client.get("/api/history/summary", params={"days": 10})

        assert r.status_code == 200
        body = r.json()
        assert body["lastDate"] == "2026-01-16"
        assert body["firstDate"] == "2026-01-14"
        sentiments = {p["date"]: p["sentiment"] for p in body["data"]}
        assert sentiments["2026-01-16"] == "Bullish"
        assert sentiments["2026-01-15"] == "Bearish"
        assert "2026-01-14" not in sentiments
        assert "AAPL" in body["symbols"]

    def test_market_movers_skips_non_finite_price_and_coerces_bad_volume(self, temp_data_dir):
        """One NaN price or volume must not 500 the movers card or emit null JSON."""

        mock_loader = MagicMock()
        mock_loader.load_daily_data.return_value = pd.DataFrame(
            {
                "symbol": ["GOOD", "BADPRICE", "BADVOL"],
                "name": ["Good Co", "Bad Price", "Bad Vol"],
                "close": [100.0, float("nan"), 50.0],
                "change": [5.0, 1.0, 2.0],
                "change_percent": [5.0, 2.0, 4.0],
                "volume": [1_000_000, 2_000_000, float("nan")],
            }
        )
        with patch.object(
            dashboard.backend.api.market, "get_data_loader", return_value=mock_loader
        ):
            client = TestClient(app)
            r = client.get("/api/market/movers", params={"type": "gainers", "limit": 10})

        assert r.status_code == 200
        data = r.json()["data"]
        by_symbol = {row["symbol"]: row for row in data}
        assert "BADPRICE" not in by_symbol
        assert by_symbol["GOOD"]["price"] == 100.0
        assert by_symbol["GOOD"]["volume"] == 1_000_000
        assert by_symbol["BADVOL"]["price"] == 50.0
        assert by_symbol["BADVOL"]["volume"] == 0

    def test_opportunities_skips_non_finite_projection_and_coerces_daily(self, temp_data_dir):
        """NaN confidence/volume must not 500 opportunities or emit null targets."""

        mock_loader = MagicMock()
        mock_loader.get_latest_date.return_value = "2026-01-15"
        mock_loader.load_projections.return_value = pd.DataFrame(
            {
                "symbol": ["KEEP", "BADCONF", "KEEP2"],
                "name": ["Keep Co", "Bad Conf", "Keep Two"],
                "target_mid": [110.0, 120.0, 130.0],
                "expected_change_percent": [10.0, 12.0, 8.0],
                "confidence": [90.0, float("nan"), 80.0],
                "risk_level": ["Low", "High", "Medium"],
                "trend": ["Bullish", "Bullish", "Bullish"],
                "recommendation": ["STRONG BUY", "STRONG BUY", "STRONG BUY"],
                "reason": ["ok", "bad", "ok"],
                "momentum_score": [1.0, 2.0, float("nan")],
                "volatility_score": [0.5, 0.5, 0.4],
            }
        )
        mock_loader.load_daily_data.return_value = pd.DataFrame(
            {
                "symbol": ["KEEP", "BADCONF", "KEEP2"],
                "close": [100.0, 100.0, float("inf")],
                "volume": [1_000_000, 2_000_000, float("nan")],
            }
        )
        with patch.object(
            dashboard.backend.api.projections,
            "get_data_loader",
            return_value=mock_loader,
        ):
            client = TestClient(app)
            r = client.get(
                "/api/projections/opportunities",
                params={"type": "STRONG_BUY", "limit": 10},
            )

        assert r.status_code == 200
        data = r.json()
        assert data["count"] == 2
        by_symbol = {row["symbol"]: row for row in data["opportunities"]}
        assert "BADCONF" not in by_symbol
        assert by_symbol["KEEP"]["currentPrice"] == 100.0
        assert by_symbol["KEEP"]["volume"] == 1_000_000
        assert by_symbol["KEEP"]["confidence"] == 90
        assert by_symbol["KEEP2"]["currentPrice"] == 0.0
        assert by_symbol["KEEP2"]["volume"] == 0
        assert by_symbol["KEEP2"]["momentum"] is None

    def test_market_movers_404_when_change_percent_missing(self, temp_data_dir):
        """Missing change_percent must 404 instead of KeyError→500."""

        mock_loader = MagicMock()
        mock_loader.load_daily_data.return_value = pd.DataFrame({"symbol": ["AAPL"]})
        with patch.object(
            dashboard.backend.api.market, "get_data_loader", return_value=mock_loader
        ):
            client = TestClient(app)
            r = client.get("/api/market/movers", params={"type": "gainers"})

        assert r.status_code == 404
        assert r.json()["detail"] == "No data available."

    def test_market_movers_404_when_daily_empty(self, temp_data_dir):
        """Empty daily frame is treated as no usable movers data."""

        mock_loader = MagicMock()
        mock_loader.load_daily_data.return_value = pd.DataFrame(
            {"change_percent": pd.Series(dtype=float)}
        )
        with patch.object(
            dashboard.backend.api.market, "get_data_loader", return_value=mock_loader
        ):
            client = TestClient(app)
            r = client.get("/api/market/movers", params={"type": "losers"})

        assert r.status_code == 404

    def test_market_movers_404_when_no_daily_files(self, temp_data_dir):
        """ValueError from the loader must stay 404 (not swallowed into 500)."""

        mock_loader = MagicMock()
        mock_loader.load_daily_data.side_effect = ValueError("No daily data files found")
        with patch.object(
            dashboard.backend.api.market, "get_data_loader", return_value=mock_loader
        ):
            client = TestClient(app)
            r = client.get("/api/market/movers", params={"type": "gainers"})

        assert r.status_code == 404
        assert r.json()["detail"] == "No data available."

    def test_market_movers_empty_when_all_unchanged(self, temp_data_dir):
        """All-zero change_percent yields an empty movers list, not opposite-sign fill."""

        mock_loader = MagicMock()
        mock_loader.load_daily_data.return_value = pd.DataFrame(
            {
                "symbol": ["AAPL", "MSFT"],
                "name": ["Apple", "Microsoft"],
                "close": [150.0, 350.0],
                "change": [0.0, 0.0],
                "change_percent": [0.0, 0.0],
                "volume": [1_000_000, 2_000_000],
            }
        )
        with patch.object(
            dashboard.backend.api.market, "get_data_loader", return_value=mock_loader
        ):
            client = TestClient(app)
            gainers = client.get("/api/market/movers", params={"type": "gainers", "limit": 10})
            losers = client.get("/api/market/movers", params={"type": "losers", "limit": 10})

        assert gainers.status_code == 200
        assert gainers.json()["data"] == []
        assert losers.status_code == 200
        assert losers.json()["data"] == []

    def test_opportunities_404_when_no_latest_date(self, temp_data_dir):
        """No latest date must stay 404 (not swallowed into 500)."""

        mock_loader = MagicMock()
        mock_loader.get_latest_date.return_value = None
        with patch.object(
            dashboard.backend.api.projections, "get_data_loader", return_value=mock_loader
        ):
            client = TestClient(app)
            r = client.get("/api/projections/opportunities")

        assert r.status_code == 404
        assert "No data available" in r.json()["detail"]

    def test_opportunities_empty_when_recommendation_column_missing(self, temp_data_dir):
        """Missing recommendation column soft-fails to empty opportunities, not 500."""

        mock_loader = MagicMock()
        mock_loader.get_latest_date.return_value = "2026-01-15"
        mock_loader.load_projections.return_value = pd.DataFrame(
            {"symbol": ["AAPL"], "confidence": [80]}
        )
        mock_loader.load_daily_data.return_value = pd.DataFrame(
            {"symbol": ["AAPL"], "close": [150.0], "volume": [1_000_000]}
        )
        with patch.object(
            dashboard.backend.api.projections, "get_data_loader", return_value=mock_loader
        ):
            client = TestClient(app)
            r = client.get(
                "/api/projections/opportunities",
                params={"type": "STRONG_BUY", "limit": 5},
            )

        assert r.status_code == 200
        data = r.json()
        assert data["type"] == "STRONG_BUY"
        assert data["count"] == 0
        assert data["opportunities"] == []

    def test_opportunities_empty_when_requested_bucket_has_no_matches(self, temp_data_dir):
        """A valid but empty recommendation bucket must return 200, not 500."""

        mock_loader = MagicMock()
        mock_loader.get_latest_date.return_value = "2026-01-15"
        mock_loader.load_projections.return_value = pd.DataFrame(
            {
                "symbol": ["AAPL"],
                "recommendation": ["HOLD"],
                "confidence": [80.0],
            }
        )
        mock_loader.load_daily_data.return_value = pd.DataFrame(
            {"symbol": ["AAPL"], "close": [150.0], "volume": [1_000_000]}
        )
        with patch.object(
            dashboard.backend.api.projections, "get_data_loader", return_value=mock_loader
        ):
            client = TestClient(app)
            r = client.get(
                "/api/projections/opportunities",
                params={"type": "STRONG_BUY", "limit": 5},
            )

        assert r.status_code == 200
        assert r.json() == {
            "type": "STRONG_BUY",
            "count": 0,
            "opportunities": [],
        }

    def test_projections_summary_coerces_all_nan_means(self, temp_data_dir):
        """All-NaN confidence/expected change must yield finite 0.0, not null JSON."""

        mock_loader = MagicMock()
        mock_loader.get_latest_date.return_value = "2026-01-15"
        mock_loader.load_projections.return_value = pd.DataFrame(
            {
                "symbol": ["AAPL", "MSFT"],
                "confidence": [float("nan"), float("nan")],
                "expected_change_percent": [float("nan"), float("nan")],
            }
        )
        with patch.object(
            dashboard.backend.api.projections, "get_data_loader", return_value=mock_loader
        ):
            client = TestClient(app)
            r = client.get("/api/projections/summary")

        assert r.status_code == 200
        data = r.json()
        assert data["totalProjections"] == 2
        assert data["averageConfidence"] == 0.0
        assert data["expectedMarketMove"] == 0.0
        assert data["sentiment"] == "Neutral"

"""
Unit tests for Stock Projector module.

Tests the projection generation, recommendation logic, and confidence scoring.
"""

import math
from datetime import datetime, timezone

import pytest

from src.analysis.market_calendar import trading_session_after_timestamp
from src.analysis.projector import StockProjector


class TestStockProjector:
    """Test suite for StockProjector class."""

    @pytest.fixture
    def projector(self):
        """Create a projector instance for testing."""
        return StockProjector()

    @pytest.fixture
    def sample_stock_bullish(self):
        """Sample bullish stock data."""
        return {
            "symbol": "AAPL",
            "close": 150.00,
            "change_percent": 5.0,
            "volume": 50000000,
            "previous_close": 142.86,
            "market_cap": 2500000,
            "name": "Apple Inc",
        }

    @pytest.fixture
    def sample_stock_bearish(self):
        """Sample bearish stock data."""
        return {
            "symbol": "BEAR",
            "close": 100.00,
            "change_percent": -6.0,
            "volume": 30000000,
            "previous_close": 106.38,
            "market_cap": 1000000,
            "name": "Bear Stock",
        }

    @pytest.fixture
    def sample_stock_neutral(self):
        """Sample neutral stock data."""
        return {
            "symbol": "NEUT",
            "close": 75.00,
            "change_percent": 0.5,
            "volume": 10000000,
            "previous_close": 74.63,
            "market_cap": 500000,
            "name": "Neutral Stock",
        }

    @pytest.fixture
    def sample_stocks_list(self, sample_stock_bullish, sample_stock_bearish, sample_stock_neutral):
        """List of sample stocks for batch testing."""
        return [sample_stock_bullish, sample_stock_bearish, sample_stock_neutral]

    # ========== Initialization Tests ==========

    def test_projector_initialization(self, projector):
        """Test projector initializes correctly."""
        assert projector is not None
        assert projector.projection_days == 5

    # ========== Momentum Calculation Tests ==========

    def test_calculate_momentum_positive(self, projector, sample_stock_bullish):
        """Test momentum calculation for positive moves."""
        momentum = projector._calculate_momentum(sample_stock_bullish)
        assert momentum > 0
        assert momentum == 50.0  # 5.0% * 10 = 50

    def test_calculate_momentum_negative(self, projector, sample_stock_bearish):
        """Test momentum calculation for negative moves."""
        momentum = projector._calculate_momentum(sample_stock_bearish)
        assert momentum < 0
        assert momentum == -60.0  # -6.0% * 10 = -60

    def test_calculate_momentum_capped(self, projector):
        """Test momentum is capped at -100 to +100."""
        extreme_bull = {"change_percent": 15.0}
        extreme_bear = {"change_percent": -15.0}

        bull_momentum = projector._calculate_momentum(extreme_bull)
        bear_momentum = projector._calculate_momentum(extreme_bear)

        assert bull_momentum == 100.0  # Capped at 100
        assert bear_momentum == -100.0  # Capped at -100

    # ========== Volatility Calculation Tests ==========

    def test_calculate_volatility_low(self, projector):
        """Test volatility for stable stocks."""
        stable_stock = {"change_percent": 1.0}
        volatility = projector._calculate_volatility(stable_stock)
        assert volatility == 5.0  # 1.0% * 5 = 5
        assert volatility < 30  # Should be low

    def test_calculate_volatility_high(self, projector):
        """Test volatility for volatile stocks."""
        volatile_stock = {"change_percent": -10.0}
        volatility = projector._calculate_volatility(volatile_stock)
        assert volatility == 50.0  # 10.0% * 5 = 50
        assert volatility > 30  # Should be high

    def test_calculate_volatility_capped(self, projector):
        """Test volatility is capped at 100."""
        extreme_stock = {"change_percent": 25.0}
        volatility = projector._calculate_volatility(extreme_stock)
        assert volatility == 100.0  # Capped at 100

    # ========== Trend Determination Tests ==========

    def test_determine_trend_bullish(self, projector):
        """Test bullish trend detection."""
        trend = projector._determine_trend(momentum=30, change_pct=4.0)
        assert trend == "Bullish"
        trend = projector._determine_trend(momentum=25, change_pct=2.0)
        assert trend == "Bullish"

    def test_determine_trend_bearish(self, projector):
        """Test bearish trend detection."""
        trend = projector._determine_trend(momentum=-30, change_pct=-4.0)
        assert trend == "Bearish"
        trend = projector._determine_trend(momentum=-25, change_pct=-2.0)
        assert trend == "Bearish"

    def test_determine_trend_neutral(self, projector):
        """Test neutral trend detection."""
        trend = projector._determine_trend(momentum=10, change_pct=0.5)
        assert trend == "Neutral"
        trend = projector._determine_trend(momentum=-10, change_pct=-0.5)
        assert trend == "Neutral"

    # ========== Recommendation Generation Tests ==========

    def test_generate_recommendation_strong_buy(self, projector):
        """Test STRONG BUY recommendation."""
        rec = projector._generate_recommendation(momentum=50, volatility=40, change_pct=5.0)
        assert rec == "STRONG BUY"

    def test_generate_recommendation_buy(self, projector):
        """Test BUY recommendation."""
        rec = projector._generate_recommendation(momentum=20, volatility=50, change_pct=2.0)
        assert rec == "BUY"

    def test_generate_recommendation_hold(self, projector):
        """Test HOLD recommendation."""
        rec = projector._generate_recommendation(momentum=5, volatility=40, change_pct=0.5)
        assert rec == "HOLD"

    def test_generate_recommendation_sell(self, projector):
        """Test SELL recommendation."""
        rec = projector._generate_recommendation(momentum=-20, volatility=50, change_pct=-2.0)
        assert rec == "SELL"

    def test_generate_recommendation_strong_sell(self, projector):
        """Test STRONG SELL recommendation."""
        rec = projector._generate_recommendation(momentum=-50, volatility=40, change_pct=-5.0)
        assert rec == "STRONG SELL"

    # ========== Price Target Calculation Tests ==========

    def test_calculate_targets_bullish(self, projector):
        """Test price targets for bullish stock."""
        current_price = 100.00
        low, mid, high = projector._calculate_targets(
            current_price=current_price, momentum=50, volatility=30, change_pct=5.0
        )
        assert low < mid < high
        assert mid > current_price

    def test_calculate_targets_bearish(self, projector):
        """Test price targets for bearish stock."""
        current_price = 100.00
        low, mid, high = projector._calculate_targets(
            current_price=current_price, momentum=-50, volatility=30, change_pct=-5.0
        )
        assert low < mid < high
        assert mid < current_price

    def test_calculate_targets_neutral(self, projector):
        """Test price targets for neutral stock."""
        current_price = 100.00
        low, mid, high = projector._calculate_targets(
            current_price=current_price, momentum=0, volatility=20, change_pct=0.0
        )
        assert low < mid < high
        assert abs(mid - current_price) < 5

    # ========== Confidence Calculation Tests ==========

    def test_calculate_confidence_high(self, projector):
        """Test high confidence scenario."""
        stock = {"volume": 50000000, "market_cap": 2500000}
        confidence = projector._calculate_confidence(
            momentum=50, volatility=20, volume=stock["volume"], stock_data=stock
        )
        assert 70 <= confidence <= 100

    def test_calculate_confidence_low(self, projector):
        """Test low confidence scenario."""
        stock = {"volume": 500000, "market_cap": 50000}
        confidence = projector._calculate_confidence(
            momentum=10, volatility=80, volume=stock["volume"], stock_data=stock
        )
        assert confidence < 70

    def test_calculate_confidence_capped(self, projector):
        """Test confidence is capped at 0-100."""
        stock = {"volume": 100000000, "market_cap": 5000000}
        confidence = projector._calculate_confidence(
            momentum=100, volatility=0, volume=stock["volume"], stock_data=stock
        )
        assert 0 <= confidence <= 100

    # ========== Risk Assessment Tests ==========

    def test_assess_risk_low(self, projector):
        """Test low risk assessment."""
        assert projector._assess_risk(volatility=20) == "Low"

    def test_assess_risk_medium(self, projector):
        """Test medium risk assessment."""
        assert projector._assess_risk(volatility=45) == "Medium"

    def test_assess_risk_high(self, projector):
        """Test high risk assessment."""
        assert projector._assess_risk(volatility=75) == "High"

    # ========== Reason Generation Tests ==========

    def test_generate_reason_bullish(self, projector, sample_stock_bullish):
        """Test reason generation for bullish stock."""
        reason = projector._generate_reason(
            stock_data=sample_stock_bullish,
            momentum=50,
            trend="Bullish",
            recommendation="STRONG BUY",
        )
        assert isinstance(reason, str)
        assert len(reason) > 0

    def test_generate_reason_bearish(self, projector, sample_stock_bearish):
        """Test reason generation for bearish stock."""
        reason = projector._generate_reason(
            stock_data=sample_stock_bearish,
            momentum=-60,
            trend="Bearish",
            recommendation="STRONG SELL",
        )
        assert isinstance(reason, str)
        assert len(reason) > 0

    # ========== Single Stock Projection Tests ==========

    def test_project_stock_bullish(self, projector, sample_stock_bullish):
        """Test projection for bullish stock."""
        projection = projector._project_stock(sample_stock_bullish)
        assert projection is not None
        assert projection["symbol"] == "AAPL"
        assert projection["current_price"] == 150.00
        assert projection["target_mid"] > 0
        assert projection["recommendation"] in ["STRONG BUY", "BUY", "HOLD", "SELL", "STRONG SELL"]

    def test_project_stock_bearish(self, projector, sample_stock_bearish):
        """Test projection for bearish stock."""
        projection = projector._project_stock(sample_stock_bearish)
        assert projection is not None
        assert projection["symbol"] == "BEAR"
        assert projection["trend"] == "Bearish"

    def test_project_stock_invalid_price(self, projector):
        """Test projection with invalid price returns None."""
        invalid_stock = {
            "symbol": "INVALID",
            "close": 0,
            "change_percent": 5.0,
            "volume": 1000000,
            "previous_close": 100,
            "market_cap": 50000,
        }
        assert projector._project_stock(invalid_stock) is None

    def test_project_stock_negative_price(self, projector):
        """Test projection with negative price returns None."""
        invalid_stock = {
            "symbol": "INVALID",
            "close": -50,
            "change_percent": 5.0,
            "volume": 1000000,
            "previous_close": 100,
            "market_cap": 50000,
        }
        assert projector._project_stock(invalid_stock) is None

    def test_project_stock_nan_close_returns_none(self, projector):
        """NaN close must not emit NaN projection targets."""
        nan_close = {
            "symbol": "NANC",
            "close": float("nan"),
            "change_percent": 2.0,
            "volume": 1000000,
            "previous_close": 100,
            "market_cap": 50000,
        }
        assert projector._project_stock(nan_close) is None

    def test_project_stock_nan_change_percent_returns_none(self, projector):
        """NaN change_percent must not poison momentum/targets."""
        nan_change = {
            "symbol": "NANP",
            "close": 100.0,
            "change_percent": float("nan"),
            "volume": 1000000,
            "previous_close": 98.0,
            "market_cap": 50000,
        }
        assert projector._project_stock(nan_change) is None

    def test_project_stock_non_numeric_close_returns_none(self, projector):
        """Non-numeric close is rejected before target math."""
        bad = {
            "symbol": "BAD",
            "close": "n/a",
            "change_percent": 2.0,
            "volume": 1000000,
            "previous_close": 100,
            "market_cap": 50000,
        }
        assert projector._project_stock(bad) is None

    def test_project_stock_non_numeric_volume_still_projects(self, projector):
        """Bad optional volume must not abort an otherwise valid projection."""
        stock = {
            "symbol": "VOLX",
            "close": 100.0,
            "change_percent": 2.0,
            "volume": "n/a",
            "previous_close": 98.0,
            "market_cap": 50000,
        }
        projection = projector._project_stock(stock)
        assert projection is not None
        assert projection["symbol"] == "VOLX"
        assert isinstance(projection["confidence"], int)

    def test_project_stock_non_finite_market_cap_still_projects(self, projector):
        """NaN/inf market_cap must not abort confidence scoring."""
        stock = {
            "symbol": "CAPX",
            "close": 100.0,
            "change_percent": 2.0,
            "volume": 1000000,
            "previous_close": 98.0,
            "market_cap": float("nan"),
        }
        projection = projector._project_stock(stock)
        assert projection is not None
        assert projection["symbol"] == "CAPX"

    # ========== Batch Projection Tests ==========

    def test_generate_projections_multiple_stocks(self, projector, sample_stocks_list):
        """Test generating projections for multiple stocks."""
        projections = projector.generate_projections(sample_stocks_list)
        assert len(projections) == 3
        assert "AAPL" in projections and "BEAR" in projections and "NEUT" in projections

    def test_generate_projections_empty_list(self, projector):
        """Test generating projections for empty list."""
        assert projector.generate_projections([]) == {}

    def test_generate_projections_filters_invalid(self, projector):
        """Test that invalid stocks are filtered out."""
        stocks = [
            {
                "symbol": "VALID",
                "close": 100,
                "change_percent": 2.0,
                "volume": 1000000,
                "previous_close": 98,
                "market_cap": 50000,
            },
            {
                "symbol": "INVALID",
                "close": 0,
                "change_percent": 2.0,
                "volume": 1000000,
                "previous_close": 98,
                "market_cap": 50000,
            },
        ]
        projections = projector.generate_projections(stocks)
        assert len(projections) == 1 and "VALID" in projections

    # ========== Projection Summary Tests ==========

    def test_generate_projection_summary(self, projector, sample_stocks_list):
        """Test projection summary generation."""
        projections = projector.generate_projections(sample_stocks_list)
        summary = projector.generate_projection_summary(projections)
        assert summary["total_projections"] == 3
        assert "recommendations" in summary
        assert summary["projection_horizon_sessions"] == 5
        assert summary["projection_calendar"] == "XNYS"

    def test_projection_summary_empty(self, projector):
        """Test projection summary with no projections."""
        assert projector.generate_projection_summary({}) == {}

    def test_projection_summary_recommendations_count(self, projector, sample_stocks_list):
        """Test recommendation counts in summary."""
        projections = projector.generate_projections(sample_stocks_list)
        summary = projector.generate_projection_summary(projections)
        assert sum(summary["recommendations"].values()) == len(projections)

    def test_projection_summary_top_opportunities_ranked_and_capped(self, projector):
        """Top opportunity lists stay confidence-sorted and capped at five."""
        projections = {}
        for i in range(7):
            symbol = f"BUY{i}"
            projections[symbol] = {
                "symbol": symbol,
                "target_mid": 100 + i,
                "confidence": 50 + i,
                "expected_change_percent": float(i),
                "recommendation": "STRONG BUY",
                "trend": "up",
            }
        for i in range(6):
            symbol = f"SELL{i}"
            projections[symbol] = {
                "symbol": symbol,
                "target_mid": 90 - i,
                "confidence": 40 + i,
                "expected_change_percent": -float(i),
                "recommendation": "STRONG SELL",
                "trend": "down",
            }

        summary = projector.generate_projection_summary(projections)
        buys = summary["top_opportunities"]["strong_buys"]
        sells = summary["top_opportunities"]["strong_sells"]

        assert len(buys) == 5
        assert [row["symbol"] for row in buys] == ["BUY6", "BUY5", "BUY4", "BUY3", "BUY2"]
        assert [row["confidence"] for row in buys] == [56, 55, 54, 53, 52]
        assert len(sells) == 5
        assert [row["symbol"] for row in sells] == ["SELL5", "SELL4", "SELL3", "SELL2", "SELL1"]
        assert summary["total_projections"] == 13

    def test_projection_summary_ignores_non_finite_confidence_and_change(self, projector):
        """NaN/inf confidence or expected_change must not poison averages or rankings."""
        projections = {
            "OK": {
                "symbol": "OK",
                "target_mid": 110.0,
                "confidence": 70.0,
                "expected_change_percent": 2.5,
                "recommendation": "STRONG BUY",
                "trend": "up",
            },
            "NANCONF": {
                "symbol": "NANCONF",
                "target_mid": 999.0,
                "confidence": float("nan"),
                "expected_change_percent": 50.0,
                "recommendation": "STRONG BUY",
                "trend": "up",
            },
            "INFCHG": {
                "symbol": "INFCHG",
                "target_mid": 120.0,
                "confidence": 90.0,
                "expected_change_percent": float("inf"),
                "recommendation": "STRONG SELL",
                "trend": "down",
            },
        }

        summary = projector.generate_projection_summary(projections)

        assert summary["average_confidence"] == 80.0  # (70 + 90) / 2; NaN confidence skipped
        assert summary["average_expected_change"] == 26.25  # (2.5 + 50) / 2; inf skipped
        assert [row["symbol"] for row in summary["top_opportunities"]["strong_buys"]] == ["OK"]
        assert [row["symbol"] for row in summary["top_opportunities"]["strong_sells"]] == ["INFCHG"]
        assert math.isfinite(summary["average_confidence"])
        assert math.isfinite(summary["average_expected_change"])

    # ========== Date and Timestamp Tests ==========

    def test_projection_has_dates(self, projector, sample_stock_bullish):
        """Test that projections include date fields."""
        projection = projector._project_stock(sample_stock_bullish)
        assert "projection_date" in projection and "generated_at" in projection

    def test_projection_date_is_5_market_sessions_ahead(self, projector, sample_stock_bullish):
        """Projection target uses the configured exchange-session horizon."""
        projection = projector._project_stock(sample_stock_bullish)

        generated = datetime.fromisoformat(projection["generated_at"])
        assert generated.utcoffset() is not None
        assert (
            projection["projection_date"]
            == trading_session_after_timestamp(generated, 5).isoformat()
        )
        assert projection["projection_horizon_sessions"] == 5
        assert projection["projection_calendar"] == "XNYS"

    def test_premarket_projection_includes_upcoming_session(
        self, projector, sample_stock_bullish, monkeypatch
    ):
        """A premarket Monday run counts Monday as the first future session."""

        class PremarketDateTime:
            @classmethod
            def now(cls, tz=None):
                return datetime(2026, 7, 6, 12, tzinfo=timezone.utc)

        monkeypatch.setattr("src.analysis.projector.datetime", PremarketDateTime)

        projection = projector._project_stock(sample_stock_bullish)

        assert projection["generated_at"] == "2026-07-06T12:00:00+00:00"
        assert projection["projection_date"] == "2026-07-10"

    # ========== Edge Cases ==========

    def test_missing_optional_fields(self, projector):
        """Test projection with missing optional fields."""
        minimal_stock = {
            "symbol": "MIN",
            "close": 50.0,
            "change_percent": 2.0,
            "volume": 0,
            "previous_close": 49.0,
        }
        projection = projector._project_stock(minimal_stock)
        assert projection is not None and projection["symbol"] == "MIN"

    def test_zero_volume_stock(self, projector):
        """Test projection for stock with zero volume."""
        zero_volume = {
            "symbol": "ZV",
            "close": 100.0,
            "change_percent": 2.0,
            "volume": 0,
            "previous_close": 98.0,
            "market_cap": 50000,
        }
        projection = projector._project_stock(zero_volume)
        assert projection is not None and projection["confidence"] < 80

    def test_extreme_price_movement(self, projector):
        """Test projection for extreme price movement."""
        extreme_stock = {
            "symbol": "EXT",
            "close": 200.0,
            "change_percent": 50.0,
            "volume": 100000000,
            "previous_close": 133.33,
            "market_cap": 1000000,
        }
        projection = projector._project_stock(extreme_stock)
        assert projection is not None


if __name__ == "__main__":
    pytest.main([__file__, "-v", "--tb=short"])

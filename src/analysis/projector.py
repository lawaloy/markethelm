"""
Stock Projector - Price Projection and Recommendation Engine

This module analyzes historical stock data and generates:
- Short-term price projections (1-5 days)
- Buy/Hold/Sell recommendations
- Confidence scores
- Risk assessments
"""

import math
from datetime import datetime, timezone
from typing import Dict, List, Optional, Tuple

import pandas as pd

from ..core.logger import get_logger
from ..utils.company_names import resolve_company_name
from ..utils.tickers import normalize_ticker
from .market_calendar import trading_session_after_timestamp

logger = get_logger(__name__)


class StockProjector:
    """
    Generates stock projections based on technical analysis and trend patterns.

    Uses multiple factors:
    - Recent momentum (1-5 day trends)
    - Volatility analysis
    - Volume patterns
    - Support/resistance levels
    """

    def __init__(self):
        """Initialize the projector."""
        self.projection_days = 5  # Default projection window
        logger.debug("Stock projector initialized")

    def generate_projections(
        self, current_data: List[Dict], historical_data: Optional[List[Dict]] = None
    ) -> Dict[str, Dict]:
        """
        Generate projections for all stocks.

        Args:
            current_data: Current day's stock data
            historical_data: Historical data for trend analysis (optional)

        Returns:
            Dictionary mapping symbols to their projections
        """
        # Dirty callers can pass None/dict/scalar; soft-fail so projection batch
        # never TypeErrors on len()/iteration and aborts the daily run.
        if not isinstance(current_data, list):
            logger.warning(
                "generate_projections expected a list, got %s; returning empty",
                type(current_data).__name__,
            )
            return {}

        logger.info(f"Generating projections for {len(current_data)} stocks")

        projections = {}

        for stock in current_data:
            # Dirty fetch/watch merges can inject None/str rows; skip them so
            # one bad entry cannot AttributeError the whole daily projection run.
            if not isinstance(stock, dict):
                continue
            # NaN/NONE/blank symbols are truthy or collapse under one key — skip
            # and canonicalize so projection maps never store float("nan").
            symbol = normalize_ticker(stock.get("symbol"))
            if not symbol:
                continue
            stock = {**stock, "symbol": symbol}

            projection = self._project_stock(stock, historical_data)
            if projection:
                projections[symbol] = projection

        logger.info(f"Generated {len(projections)} projections")
        return projections

    def _project_stock(
        self, stock_data: Dict, historical_data: Optional[List[Dict]] = None
    ) -> Optional[Dict]:
        """
        Generate projection for a single stock.

        Args:
            stock_data: Current stock data
            historical_data: Historical data for the stock

        Returns:
            Projection dictionary with targets and recommendations
        """
        try:
            symbol = stock_data.get("symbol")
            try:
                current_price = float(stock_data.get("close", 0) or 0)
                change_pct = float(stock_data.get("change_percent", 0) or 0)
            except (TypeError, ValueError):
                return None
            # Optional fields: bad volume/market_cap must not abort projection.
            try:
                volume = float(stock_data.get("volume", 0) or 0)
                if not math.isfinite(volume):
                    volume = 0.0
            except (TypeError, ValueError):
                volume = 0.0

            # NaN closes pass `<= 0` and would write NaN targets into projections CSV.
            if not math.isfinite(current_price) or current_price <= 0:
                return None
            if not math.isfinite(change_pct):
                return None

            # Calculate momentum score (-100 to +100)
            stock_for_metrics = {**stock_data, "change_percent": change_pct}
            # Keep confidence math on coerced optionals even when raw cells are garbage.
            try:
                market_cap = float(stock_data.get("market_cap", 0) or 0)
                if not math.isfinite(market_cap):
                    market_cap = 0.0
            except (TypeError, ValueError):
                market_cap = 0.0
            stock_for_confidence = {**stock_for_metrics, "market_cap": market_cap}
            momentum = self._calculate_momentum(stock_for_metrics)

            # Calculate volatility (0 to 100)
            volatility = self._calculate_volatility(stock_for_metrics)

            # Determine trend direction
            trend = self._determine_trend(momentum, change_pct)

            # Calculate price targets
            target_low, target_mid, target_high = self._calculate_targets(
                current_price, momentum, volatility, change_pct
            )

            # Generate recommendation
            recommendation = self._generate_recommendation(momentum, volatility, change_pct)

            # Calculate confidence score
            confidence = self._calculate_confidence(
                momentum, volatility, volume, stock_for_confidence
            )

            # Generate reasoning (use coerced optionals ΓÇö raw volume/change can be non-numeric)
            reason = self._generate_reason(
                {**stock_for_confidence, "volume": volume},
                momentum,
                trend,
                recommendation,
            )

            # Resolve company name - API often returns symbol when profile fetch fails
            raw_name = stock_data.get("name", symbol)
            company_name = resolve_company_name(symbol, raw_name)
            generated_at = datetime.now(timezone.utc)
            target_session = trading_session_after_timestamp(generated_at, self.projection_days)

            projection = {
                "symbol": symbol,
                "name": company_name,
                "current_price": round(current_price, 2),
                "target_low": round(target_low, 2),
                "target_mid": round(target_mid, 2),
                "target_high": round(target_high, 2),
                "expected_change_percent": round(
                    ((target_mid - current_price) / current_price) * 100, 2
                ),
                "recommendation": recommendation,
                "confidence": confidence,
                "trend": trend,
                "momentum_score": round(momentum, 1),
                "volatility_score": round(volatility, 1),
                "risk_level": self._assess_risk(volatility),
                "reason": reason,
                "projection_date": target_session.isoformat(),
                "projection_horizon_sessions": self.projection_days,
                "projection_calendar": "XNYS",
                "generated_at": generated_at.isoformat(),
            }

            logger.debug(f"Projection for {symbol}: {recommendation} @ ${target_mid:.2f}")
            return projection

        except Exception as e:
            logger.warning(f"Failed to project {stock_data.get('symbol')}: {e}")
            return None

    def _calculate_momentum(self, stock_data: Dict) -> float:
        """
        Calculate momentum score based on recent price action.

        Returns:
            Momentum score from -100 (strong bearish) to +100 (strong bullish)
        """
        change_pct = stock_data.get("change_percent", 0)

        # Base momentum on daily change, scaled and capped
        # Strong moves (>5%) get full weight
        momentum = change_pct * 10  # Scale to -100 to +100 range
        momentum = max(-100, min(100, momentum))  # Cap at -100 to +100

        return momentum

    def _calculate_volatility(self, stock_data: Dict) -> float:
        """
        Calculate volatility score.

        Returns:
            Volatility score from 0 (stable) to 100 (highly volatile)
        """
        change_pct = abs(stock_data.get("change_percent", 0))

        # Higher daily changes indicate higher volatility
        volatility = change_pct * 5  # Scale to 0-100 range
        volatility = min(100, volatility)  # Cap at 100

        return volatility

    def _determine_trend(self, momentum: float, change_pct: float) -> str:
        """
        Determine overall trend direction.

        Returns:
            'Bullish', 'Bearish', or 'Neutral'
        """
        if momentum > 20 or change_pct > 3:
            return "Bullish"
        elif momentum < -20 or change_pct < -3:
            return "Bearish"
        else:
            return "Neutral"

    def _calculate_targets(
        self, current_price: float, momentum: float, volatility: float, change_pct: float
    ) -> Tuple[float, float, float]:
        """
        Calculate price targets (low, mid, high) for next 5 days.

        Returns:
            Tuple of (target_low, target_mid, target_high)
        """
        # Base projection on momentum and volatility
        # Momentum determines direction, volatility determines range

        # Calculate expected move percentage
        momentum_factor = momentum / 100  # -1 to +1
        volatility_factor = volatility / 100  # 0 to 1

        # Base expected move on recent momentum
        base_move_pct = momentum_factor * 5  # Up to ┬▒5% base move

        # Adjust for continuation or mean reversion
        if abs(change_pct) > 5:
            # Large moves often see partial reversal
            base_move_pct *= 0.5
        elif abs(change_pct) < 1:
            # Small moves may continue
            base_move_pct *= 1.2

        # Calculate range based on volatility
        range_pct = volatility_factor * 3  # Up to 3% range

        # Calculate targets
        target_mid = current_price * (1 + base_move_pct / 100)
        target_low = target_mid * (1 - range_pct / 100)
        target_high = target_mid * (1 + range_pct / 100)

        return target_low, target_mid, target_high

    def _generate_recommendation(
        self, momentum: float, volatility: float, change_pct: float
    ) -> str:
        """
        Generate buy/hold/sell recommendation.

        Returns:
            'STRONG BUY', 'BUY', 'HOLD', 'SELL', or 'STRONG SELL'
        """
        # Strong momentum with reasonable volatility = Strong Buy/Sell
        if momentum > 40 and volatility < 60:
            return "STRONG BUY"
        elif momentum > 15:
            return "BUY"
        elif momentum < -40 and volatility < 60:
            return "STRONG SELL"
        elif momentum < -15:
            return "SELL"
        else:
            return "HOLD"

    def _calculate_confidence(
        self, momentum: float, volatility: float, volume: float, stock_data: Dict
    ) -> int:
        """
        Calculate confidence score for the projection.

        Returns:
            Confidence score from 0 to 100
        """
        confidence = 50  # Base confidence

        # Higher momentum = higher confidence
        if abs(momentum) > 30:
            confidence += 20
        elif abs(momentum) > 15:
            confidence += 10

        # Lower volatility = higher confidence
        if volatility < 30:
            confidence += 15
        elif volatility < 50:
            confidence += 5
        elif volatility > 80:
            confidence -= 15

        # High volume = higher confidence
        if volume > 10000000:
            confidence += 10
        elif volume > 5000000:
            confidence += 5

        # Large market cap = higher confidence
        market_cap = stock_data.get("market_cap", 0)
        if market_cap > 100000:  # >$100B
            confidence += 5

        # Cap confidence at 0-100
        confidence = max(0, min(100, confidence))

        return confidence

    def _assess_risk(self, volatility: float) -> str:
        """
        Assess risk level based on volatility.

        Returns:
            'Low', 'Medium', or 'High'
        """
        if volatility < 30:
            return "Low"
        elif volatility < 60:
            return "Medium"
        else:
            return "High"

    def _generate_reason(
        self, stock_data: Dict, momentum: float, trend: str, recommendation: str
    ) -> str:
        """
        Generate human-readable reason for the projection.

        Returns:
            Explanation string
        """
        change_pct = stock_data.get("change_percent", 0)
        volume = stock_data.get("volume", 0)
        try:
            change_pct = float(change_pct or 0)
            if not math.isfinite(change_pct):
                change_pct = 0.0
        except (TypeError, ValueError):
            change_pct = 0.0
        try:
            volume = float(volume or 0)
            if not math.isfinite(volume):
                volume = 0.0
        except (TypeError, ValueError):
            volume = 0.0

        reasons = []

        # Trend analysis
        if trend == "Bullish":
            if change_pct > 5:
                reasons.append(f"Strong +{change_pct:.1f}% gain")
            elif change_pct > 0:
                reasons.append(f"Positive +{change_pct:.1f}% momentum")
        elif trend == "Bearish":
            if change_pct < -5:
                reasons.append(f"Sharp {change_pct:.1f}% decline")
            elif change_pct < 0:
                reasons.append(f"Negative {change_pct:.1f}% pressure")
        else:
            reasons.append(f"Stable {change_pct:+.1f}% change")

        # Momentum context
        if abs(momentum) > 50:
            reasons.append("very strong momentum")
        elif abs(momentum) > 30:
            reasons.append("strong momentum")
        elif abs(momentum) > 15:
            reasons.append("moderate momentum")

        # Volume context
        if volume > 10000000:
            reasons.append("high volume support")
        elif volume < 1000000:
            reasons.append("low volume caution")

        # Mean reversion potential
        if abs(change_pct) > 7:
            reasons.append("potential for reversal")
        elif abs(change_pct) < 1:
            reasons.append("trend likely to continue")

        return "; ".join(reasons)

    def generate_projection_summary(self, projections: Dict[str, Dict]) -> Dict:
        """
        Generate summary statistics for all projections.

        Args:
            projections: Dictionary of stock projections

        Returns:
            Summary dictionary with aggregate statistics
        """
        if not projections:
            return {}

        df = pd.DataFrame(projections.values())

        def _finite_series(column: str) -> pd.Series:
            numeric = pd.to_numeric(df[column], errors="coerce")
            return numeric.map(
                lambda value: (
                    float(value) if pd.notna(value) and math.isfinite(value) else float("nan")
                )
            )

        confidence = _finite_series("confidence")
        expected_change = _finite_series("expected_change_percent")
        finite_confidence = confidence.notna()

        # Count recommendations
        rec_counts = df["recommendation"].value_counts().to_dict()

        # Count trends
        trend_counts = df["trend"].value_counts().to_dict()

        # Calculate averages from finite values only (inf/NaN must not poison JSON).
        avg_confidence = (
            float(confidence[finite_confidence].mean()) if finite_confidence.any() else 0.0
        )
        finite_expected = expected_change.notna()
        avg_expected_change = (
            float(expected_change[finite_expected].mean()) if finite_expected.any() else 0.0
        )

        ranked = df.loc[finite_confidence].copy()
        ranked["_confidence"] = confidence.loc[finite_confidence]
        ranked["_target_mid"] = pd.to_numeric(ranked["target_mid"], errors="coerce").map(
            lambda value: float(value) if pd.notna(value) and math.isfinite(value) else 0.0
        )

        # Identify top opportunities
        strong_buys = [
            {
                "symbol": row["symbol"],
                "target_mid": float(row["_target_mid"]),
                "confidence": float(row["_confidence"]),
            }
            for _, row in ranked[ranked["recommendation"] == "STRONG BUY"]
            .nlargest(5, "_confidence")
            .iterrows()
        ]

        strong_sells = [
            {
                "symbol": row["symbol"],
                "target_mid": float(row["_target_mid"]),
                "confidence": float(row["_confidence"]),
            }
            for _, row in ranked[ranked["recommendation"] == "STRONG SELL"]
            .nlargest(5, "_confidence")
            .iterrows()
        ]

        generated_at = datetime.now(timezone.utc)
        summary = {
            "total_projections": len(projections),
            "recommendations": rec_counts,
            "trends": trend_counts,
            "average_confidence": round(avg_confidence, 1),
            "average_expected_change": round(avg_expected_change, 2),
            "top_opportunities": {"strong_buys": strong_buys, "strong_sells": strong_sells},
            "projection_date": trading_session_after_timestamp(
                generated_at, self.projection_days
            ).isoformat(),
            "projection_horizon_sessions": self.projection_days,
            "projection_calendar": "XNYS",
            "generated_at": generated_at.isoformat(),
        }

        logger.info(f"Projection summary: {rec_counts}")
        return summary

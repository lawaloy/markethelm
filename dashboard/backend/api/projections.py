"""
Projections API endpoints
"""

import math
from typing import Any, Optional

import pandas as pd
from fastapi import APIRouter, HTTPException, Query

from dashboard.backend.models.projection import (
    OpportunitiesResponse,
    Opportunity,
    ProjectionsSummary,
)
from dashboard.backend.services.data_loader import get_data_loader
from src.analysis.market_calendar import projection_target_date
from src.utils.tickers import normalize_ticker

router = APIRouter()


def _finite_float(value: Any) -> Optional[float]:
    """Return float when finite; otherwise None (missing/non-numeric/NaN/Inf)."""
    try:
        if value is None:
            return None
        result = float(value)
        if not math.isfinite(result):
            return None
        return result
    except (TypeError, ValueError):
        return None


def _finite_column_mean(df: pd.DataFrame, column: str, default: float = 0.0) -> float:
    """Mean of finite numeric cells only so Inf/NaN cannot poison summary averages."""
    if column not in df.columns:
        return default
    series = pd.to_numeric(df[column], errors="coerce")
    finite = series.map(lambda value: isinstance(value, (int, float)) and math.isfinite(value))
    if not bool(finite.any()):
        return default
    return _finite_float(series[finite].mean()) or default


def _safe_volume(value: Any) -> int:
    """Coerce volume to a finite int; bad/missing cells become 0 (never abort the list)."""
    try:
        if value is None:
            return 0
        result = float(value)
        if not math.isfinite(result):
            return 0
        return int(result)
    except (TypeError, ValueError):
        return 0


def _safe_label(value: Any, default: str) -> str:
    """Return a display label; blank/NaN sentinels fall back instead of crashing."""
    if value is None:
        return default
    try:
        text = str(value).strip()
    except Exception:
        return default
    if not text or text.lower() in {"nan", "<na>", "none"}:
        return default
    return text


@router.get("/summary", response_model=ProjectionsSummary)
async def get_projections_summary():
    """Get projections summary with statistics"""
    try:
        loader = get_data_loader()
        date = loader.get_latest_date()

        if not date:
            raise HTTPException(status_code=404, detail="No data available")

        # Load projections data
        df = loader.load_projections()

        projection = df.iloc[0].to_dict() if not df.empty else {}
        target_date = projection_target_date(projection, date).isoformat()

        # Calculate statistics
        total_projections = len(df)
        avg_confidence = _finite_column_mean(df, "confidence")
        avg_expected_change = _finite_column_mean(df, "expected_change_percent")

        # Determine sentiment
        if avg_expected_change > 1.0:
            sentiment = "Bullish"
        elif avg_expected_change < -1.0:
            sentiment = "Bearish"
        else:
            sentiment = "Neutral"

        # Count recommendations
        recommendations = {}
        if "recommendation" in df.columns:
            rec_counts = df["recommendation"].value_counts().to_dict()
            recommendations = {
                "STRONG_BUY": rec_counts.get("STRONG BUY", 0),
                "BUY": rec_counts.get("BUY", 0),
                "HOLD": rec_counts.get("HOLD", 0),
                "SELL": rec_counts.get("SELL", 0),
                "STRONG_SELL": rec_counts.get("STRONG SELL", 0),
            }

        # Count trends
        trends = {}
        if "trend" in df.columns:
            trend_counts = df["trend"].value_counts().to_dict()
            trends = {
                "Bullish": trend_counts.get("Bullish", 0),
                "Neutral": trend_counts.get("Neutral", 0),
                "Bearish": trend_counts.get("Bearish", 0),
            }

        # Count risk levels
        risk_profile = {}
        if "risk_level" in df.columns:
            risk_counts = df["risk_level"].value_counts().to_dict()
            risk_profile = {
                "Low": risk_counts.get("Low", 0),
                "Medium": risk_counts.get("Medium", 0),
                "High": risk_counts.get("High", 0),
            }

        return ProjectionsSummary(
            date=date,
            targetDate=target_date,
            totalProjections=total_projections,
            averageConfidence=round(avg_confidence, 1),
            expectedMarketMove=round(avg_expected_change, 2),
            sentiment=sentiment,
            recommendations=recommendations,
            trends=trends,
            riskProfile=risk_profile,
        )

    except HTTPException:
        raise
    except ValueError:
        raise HTTPException(status_code=404, detail="No data available.")
    except Exception:
        raise HTTPException(status_code=500, detail="Something went wrong. Please try again.")


@router.get("/opportunities", response_model=OpportunitiesResponse)
async def get_opportunities(
    type: str = Query("STRONG_BUY", pattern="^(STRONG_BUY|BUY|HOLD|SELL|STRONG_SELL)$"),
    limit: int = Query(10, ge=1, le=50),
):
    """Get top opportunities by recommendation type"""
    try:
        loader = get_data_loader()
        date = loader.get_latest_date()

        if not date:
            raise HTTPException(status_code=404, detail="No data available")

        # Load projections and daily data
        proj_df = loader.load_projections()
        daily_df = loader.load_daily_data()

        # Missing ranking columns → empty list (not KeyError→500)
        if (
            proj_df is None
            or getattr(proj_df, "empty", False)
            or "recommendation" not in proj_df.columns
            or "confidence" not in proj_df.columns
        ):
            return OpportunitiesResponse(type=type, count=0, opportunities=[])

        # Map type to recommendation string
        rec_map = {
            "STRONG_BUY": "STRONG BUY",
            "BUY": "BUY",
            "HOLD": "HOLD",
            "SELL": "SELL",
            "STRONG_SELL": "STRONG SELL",
        }

        # Filter by recommendation
        filtered_df = proj_df[proj_df["recommendation"] == rec_map[type]]

        # An empty Series mapped below keeps a non-boolean dtype in pandas.
        # Using it as a DataFrame mask then removes every column, causing
        # nlargest("confidence") to raise KeyError and turning a legitimate
        # no-matches result into a 500 response.
        if filtered_df.empty:
            return OpportunitiesResponse(type=type, count=0, opportunities=[])

        # Dirty CSV confidence (strings / object dtype) makes nlargest TypeError → 500.
        # Coerce to numeric and drop non-finite before ranking; row loop still re-checks.
        ranking_df = filtered_df.copy()
        ranking_df["confidence"] = pd.to_numeric(ranking_df["confidence"], errors="coerce")
        ranking_df = ranking_df[
            ranking_df["confidence"].map(lambda value: _finite_float(value) is not None)
        ]
        sorted_df = ranking_df.nlargest(limit, "confidence")

        opportunities = []
        for _, row in sorted_df.iterrows():
            # Match padded / mixed-case CSV symbols the same way stock detail does.
            symbol = normalize_ticker(row.get("symbol"))
            if not symbol:
                continue

            # Required projection numerics: skip the row instead of int(NaN)→500
            # or Pydantic serializing NaN/Inf as JSON null.
            target_price = _finite_float(row.get("target_mid"))
            expected_change = _finite_float(row.get("expected_change_percent"))
            confidence_f = _finite_float(row.get("confidence"))
            if target_price is None or expected_change is None or confidence_f is None:
                continue

            # Get current price from daily data (normalize like stock detail).
            # Missing daily schema must soft-fail to zeros — not KeyError→500.
            if (
                daily_df is None
                or getattr(daily_df, "empty", True)
                or "symbol" not in getattr(daily_df, "columns", [])
            ):
                current_price = 0.0
                volume = 0
            else:
                stock_daily = daily_df[daily_df["symbol"].map(normalize_ticker) == symbol]
                if stock_daily.empty:
                    current_price = 0.0
                    volume = 0
                else:
                    daily_row = stock_daily.iloc[0]
                    current_price = _finite_float(daily_row.get("close")) or 0.0
                    volume = _safe_volume(daily_row.get("volume", 0))

            momentum = (
                _finite_float(row.get("momentum_score")) if "momentum_score" in row.index else None
            )
            volatility = (
                _finite_float(row.get("volatility_score"))
                if "volatility_score" in row.index
                else None
            )

            opportunities.append(
                Opportunity(
                    symbol=symbol,
                    # Dirty CSV name/reason cells (NaN/None) fail Pydantic str → 500.
                    name=_safe_label(row.get("name"), symbol),
                    currentPrice=current_price,
                    targetPrice=target_price,
                    expectedChange=expected_change,
                    confidence=int(confidence_f),
                    risk=_safe_label(row.get("risk_level"), "Unknown"),
                    # Echo the filter bucket so the dashboard table can filter/badge
                    # by BUY/HOLD/SELL without conflating it with Bullish/Bearish trend.
                    recommendation=rec_map[type],
                    trend=_safe_label(row.get("trend"), "Neutral"),
                    reason=_safe_label(row.get("reason"), ""),
                    volume=volume,
                    momentum=momentum,
                    volatility=volatility,
                )
            )

        return OpportunitiesResponse(
            type=type, count=len(opportunities), opportunities=opportunities
        )

    except HTTPException:
        raise
    except ValueError:
        raise HTTPException(status_code=404, detail="No data available.")
    except Exception:
        raise HTTPException(status_code=500, detail="Something went wrong. Please try again.")

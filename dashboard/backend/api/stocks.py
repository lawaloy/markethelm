"""
Stocks API endpoints
"""

import math
from typing import Any, Optional

from fastapi import APIRouter, HTTPException, Path, Query

from dashboard.backend.models.stock import (
    CurrentData,
    HistoricalData,
    HistoricalPoint,
    ProjectionData,
    StockDetail,
    TechnicalData,
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


@router.get("/{symbol}", response_model=StockDetail)
async def get_stock_detail(symbol: str = Path(..., description="Stock symbol")):
    """Get detailed information for a specific stock"""
    try:
        sym = normalize_ticker(symbol)
        if not sym:
            raise HTTPException(status_code=404, detail="Stock not found.")

        loader = get_data_loader()
        date = loader.get_latest_date()

        if not date:
            raise HTTPException(status_code=404, detail="No data available")

        # Load daily data — match padded / mixed-case CSV symbols to the path key.
        daily_df = loader.load_daily_data()
        if (
            daily_df is None
            or getattr(daily_df, "empty", True)
            or "symbol" not in getattr(daily_df, "columns", [])
        ):
            raise HTTPException(status_code=404, detail="Stock not found.")

        stock_daily = daily_df[daily_df["symbol"].map(normalize_ticker) == sym]

        if stock_daily.empty:
            raise HTTPException(status_code=404, detail="Stock not found.")

        stock_row = stock_daily.iloc[0]
        for col in ("close", "change", "change_percent"):
            if col not in stock_row.index:
                raise HTTPException(status_code=404, detail="Stock not found.")

        price = _finite_float(stock_row["close"])
        change = _finite_float(stock_row["change"])
        change_percent = _finite_float(stock_row["change_percent"])
        if price is None or change is None or change_percent is None:
            raise HTTPException(status_code=404, detail="Stock not found.")

        # Build current data
        volume_raw = stock_row.get("volume", 0)
        try:
            volume = (
                int(float(volume_raw))
                if volume_raw is not None and math.isfinite(float(volume_raw))
                else 0
            )
        except (TypeError, ValueError):
            volume = 0
        market_cap = None
        if "market_cap" in stock_row.index:
            market_cap = _finite_float(stock_row.get("market_cap"))

        current_data = CurrentData(
            price=price,
            change=change,
            changePercent=change_percent,
            volume=volume,
            marketCap=market_cap,
        )

        # Try to load projection data
        projection_data = None
        technical_data = None

        try:
            proj_df = loader.load_projections()
            stock_proj = proj_df[proj_df["symbol"].map(normalize_ticker) == sym]

            if not stock_proj.empty:
                proj_row = stock_proj.iloc[0]

                # Soft-fail projection when required numerics are missing/non-finite
                # so Pydantic cannot serialize NaN/Inf as JSON null on the detail card.
                target_price = _finite_float(proj_row.get("target_mid"))
                expected_change = _finite_float(proj_row.get("expected_change_percent"))
                confidence_f = _finite_float(proj_row.get("confidence"))
                if (
                    target_price is not None
                    and expected_change is not None
                    and confidence_f is not None
                ):
                    target_date = projection_target_date(proj_row.to_dict(), date).isoformat()

                    projection_data = ProjectionData(
                        targetDate=target_date,
                        targetPrice=target_price,
                        expectedChange=expected_change,
                        confidence=int(confidence_f),
                        # Legacy CSVs may omit label columns; keep the card
                        # instead of KeyError→bare except dropping projection.
                        recommendation=_safe_label(proj_row.get("recommendation"), "HOLD"),
                        risk=_safe_label(proj_row.get("risk_level"), "Unknown"),
                        trend=_safe_label(proj_row.get("trend"), "Neutral"),
                    )

                    technical_data = TechnicalData(
                        momentum=(
                            _finite_float(proj_row.get("momentum_score"))
                            if "momentum_score" in proj_row.index
                            else None
                        ),
                        volatility=(
                            _finite_float(proj_row.get("volatility_score"))
                            if "volatility_score" in proj_row.index
                            else None
                        ),
                        rsi=None,  # Not available in current data
                    )

        except Exception:
            # No projection data available
            pass

        return StockDetail(
            symbol=sym,
            # Dirty CSV name cells (NaN/None) fail Pydantic str → 500 the detail.
            name=_safe_label(stock_row.get("name"), sym),
            currentData=current_data,
            projection=projection_data,
            technical=technical_data,
        )

    except HTTPException:
        raise
    except Exception:
        raise HTTPException(status_code=500, detail="Something went wrong. Please try again.")


@router.get("/{symbol}/historical", response_model=HistoricalData)
async def get_stock_historical(
    symbol: str = Path(..., description="Stock symbol"),
    days: int = Query(30, ge=1, le=365, description="Number of days of history"),
):
    """Get historical data for a specific stock"""
    try:
        sym = normalize_ticker(symbol)
        if not sym:
            raise HTTPException(status_code=404, detail="No historical data found.")

        loader = get_data_loader()
        historical_records = loader.load_historical_data(sym, days)

        if not historical_records:
            raise HTTPException(status_code=404, detail="No historical data found.")

        historical_points = []
        for record in historical_records:
            close = _finite_float(record.get("close"))
            change = _finite_float(record.get("change_percent"))
            if close is None or change is None:
                # Skip corrupt/partial days so one bad row cannot 500 the series.
                continue

            proj = record.get("projection")
            # Convert to camelCase for frontend; omit nested projection when required
            # numerics are missing/non-finite (mirrors stock detail soft-fail).
            projection = None
            if proj:
                target_price = _finite_float(proj.get("target_price"))
                expected_change = _finite_float(proj.get("expected_change"))
                confidence = _finite_float(proj.get("confidence"))
                if (
                    target_price is not None
                    and expected_change is not None
                    and confidence is not None
                ):
                    projection = {
                        "targetPrice": target_price,
                        "confidence": confidence,
                        "recommendation": proj.get("recommendation"),
                        "expectedChange": expected_change,
                    }

            volume_raw = record.get("volume", 0)
            try:
                volume = (
                    int(float(volume_raw))
                    if volume_raw is not None and math.isfinite(float(volume_raw))
                    else 0
                )
            except (TypeError, ValueError):
                volume = 0

            historical_points.append(
                HistoricalPoint(
                    date=record["date"],
                    close=close,
                    change=change,
                    volume=volume,
                    projection=projection,
                )
            )

        if not historical_points:
            raise HTTPException(status_code=404, detail="No historical data found.")

        return HistoricalData(symbol=sym, data=historical_points)

    except HTTPException:
        raise
    except Exception:
        raise HTTPException(status_code=500, detail="Something went wrong. Please try again.")

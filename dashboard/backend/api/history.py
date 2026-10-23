"""
Historical trends API endpoints
"""

import math
from datetime import date as calendar_date
from functools import lru_cache
from typing import Any, Dict, List, Optional

import pandas as pd
from fastapi import APIRouter, HTTPException, Path, Query
from pydantic import BaseModel

from dashboard.backend.services.data_loader import get_data_loader
from src.core.logger import get_logger
from src.utils.tickers import normalize_ticker

try:
    from pytickersymbols import PyTickerSymbols
except ImportError:
    PyTickerSymbols = None

logger = get_logger(__name__)
router = APIRouter()


def _safe_float(value: Any, default: float = 0.0) -> float:
    """Coerce numeric aggregates; fall back when missing, non-numeric, or non-finite."""
    try:
        if value is None:
            return default
        result = float(value)
        if not math.isfinite(result):
            return default
        return result
    except (TypeError, ValueError):
        return default


def _finite_column_mean(df: pd.DataFrame, column: str, default: float = 0.0) -> float:
    """Mean of finite numeric cells only so Inf/NaN cannot poison day averages."""
    if column not in df.columns:
        return default
    series = pd.to_numeric(df[column], errors="coerce")
    finite = series.map(lambda value: isinstance(value, (int, float)) and math.isfinite(value))
    if not bool(finite.any()):
        return default
    return _safe_float(series[finite].mean(), default)


@lru_cache(maxsize=4)
def _load_index_symbol_names(provider_class: type) -> tuple[tuple[str, str], ...]:
    """Build the static index catalog once per provider implementation."""
    result = {}
    try:
        data = provider_class()
        for index_name in ["S&P 500", "NASDAQ 100", "Dow Jones"]:
            try:
                for stock in data.get_stocks_by_index(index_name):
                    sym = normalize_ticker(stock.get("symbol"))
                    name = stock.get("name")
                    if sym and name:
                        result[sym] = name
            except Exception:
                continue
    except Exception:
        pass
    return tuple(result.items())


def load_index_symbol_names() -> dict:
    """Symbol -> company name for major US indices, cached for the process lifetime."""
    if PyTickerSymbols is None:
        return {}
    return dict(_load_index_symbol_names(PyTickerSymbols))


def _resolve_company_names(symbols: list) -> dict:
    """
    Resolve symbol->company name via pytickersymbols (S&P 500, NASDAQ 100, Dow Jones).

    Keys are normalized tickers; blank / sentinel inputs are skipped so None/NaN
    never become fake NONE/NAN name-map entries.
    """
    if not symbols:
        return {}
    catalog = load_index_symbol_names()
    out = {}
    for sym in symbols:
        key = normalize_ticker(sym)
        if not key:
            continue
        out[key] = catalog.get(key, key)
    return out


def build_symbol_catalog() -> tuple:
    """
    Full picker catalog: major US index constituents plus any symbols in latest projections.
    Returns (sorted_symbols, symbol_names).
    """
    names = load_index_symbol_names()
    symbols = set(names.keys())
    try:
        loader = get_data_loader()
        df = loader.load_projections()
        if not df.empty and "symbol" in df.columns:
            name_col = "name" if "name" in df.columns else None
            for sym in df["symbol"].unique():
                # Skip None/NaN/blank so they never appear as "NONE"/"NAN"/"".
                sym_key = normalize_ticker(sym)
                if not sym_key:
                    continue
                symbols.add(sym_key)
                if name_col:
                    stored = df.loc[df["symbol"] == sym, name_col].iloc[0]
                    if stored and str(stored).strip() and str(stored) != sym_key:
                        names[sym_key] = str(stored).strip()
                elif sym_key not in names:
                    names[sym_key] = sym_key
    except (ValueError, Exception):
        pass
    sorted_symbols = sorted(symbols)
    return sorted_symbols, {sym: names.get(sym, sym) for sym in sorted_symbols}


class DailySummaryPoint(BaseModel):
    """Summary for a single day"""

    date: str
    totalProjections: int
    averageConfidence: float
    expectedMarketMove: float
    sentiment: str
    strongBuy: int
    buy: int
    hold: int
    sell: int
    strongSell: int


class HistoricalSummaryResponse(BaseModel):
    """Aggregated historical summary"""

    dates: List[str]
    data: List[DailySummaryPoint]
    firstDate: str
    lastDate: str
    symbols: List[str] = []
    names: dict = {}


class RunProjection(BaseModel):
    """One saved projection in a historical run."""

    symbol: str
    name: str
    recommendation: str
    confidence: Optional[float] = None
    expectedChange: Optional[float] = None
    currentPrice: Optional[float] = None
    targetPrice: Optional[float] = None
    targetLow: Optional[float] = None
    targetHigh: Optional[float] = None
    risk: str
    reason: str = ""


class RunProjectionsResponse(BaseModel):
    date: str
    totalProjections: int
    projections: List[RunProjection]


def _optional_finite(value: Any) -> Optional[float]:
    """Keep missing or non-finite stored values out of JSON responses."""
    result = _safe_float(value, default=float("nan"))
    return result if math.isfinite(result) else None


def _display_text(value: Any, fallback: str = "") -> str:
    if value is None:
        return fallback
    text = str(value).strip()
    return fallback if not text or text.lower() in {"nan", "none", "<na>"} else text


class AccuracySample(BaseModel):
    """One evaluated projection vs actual close."""

    symbol: str
    runDate: str
    targetDate: str
    actualDate: str
    current: Optional[float] = None
    predicted: float
    actual: float
    absErrorPct: float
    signedErrorPct: float
    directionCorrect: Optional[bool] = None
    bandHit: Optional[bool] = None
    confidence: Optional[float] = None
    confidenceBand: str
    recommendation: str


class AccuracyAggregate(BaseModel):
    """Metrics for one report or cohort."""

    count: int
    meanAbsErrorPct: Optional[float] = None
    medianAbsErrorPct: Optional[float] = None
    directionalAccuracyPct: Optional[float] = None
    bandCoveragePct: Optional[float] = None
    meanConfidence: Optional[float] = None
    calibrationGapPct: Optional[float] = None


class AccuracySummary(BaseModel):
    """Rollups for projection accuracy."""

    schemaVersion: int
    calendar: str
    horizonSessions: int
    projectionCount: int
    validProjectionCount: int
    sampleCount: int
    invalidCount: int
    pendingCount: int
    missingActualCount: int
    evaluationCoveragePct: Optional[float] = None
    meanAbsErrorPct: Optional[float] = None
    medianAbsErrorPct: Optional[float] = None
    directionalAccuracyPct: Optional[float] = None
    bandCoveragePct: Optional[float] = None
    meanConfidence: Optional[float] = None
    calibrationGapPct: Optional[float] = None
    byRecommendation: Dict[str, AccuracyAggregate]
    byConfidenceBand: Dict[str, AccuracyAggregate]


class ProjectionAccuracyResponse(BaseModel):
    """Projection accuracy over the lookback window."""

    summary: AccuracySummary
    samples: List[AccuracySample]
    samplesTruncated: bool


@router.get("/dates")
async def get_available_dates():
    """Get list of all available dates with data"""
    try:
        loader = get_data_loader()
        dates = loader.get_available_dates()
        return {"dates": dates}
    except ValueError:
        raise HTTPException(status_code=404, detail="No data available.")
    except Exception:
        raise HTTPException(status_code=500, detail="Something went wrong. Please try again.")


@router.get("/symbols")
async def get_tracked_symbols():
    """Get list of all tracked stock symbols (from latest projections)"""
    try:
        loader = get_data_loader()
        date = loader.get_latest_date()
        if not date:
            raise HTTPException(status_code=404, detail="No data available")

        df = loader.load_projections()
        symbols, symbol_names = build_symbol_catalog()
        if not symbols:
            raw = df["symbol"].unique().tolist() if "symbol" in df.columns else []
            symbol_names = _resolve_company_names(raw)
            symbols = sorted(symbol_names.keys())

        return {
            "symbols": symbols,
            "names": symbol_names,
            "date": date,
        }
    except HTTPException:
        raise
    except ValueError:
        raise HTTPException(status_code=404, detail="No data available.")
    except Exception:
        raise HTTPException(status_code=500, detail="Something went wrong. Please try again.")


@router.get("/summary")
async def get_historical_summary(
    days: int = Query(30, ge=1, le=365, description="Number of days of history")
):
    """Get aggregated projections summary over time"""
    try:
        loader = get_data_loader()
        dates = loader.get_available_dates()
        if not dates:
            raise HTTPException(status_code=404, detail="No data available")

        dates_to_load = dates[:days]
        symbols_list = []
        symbol_names = {}  # Populated from projections CSV when name column has real names

        data_points = []
        for date in dates_to_load:
            try:
                df = loader.load_projections(date)
                if df.empty:
                    continue

                total = len(df)
                # Finite-only means: pandas .mean() treats Inf as poison (→ Inf → 0.0).
                avg_confidence = _finite_column_mean(df, "confidence")
                avg_expected = _finite_column_mean(df, "expected_change_percent")

                if avg_expected > 1.0:
                    sentiment = "Bullish"
                elif avg_expected < -1.0:
                    sentiment = "Bearish"
                else:
                    sentiment = "Neutral"

                rec_counts = (
                    df["recommendation"].value_counts().to_dict()
                    if "recommendation" in df.columns
                    else {}
                )
                strong_buy = rec_counts.get("STRONG BUY", 0)
                buy = rec_counts.get("BUY", 0)
                hold = rec_counts.get("HOLD", 0)
                sell = rec_counts.get("SELL", 0)
                strong_sell = rec_counts.get("STRONG SELL", 0)

                data_points.append(
                    DailySummaryPoint(
                        date=date,
                        totalProjections=total,
                        averageConfidence=round(avg_confidence, 1),
                        expectedMarketMove=round(avg_expected, 2),
                        sentiment=sentiment,
                        strongBuy=strong_buy,
                        buy=buy,
                        hold=hold,
                        sell=sell,
                        strongSell=strong_sell,
                    )
                )
                # Extract symbols and names from first successful load (latest data)
                if not symbols_list and "symbol" in df.columns:
                    # Normalize so None/NaN never leak as NONE/NAN list entries.
                    symbols_list = sorted(
                        {key for key in (normalize_ticker(s) for s in df["symbol"].unique()) if key}
                    )
                    if "name" in df.columns:
                        symbol_names = {}
                        for _, row in df.drop_duplicates("symbol").iterrows():
                            key = normalize_ticker(row["symbol"])
                            if not key:
                                continue
                            stored = row.get("name")
                            if stored and str(stored).strip() and str(stored).strip() != key:
                                symbol_names[key] = str(stored).strip()
            except Exception:
                continue

        first_date = dates_to_load[-1] if dates_to_load else ""
        last_date = dates_to_load[0] if dates_to_load else ""

        # Resolve company names: use stored names from CSV when real, else pytickersymbols
        resolved = _resolve_company_names(symbols_list)
        for sym in symbols_list:
            stored = symbol_names.get(sym)
            if stored and stored != sym:
                resolved[sym] = stored  # Prefer CSV/stored names over lookup
        symbol_names = resolved

        return {
            "dates": dates_to_load,
            "data": [p.model_dump() for p in data_points],
            "firstDate": first_date,
            "lastDate": last_date,
            "symbols": symbols_list,
            "names": symbol_names,
        }

    except HTTPException:
        raise
    except ValueError:
        raise HTTPException(status_code=404, detail="No data available.")
    except Exception:
        raise HTTPException(status_code=500, detail="Something went wrong. Please try again.")


@router.get("/runs/{run_date}/projections", response_model=RunProjectionsResponse)
async def get_run_projections(
    run_date: calendar_date = Path(..., description="Recorded run date"),
):
    """Expose the saved rows behind a timeline count for one date."""
    day = run_date.isoformat()
    try:
        df = get_data_loader().load_projections(day)
    except ValueError:
        raise HTTPException(status_code=404, detail="No projections found for this run.")
    except Exception:
        logger.exception("Unable to load projections for run %s", day)
        raise HTTPException(status_code=500, detail="Something went wrong. Please try again.")

    projections = []
    for _, row in df.iterrows():
        symbol = normalize_ticker(row.get("symbol"))
        if not symbol:
            continue
        projections.append(
            RunProjection(
                symbol=symbol,
                name=_display_text(row.get("name"), symbol),
                recommendation=_display_text(row.get("recommendation"), "Unrated"),
                confidence=_optional_finite(row.get("confidence")),
                expectedChange=_optional_finite(row.get("expected_change_percent")),
                currentPrice=_optional_finite(row.get("current_price")),
                targetPrice=_optional_finite(row.get("target_mid")),
                targetLow=_optional_finite(row.get("target_low")),
                targetHigh=_optional_finite(row.get("target_high")),
                risk=_display_text(row.get("risk_level"), "Unknown"),
                reason=_display_text(row.get("reason")),
            )
        )

    return RunProjectionsResponse(
        date=day,
        totalProjections=len(df),
        projections=projections,
    )


@router.get("/accuracy", response_model=ProjectionAccuracyResponse)
async def get_projection_accuracy(
    days: int = Query(90, ge=7, le=365, description="Look back this many days of projection runs"),
):
    """
    Compare each run's target_mid to the close on its exact fifth XNYS session.
    Missing target-session closes remain explicit and are not rolled forward.
    """
    try:
        loader = get_data_loader()
        raw = loader.compute_projection_accuracy(days)
        return ProjectionAccuracyResponse(
            summary=AccuracySummary(**raw["summary"]),
            samples=[AccuracySample(**s) for s in raw["samples"]],
            samplesTruncated=raw["samplesTruncated"],
        )
    except HTTPException:
        raise
    except ValueError:
        raise HTTPException(status_code=404, detail="No data available.")
    except Exception:
        raise HTTPException(status_code=500, detail="Something went wrong. Please try again.")

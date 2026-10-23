"""Deterministic projection backtesting over saved MarketHelm snapshots."""

from __future__ import annotations

import math
from collections import defaultdict
from datetime import date, datetime, timedelta
from pathlib import Path
from statistics import median
from typing import Any, Dict, Iterable, List, Mapping, Optional, Tuple

import src.storage.market_bars as market_bars
import src.storage.projections_store as projections_store

from ..utils.tickers import normalize_ticker
from .market_calendar import (
    DEFAULT_CALENDAR,
    get_market_calendar,
    trading_session_after,
    trading_session_after_timestamp,
)

_INVALID_LABELS = frozenset({"", "nan", "<na>", "none", "nat", "null"})


def _finite_positive(value: Any) -> Optional[float]:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) and number > 0 else None


def _finite_confidence(value: Any) -> Optional[float]:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) and 0 <= number <= 100 else None


def _label(value: Any, default: str = "UNKNOWN") -> str:
    try:
        text = str(value).strip()
    except Exception:
        return default
    return default if text.lower() in _INVALID_LABELS else text


def _truthy(value: Any) -> bool:
    return value is True or str(value).strip().lower() in {"1", "true", "yes"}


def _direction(value: float, origin: float) -> int:
    return 1 if value > origin else -1 if value < origin else 0


def _mean(values: Iterable[float]) -> Optional[float]:
    items = list(values)
    return sum(items) / len(items) if items else None


def _rounded(value: Optional[float], places: int = 3) -> Optional[float]:
    return round(value, places) if value is not None else None


def _confidence_band(confidence: Optional[float]) -> str:
    if confidence is None:
        return "UNKNOWN"
    lower = min(int(confidence // 10) * 10, 90)
    if lower < 50:
        return "0-49"
    return f"{lower}-{lower + 9 if lower < 90 else 100}"


def _target_session(
    row: Mapping[str, Any],
    run_date: date,
    horizon_sessions: int,
    calendar_name: str,
) -> date:
    """Derive a target from generation time, with a legacy run-date fallback."""
    generated_at = row.get("generated_at")
    if generated_at is not None:
        try:
            timestamp = datetime.fromisoformat(str(generated_at).replace("Z", "+00:00"))
            if timestamp.tzinfo is not None and timestamp.utcoffset() is not None:
                return trading_session_after_timestamp(timestamp, horizon_sessions, calendar_name)
        except (TypeError, ValueError):
            pass
    return trading_session_after(run_date, horizon_sessions, calendar_name)


def _has_timestamped_generation(row: Mapping[str, Any]) -> bool:
    try:
        timestamp = datetime.fromisoformat(str(row.get("generated_at")).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return False
    return timestamp.tzinfo is not None and timestamp.utcoffset() is not None


def _aggregate(samples: List[Dict[str, Any]]) -> Dict[str, Any]:
    errors = [sample["absErrorPct"] for sample in samples]
    directions = [sample for sample in samples if sample["directionCorrect"] is not None]
    bands = [sample for sample in samples if sample["bandHit"] is not None]
    calibrated = [sample for sample in directions if sample["confidence"] is not None]
    direction_accuracy = _mean(float(sample["directionCorrect"]) * 100 for sample in directions)
    mean_confidence = _mean(sample["confidence"] for sample in calibrated)
    calibrated_accuracy = _mean(float(sample["directionCorrect"]) * 100 for sample in calibrated)
    return {
        "count": len(samples),
        "meanAbsErrorPct": _rounded(_mean(errors)),
        "medianAbsErrorPct": _rounded(median(errors) if errors else None),
        "directionalAccuracyPct": _rounded(direction_accuracy),
        "bandCoveragePct": _rounded(_mean(float(sample["bandHit"]) * 100 for sample in bands)),
        "meanConfidence": _rounded(mean_confidence),
        "calibrationGapPct": _rounded(
            mean_confidence - calibrated_accuracy
            if mean_confidence is not None and calibrated_accuracy is not None
            else None
        ),
    }


def evaluate_projections(
    projections: Iterable[Mapping[str, Any]],
    closes: Iterable[Mapping[str, Any]],
    *,
    horizon_sessions: int = 5,
    calendar_name: str = DEFAULT_CALENDAR,
    max_samples: Optional[int] = 300,
    verified_outcomes_only: bool = False,
) -> Dict[str, Any]:
    """Evaluate projection rows against the exact target exchange session.

    Each projection must include ``run_date``, ``symbol``, and ``target_mid``.
    Actual rows must include ``date``, ``symbol``, and ``close``. Missing closes
    on the target session are reported as missing rather than rolled forward,
    keeping every sample on the same horizon and preventing accidental look-ahead.
    """
    if horizon_sessions < 1:
        raise ValueError("horizon_sessions must be at least 1")
    get_market_calendar(calendar_name)

    projection_rows = list(projections)
    close_map: Dict[Tuple[str, str], Tuple[float, str]] = {}
    observed_dates = set()
    for row in closes:
        symbol = normalize_ticker(row.get("symbol"))
        # market_bars rows always include outcome_* keys (often NULL); only treat
        # non-empty values as verified-outcome provenance.
        has_provenance = any(
            row.get(key) not in (None, "")
            for key in ("outcome_final", "outcome_session", "outcome_close")
        )
        if verified_outcomes_only and not has_provenance:
            continue
        close = _finite_positive(row.get("outcome_close") if has_provenance else row.get("close"))
        if has_provenance and not (
            _truthy(row.get("outcome_final")) and row.get("outcome_session")
        ):
            continue
        actual_date_value = row.get("outcome_session") if has_provenance else row.get("date")
        try:
            actual_date = datetime.strptime(str(actual_date_value)[:10], "%Y-%m-%d").date()
        except (TypeError, ValueError):
            continue
        if symbol and close is not None:
            provenance = "verified_previous_close" if has_provenance else "legacy_filename"
            close_map[(actual_date.isoformat(), symbol)] = (close, provenance)
            observed_dates.add(actual_date)

    latest_observed = max(observed_dates) if observed_dates else None
    samples: List[Dict[str, Any]] = []
    invalid_count = 0
    pending_count = 0
    missing_actual_count = 0

    for row in projection_rows:
        symbol = normalize_ticker(row.get("symbol"))
        predicted = _finite_positive(row.get("target_mid"))
        try:
            run_date = datetime.strptime(str(row.get("run_date"))[:10], "%Y-%m-%d").date()
            target_date = _target_session(row, run_date, horizon_sessions, calendar_name)
        except (TypeError, ValueError):
            invalid_count += 1
            continue
        if not symbol or predicted is None:
            invalid_count += 1
            continue
        if latest_observed is None or target_date > latest_observed:
            pending_count += 1
            continue

        actual_entry = close_map.get((target_date.isoformat(), symbol))
        if actual_entry is None:
            missing_actual_count += 1
            continue
        actual, actual_provenance = actual_entry

        current = _finite_positive(row.get("current_price"))
        target_low = _finite_positive(row.get("target_low"))
        target_high = _finite_positive(row.get("target_high"))
        confidence = _finite_confidence(row.get("confidence"))
        direction_correct = None
        if current is not None:
            direction_correct = _direction(predicted, current) == _direction(actual, current)
        band_hit = None
        if target_low is not None and target_high is not None and target_low <= target_high:
            band_hit = target_low <= actual <= target_high

        samples.append(
            {
                "symbol": symbol,
                "runDate": run_date.isoformat(),
                "targetDate": target_date.isoformat(),
                "actualDate": target_date.isoformat(),
                "actualProvenance": actual_provenance,
                "generationProvenance": (
                    "timestamped" if _has_timestamped_generation(row) else "legacy_run_date"
                ),
                "current": _rounded(current, 4),
                "predicted": round(predicted, 4),
                "actual": round(actual, 4),
                "absErrorPct": round(abs(predicted - actual) / actual * 100, 3),
                "signedErrorPct": round((predicted - actual) / actual * 100, 3),
                "directionCorrect": direction_correct,
                "bandHit": band_hit,
                "confidence": _rounded(confidence, 2),
                "confidenceBand": _confidence_band(confidence),
                "recommendation": _label(row.get("recommendation")),
            }
        )

    by_recommendation: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    by_confidence: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for sample in samples:
        by_recommendation[sample["recommendation"]].append(sample)
        by_confidence[sample["confidenceBand"]].append(sample)

    mature_count = len(samples) + missing_actual_count
    verified_outcome_count = sum(
        sample["actualProvenance"] == "verified_previous_close" for sample in samples
    )
    timestamped_projection_count = sum(
        sample["generationProvenance"] == "timestamped" for sample in samples
    )
    summary = {
        "schemaVersion": 1,
        "calendar": calendar_name,
        "horizonSessions": horizon_sessions,
        "projectionCount": len(projection_rows),
        "validProjectionCount": len(projection_rows) - invalid_count,
        "sampleCount": len(samples),
        "verifiedOutcomeCount": verified_outcome_count,
        "legacyOutcomeCount": len(samples) - verified_outcome_count,
        "timestampedProjectionCount": timestamped_projection_count,
        "legacyProjectionCount": len(samples) - timestamped_projection_count,
        "invalidCount": invalid_count,
        "pendingCount": pending_count,
        "missingActualCount": missing_actual_count,
        "evaluationCoveragePct": _rounded(
            len(samples) / mature_count * 100 if mature_count else None
        ),
        **{key: value for key, value in _aggregate(samples).items() if key != "count"},
        "byRecommendation": {
            key: _aggregate(value) for key, value in sorted(by_recommendation.items())
        },
        "byConfidenceBand": {
            key: _aggregate(value) for key, value in sorted(by_confidence.items())
        },
    }
    samples.sort(key=lambda sample: sample["symbol"])
    samples.sort(key=lambda sample: (sample["runDate"], sample["targetDate"]), reverse=True)
    visible_samples = samples if max_samples is None else samples[:max_samples]
    return {
        "summary": summary,
        "samples": visible_samples,
        "samplesTruncated": len(visible_samples) < len(samples),
    }


def backtest_data_dir(
    data_dir: Path,
    *,
    days: int = 365,
    horizon_sessions: int = 5,
    calendar_name: str = DEFAULT_CALENDAR,
    max_samples: Optional[int] = 300,
    verified_outcomes_only: bool = False,
) -> Dict[str, Any]:
    """Load market_bars closes and durable projections from ``data_dir``."""
    root = Path(data_dir).resolve()
    if not root.is_dir():
        raise ValueError(f"Data directory not found: {root}")
    if days < 1:
        raise ValueError("days must be at least 1")

    try:
        bar_dates = market_bars.list_market_bar_dates(data_dir=root, limit=3650)
    except Exception:
        bar_dates = []
    try:
        projection_dates = projections_store.list_projection_dates(data_dir=root, limit=3650)
    except Exception:
        projection_dates = []

    dated_inputs = [(day, None) for day in bar_dates] or [(day, None) for day in projection_dates]
    if not dated_inputs:
        return evaluate_projections(
            [],
            [],
            horizon_sessions=horizon_sessions,
            calendar_name=calendar_name,
            max_samples=max_samples,
            verified_outcomes_only=verified_outcomes_only,
        )

    latest = max(datetime.strptime(day, "%Y-%m-%d").date() for day, _ in dated_inputs)
    cutoff = latest - timedelta(days=days)
    projections: List[Dict[str, Any]] = []
    closes: List[Dict[str, Any]] = []

    for day in bar_dates:
        for row in market_bars.load_market_bars(day, data_dir=root):
            closes.append({**row, "date": day})

    for day in projection_dates:
        if datetime.strptime(day, "%Y-%m-%d").date() < cutoff:
            continue
        for row in projections_store.load_projections(day, data_dir=root):
            projections.append({**row, "run_date": day})

    return evaluate_projections(
        projections,
        closes,
        horizon_sessions=horizon_sessions,
        calendar_name=calendar_name,
        max_samples=max_samples,
        verified_outcomes_only=verified_outcomes_only,
    )

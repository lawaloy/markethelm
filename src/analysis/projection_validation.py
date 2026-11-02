"""Frozen temporal benchmarks and research-only projection calibration candidates."""

from __future__ import annotations

import math
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from statistics import mean, median
from typing import Any

import src.storage.market_bars as market_bars
import src.storage.projections_store as projections_store
from src.analysis.backtesting import backtest_data_dir
from src.analysis.market_calendar import get_market_calendar
from src.storage.database import database_enabled


@dataclass(frozen=True)
class ValidationSettings:
    """Predeclared split and sample floors; never optimized against the holdout."""

    holdout_run_dates: int = 5
    min_training_run_dates: int = 5
    min_training_samples: int = 200
    min_cohort_samples: int = 30
    interval_coverage: float = 0.8

    def __post_init__(self):
        for name in (
            "holdout_run_dates",
            "min_training_run_dates",
            "min_training_samples",
            "min_cohort_samples",
        ):
            if type(getattr(self, name)) is not int or getattr(self, name) < 1:
                raise ValueError(f"{name} must be a positive integer")
        if not 0 < self.interval_coverage < 1:
            raise ValueError("interval_coverage must be between zero and one")


def _timestamp(value: Any) -> datetime:
    stamp = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if stamp.utcoffset() is None:
        raise ValueError("timestamp must include a timezone")
    return stamp


def _direction(prediction: float, current: float) -> int:
    return (prediction > current) - (prediction < current)


def _correct(sample: dict) -> bool:
    return _direction(sample["predicted"], sample["current"]) == _direction(
        sample["actual"], sample["current"]
    )


def _metrics(samples: list[dict], *, unchanged: bool = False, fit: dict | None = None) -> dict:
    errors, directions, bands, probabilities, widths = [], [], [], [], []
    cohorts = defaultdict(list)
    for sample in samples:
        predicted = sample["current"] if unchanged else sample["predicted"]
        actual = sample["actual"]
        errors.append(abs(predicted - actual) / actual * 100)
        correct = _direction(predicted, sample["current"]) == _direction(actual, sample["current"])
        directions.append(correct)
        if unchanged:
            continue
        if fit is None:
            hit = sample.get("bandHit")
            probability = sample.get("confidence")
            probability = None if probability is None else probability / 100
            low, high = sample.get("targetLow"), sample.get("targetHigh")
            if low is not None and high is not None and 0 < low <= high:
                widths.append((high - low) / sample["current"] * 100)
        else:
            width = sample["current"] * fit["residualQuantile"]
            low, high = max(0, predicted - width), predicted + width
            hit = low <= actual <= high
            widths.append((high - low) / sample["current"] * 100)
            probability = fit["confidenceByCohort"].get(
                sample["confidenceBand"], fit["fallbackConfidence"]
            )
        if hit is not None:
            bands.append(hit)
        if probability is not None:
            probabilities.append((probability, float(correct)))
            cohorts[sample["confidenceBand"]].append((probability, float(correct)))
    confidence = mean(p for p, _ in probabilities) * 100 if probabilities else None
    calibrated_accuracy = mean(y for _, y in probabilities) * 100 if probabilities else None
    return {
        "sampleCount": len(samples),
        "meanAbsErrorPct": mean(errors) if errors else None,
        "medianAbsErrorPct": median(errors) if errors else None,
        "directionalAccuracyPct": mean(directions) * 100 if directions else None,
        "bandCoveragePct": mean(bands) * 100 if bands else None,
        "bandSampleCount": len(bands),
        "meanBandWidthPct": mean(widths) if widths else None,
        "confidenceSampleCount": len(probabilities),
        "meanConfidence": confidence,
        "calibrationGapPct": (confidence - calibrated_accuracy if probabilities else None),
        "brierScore": mean((p - y) ** 2 for p, y in probabilities) if probabilities else None,
        "cohortCalibrationErrorPct": (
            sum(abs(mean(p - y for p, y in group)) * len(group) for group in cohorts.values())
            / len(probabilities)
            * 100
            if probabilities
            else None
        ),
    }


def _comparison(samples: list[dict]) -> dict:
    by_date = defaultdict(list)
    for sample in samples:
        by_date[sample["runDate"]].append(sample)
    daily_deltas = []
    for group in by_date.values():
        # Positive means the incumbent is better than an unchanged-price forecast.
        daily_deltas.append(
            _metrics(group, unchanged=True)["meanAbsErrorPct"] - _metrics(group)["meanAbsErrorPct"]
        )
    return {
        "incumbent": _metrics(samples),
        "unchangedPrice": _metrics(samples, unchanged=True),
        "neutralConfidenceBrierScore": 0.25 if samples else None,
        "alwaysUpDirectionalAccuracyPct": (
            mean(s["actual"] > s["current"] for s in samples) * 100 if samples else None
        ),
        "alwaysDownDirectionalAccuracyPct": (
            mean(s["actual"] < s["current"] for s in samples) * 100 if samples else None
        ),
        "equalRunDateMeanErrorImprovementPct": mean(daily_deltas) if daily_deltas else None,
        "runDateWins": sum(delta > 1e-12 for delta in daily_deltas),
        "runDateTies": sum(abs(delta) <= 1e-12 for delta in daily_deltas),
        "runDateLosses": sum(delta < -1e-12 for delta in daily_deltas),
    }


def validate_samples(samples: list[dict], settings: ValidationSettings | None = None) -> dict:
    """Fit once on available earlier outcomes, then score complete later run dates.

    The newest five scored run dates are a fixed holdout. Every training outcome
    must have been persisted strictly before the earliest holdout generation time.
    Calibration is diagnostic: no application scoring or deployment is changed.
    """
    settings = settings or ValidationSettings()
    eligible = []
    for sample in samples:
        try:
            if sample.get("actualProvenance") != "verified_previous_close":
                continue
            if sample.get("generationProvenance") != "timestamped":
                continue
            generated = _timestamp(sample["generatedAt"])
            available = _timestamp(sample["outcomeAvailableAt"])
            target_close = get_market_calendar().session_close(sample["targetDate"])
            if generated >= target_close or available <= generated or available < target_close:
                continue
            if any(
                not math.isfinite(float(sample[key])) or float(sample[key]) <= 0
                for key in ("current", "predicted", "actual")
            ):
                continue
            eligible.append(sample)
        except (KeyError, TypeError, ValueError, OverflowError):
            continue
    eligible.sort(key=lambda s: (s["runDate"], s["symbol"]))
    dates = sorted({s["runDate"] for s in eligible})
    report = {
        "schemaVersion": 1,
        "protocol": "frozen-temporal-v1",
        "promotionDecision": "research_only",
        "settings": settings.__dict__,
        "sourceSampleCount": len(samples),
        "eligibleSampleCount": len(eligible),
        "excludedSampleCount": len(samples) - len(eligible),
        "latestOutcomeAvailableAt": max(
            (s["outcomeAvailableAt"] for s in eligible), default=None, key=_timestamp
        ),
        "allScoredBenchmarks": _comparison(eligible),
        "status": "insufficient_data",
        "failures": [],
        "heldOut": None,
        "limitations": [
            "Run dates and symbols have correlated, overlapping five-session outcomes.",
            "This rolling retrospective holdout has already been observed; fresh forward "
            "validation is required before promotion or selecting another candidate.",
            "The nominal interval coverage is a diagnostic target, not a guarantee under drift.",
            "Unchanged-price direction is flat; always-up/down are directional references only.",
        ],
    }
    if len(dates) <= settings.holdout_run_dates:
        report["failures"].append("Not enough run dates to reserve the fixed holdout")
        return report
    holdout_start = len(dates) - settings.holdout_run_dates
    test_dates = dates[holdout_start:]
    testing = [s for s in eligible if s["runDate"] in test_dates]
    cutoff = min(_timestamp(s["generatedAt"]) for s in testing)
    prior = [s for s in eligible if s["runDate"] < test_dates[0]]
    training = [s for s in prior if _timestamp(s["outcomeAvailableAt"]) < cutoff]
    training_dates = sorted({s["runDate"] for s in training})
    report["split"] = {
        "trainingRunDates": training_dates,
        "trainingSampleCount": len(training),
        "purgedUnavailableOutcomeCount": len(prior) - len(training),
        "latestTrainingOutcomeAvailableAt": max(
            (s["outcomeAvailableAt"] for s in training), default=None, key=_timestamp
        ),
        "holdoutRunDates": test_dates,
        "earliestHoldoutGeneratedAt": cutoff.isoformat(),
        "holdoutSampleCount": len(testing),
    }
    if len(training) < settings.min_training_samples:
        report["failures"].append("Not enough training samples available before the holdout")
    if len(training_dates) < settings.min_training_run_dates:
        report["failures"].append("Not enough training run dates available before the holdout")
    rank = math.ceil((len(training) + 1) * settings.interval_coverage)
    if not training or rank > len(training):
        report["failures"].append("Not enough residuals for the nominal interval coverage")
    if report["failures"]:
        return report
    cohorts = defaultdict(list)
    for sample in training:
        cohorts[sample["confidenceBand"]].append(sample)
    residuals = sorted(abs(s["actual"] - s["predicted"]) / s["current"] for s in training)
    fit = {
        "confidenceMethod": "training-cohort Laplace-smoothed directional frequency",
        "fallbackConfidence": (sum(_correct(s) for s in training) + 1) / (len(training) + 2),
        "confidenceByCohort": {
            band: (sum(_correct(s) for s in group) + 1) / (len(group) + 2)
            for band, group in sorted(cohorts.items())
            if len(group) >= settings.min_cohort_samples
        },
        "trainingCohortCounts": {band: len(group) for band, group in sorted(cohorts.items())},
        "intervalMethod": "symmetric absolute residual / origin price, finite-sample quantile",
        "residualQuantile": residuals[rank - 1],
        "residualQuantileRank": rank,
        "nominalCoveragePct": settings.interval_coverage * 100,
    }
    report.update(
        status="evaluated",
        fittedOnEarlierData=fit,
        heldOut={
            **_comparison(testing),
            "calibrationCandidate": _metrics(testing, fit=fit),
            "trainingRateConfidence": _metrics(testing, fit={**fit, "confidenceByCohort": {}}),
            "fallbackConfidenceSampleCount": sum(
                s["confidenceBand"] not in fit["confidenceByCohort"] for s in testing
            ),
        },
    )
    return report


def validation_data_dir(data_dir: Path, days: int = 365) -> dict:
    """Use the shared evaluator, enriching samples with persisted availability."""
    if database_enabled():
        raise ValueError("Unset MARKETHELM_DATABASE_URL to evaluate the --data-dir SQLite archive")
    report = backtest_data_dir(data_dir, days=days, max_samples=None, verified_outcomes_only=True)
    projections = {}
    for day in projections_store.list_projection_dates(data_dir=data_dir, limit=3650):
        for row in projections_store.load_projections(day, data_dir=data_dir):
            projections[(day, row["symbol"])] = row
    outcomes = defaultdict(list)
    for day in market_bars.list_market_bar_dates(data_dir=data_dir, limit=3650):
        for row in market_bars.load_market_bars(day, data_dir=data_dir):
            if row.get("outcome_final") and row.get("outcome_session"):
                outcomes[(row["symbol"], row["outcome_session"])].append(row)
    samples = []
    for sample in report["samples"]:
        projection = projections[(sample["runDate"], sample["symbol"])]
        available = []
        for row in outcomes[(sample["symbol"], sample["targetDate"])]:
            try:
                if round(float(row["outcome_close"]), 4) == sample["actual"]:
                    available.append(_timestamp(row["written_at"]))
            except (TypeError, ValueError, OverflowError):
                continue
        samples.append(
            {
                **sample,
                "generatedAt": projection.get("generated_at"),
                "targetLow": projection.get("target_low"),
                "targetHigh": projection.get("target_high"),
                "outcomeAvailableAt": min(available).isoformat() if available else None,
            }
        )
    validation = validate_samples(samples)
    validation["sourceSummary"] = report["summary"]
    return validation

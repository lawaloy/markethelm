"""Fit the predeclared candidate once, with a purged chronological diagnostic."""

from __future__ import annotations

import inspect
import math
from collections import defaultdict
from datetime import datetime
from statistics import mean

from src.analysis.forecast_models import canonical_json, fingerprint
from src.analysis.projection_validation import (
    eligible_validation_samples,
    validate_samples,
)
from src.analysis.projector import StockProjector
from src.analysis.return_candidate import MODEL_VERSION, RECIPE, ReturnCandidate


def _metrics(rows: list[dict], candidate: ReturnCandidate | None, unchanged=False) -> dict:
    daily = defaultdict(list)
    correct = []
    for row in rows:
        origin, actual = row["current"], row["actual"]
        predicted = origin if unchanged else row["predicted"]
        if candidate is not None:
            predicted = candidate.target(origin, row["predicted"])
        daily[row["runDate"]].append(abs(predicted - actual) / actual * 100)
        correct.append(
            int(predicted > origin) - int(predicted < origin)
            == int(actual > origin) - int(actual < origin)
        )
    errors = {day: mean(values) for day, values in sorted(daily.items())}
    return {
        "sampleCount": len(rows),
        "originSessionCount": len(daily),
        "meanAbsErrorPct": mean(x for group in daily.values() for x in group),
        "equalSessionErrorPct": mean(errors.values()),
        "directionalAccuracyPct": mean(correct) * 100,
        "byOriginSession": errors,
    }


def train_candidate(samples: list[dict], *, frozen_at: datetime, source: dict) -> tuple[dict, dict]:
    """No holdout labels enter coefficients, sample weights or parameter choices."""
    validation = validate_samples(samples)
    if validation["status"] != "evaluated":
        raise ValueError("Insufficient purged temporal training evidence")
    split = validation["split"]
    cutoff = datetime.fromisoformat(split["earliestHoldoutGeneratedAt"])
    if frozen_at.utcoffset() is None or frozen_at <= cutoff:
        raise ValueError("Freeze timestamp must be aware and after the training cutoff")
    eligible = eligible_validation_samples(samples)
    train = [
        row
        for row in eligible
        if row["runDate"] in split["trainingRunDates"]
        and datetime.fromisoformat(row["outcomeAvailableAt"]) < cutoff
    ]
    test = [row for row in eligible if row["runDate"] in split["holdoutRunDates"]]
    groups = defaultdict(list)
    for row in train:
        groups[row["runDate"]].append(row)
    # Minimize equally weighted daily mean squared return error plus lambda * ||beta||².
    a00 = a11 = RECIPE["ridge_penalty"]
    a01 = b0 = b1 = 0.0
    for rows in groups.values():
        weight = 1 / (len(groups) * len(rows))
        for row in rows:
            x = ReturnCandidate.feature(row["current"], row["predicted"])
            cap = RECIPE["training_return_cap"]
            y = max(-cap, min(cap, row["actual"] / row["current"] - 1))
            a00 += weight
            a01 += weight * x
            a11 += weight * x * x
            b0 += weight * y
            b1 += weight * x * y
    determinant = a00 * a11 - a01 * a01
    coefficients = [(b0 * a11 - b1 * a01) / determinant, (a00 * b1 - a01 * b0) / determinant]
    if not all(math.isfinite(x) for x in coefficients):
        raise ValueError("Non-finite candidate fit")
    artifact = {
        "schema_version": 1,
        "model_version": MODEL_VERSION,
        "frozen_at": frozen_at.isoformat(),
        "recipe": dict(RECIPE),
        "coefficients": coefficients,
        "incumbent_version": StockProjector.MODEL_VERSION,
        "calendar": "XNYS",
        "horizon_sessions": 5,
        "training": {
            "validation_protocol": validation["protocol"],
            "validation_settings": validation["settings"],
            "available_before": cutoff.isoformat(),
            "sample_count": len(train),
            "origin_sessions": sorted(groups),
            "latest_outcome_available_at": split["latestTrainingOutcomeAvailableAt"],
            "samples_sha256": fingerprint(canonical_json(train)),
            "source": source,
            "trainer_sha256": fingerprint(inspect.getsource(train_candidate).replace("\r\n", "\n")),
        },
        "deployment": "shadow_only",
    }
    candidate = ReturnCandidate(canonical_json(artifact))
    metrics = {
        "candidate": _metrics(test, candidate),
        "incumbent": _metrics(test, None),
        "unchangedPrice": _metrics(test, None, unchanged=True),
    }
    comparisons = {}
    for reference in ("incumbent", "unchangedPrice"):
        ours, base = metrics["candidate"], metrics[reference]
        error = base["equalSessionErrorPct"]
        comparisons[reference] = {
            "relativeErrorImprovementPct": (
                (error - ours["equalSessionErrorPct"]) / error * 100 if error else None
            ),
            "sessionWinFraction": mean(
                ours["byOriginSession"][day] < value
                for day, value in base["byOriginSession"].items()
            ),
        }
    return artifact, {
        "schemaVersion": 1,
        "status": "retrospective_diagnostic",
        "promotion": "shadow_only",
        "modelVersion": MODEL_VERSION,
        "recipe": dict(RECIPE),
        "split": split,
        "artifactSha256": fingerprint(canonical_json(artifact)),
        "heldOut": metrics,
        "directionReferences": {
            "alwaysUpAccuracyPct": validation["heldOut"]["alwaysUpDirectionalAccuracyPct"],
            "alwaysDownAccuracyPct": validation["heldOut"]["alwaysDownDirectionalAccuracyPct"],
        },
        "comparisons": comparisons,
        "limitations": [
            "These archived outcomes were previously observed; this is not fresh evidence.",
            "Overlapping sessions and symbols are correlated.",
            "Point-only candidate; confidence and intervals are not estimated.",
            "No holdout-driven parameter search and no automatic promotion.",
        ],
    }

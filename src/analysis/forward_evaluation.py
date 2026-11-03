"""Prospective comparisons from immutable forecasts, never mutable daily snapshots."""

from __future__ import annotations

import json
from collections import Counter, defaultdict
from datetime import datetime, timedelta
from statistics import mean
from typing import Any

from src.analysis.forecast_models import PROTOCOL_VERSION, canonical_json, fingerprint
from src.analysis.market_calendar import get_market_calendar
from src.storage.forecast_ledger import ledger_snapshot, stamp, timestamp, utc_now


def _metrics(rows: list[dict]) -> dict:
    scored = [row for row in rows if row["score"] is not None]
    mature = [row for row in rows if row["mature"]]
    errors = [
        row["score"]["abs_error_pct"] for row in scored if row["score"]["abs_error_pct"] is not None
    ]
    bands = [row for row in scored if row["score"]["band_hit"] is not None]
    confident = [row for row in scored if row["score"]["brier_score"] is not None]
    widths = [
        (r["prediction"]["target_high"] - r["prediction"]["target_low"])
        / r["case"]["origin_price"]
        * 100
        for r in bands
    ]
    confidence_bands = Counter(
        str(min(int(row["prediction"]["confidence"] // 10) * 10, 90)) for row in confident
    )
    return {
        "forecastCount": len(rows),
        "scoredCount": len(scored),
        "matureCount": len(mature),
        "pendingCount": len(rows) - len(mature),
        "missingOutcomeCount": len(mature) - len(scored),
        "outcomeCoveragePct": len(scored) / len(mature) * 100 if mature else None,
        "distinctOriginSessions": len({r["case"]["origin_session"] for r in scored}),
        "distinctSymbols": len({r["case"]["symbol"] for r in scored}),
        "meanAbsErrorPct": mean(errors) if errors else None,
        "directionalAccuracyPct": (
            mean(r["score"]["direction_correct"] for r in scored) * 100 if scored else None
        ),
        "bandCoveragePct": mean(r["score"]["band_hit"] for r in bands) * 100 if bands else None,
        "bandSampleCount": len(bands),
        "meanBandWidthPct": mean(widths) if widths else None,
        "brierScore": mean(r["score"]["brier_score"] for r in confident) if confident else None,
        "confidenceSampleCount": len(confident),
        "confidenceCoveragePct": len(confident) / len(scored) * 100 if scored else None,
        "meanConfidence": (
            mean(r["prediction"]["confidence"] for r in confident) if confident else None
        ),
        "confidenceCohortCounts": dict(sorted(confidence_bands.items())),
    }


def _paired(rows: list[dict], reference_rows: list[dict]) -> dict:
    references = {row["case"]["id"]: row for row in reference_rows}
    mature_pairs = [row for row in rows if row["mature"] and row["case"]["id"] in references]
    pairs = [
        (row, references[row["case"]["id"]])
        for row in mature_pairs
        if row["score"] is not None and references[row["case"]["id"]]["score"] is not None
    ]
    by_session = defaultdict(list)
    for pair in pairs:
        if (
            pair[0]["score"]["abs_error_pct"] is not None
            and pair[1]["score"]["abs_error_pct"] is not None
        ):
            by_session[pair[0]["case"]["origin_session"]].append(pair)
    model_daily, reference_daily, wins = [], [], 0
    for group in by_session.values():
        model_error = mean(left["score"]["abs_error_pct"] for left, _ in group)
        ref_error = mean(right["score"]["abs_error_pct"] for _, right in group)
        model_daily.append(model_error)
        reference_daily.append(ref_error)
        wins += model_error < ref_error - 1e-12
    improvement = None
    if reference_daily:
        base, current = mean(reference_daily), mean(model_daily)
        improvement = (base - current) / base * 100 if base else (0 if current == 0 else None)
    return {
        "maturePairCount": len(mature_pairs),
        "scoredPairCount": len(pairs),
        "coveragePct": len(pairs) / len(mature_pairs) * 100 if mature_pairs else None,
        "originSessionCount": len(by_session),
        "distinctSymbols": len({row["case"]["symbol"] for row, _ in pairs}),
        "equalSessionModelErrorPct": mean(model_daily) if model_daily else None,
        "equalSessionReferenceErrorPct": mean(reference_daily) if reference_daily else None,
        "relativeErrorImprovementPct": improvement,
        "sessionWinFraction": wins / len(by_session) if by_session else None,
        "modelBrierScore": _metrics([left for left, _ in pairs])["brierScore"],
        "referenceBrierScore": _metrics([right for _, right in pairs])["brierScore"],
    }


def _readiness(version: str, metrics: dict, comparisons: dict, definition: dict) -> dict:
    failures = []
    if version in {definition["price_reference"], "always-up-v1", "always-down-v1"}:
        return {"passed": False, "failures": ["benchmark_reference"], "decision": "reference_only"}
    for name, minimum in [
        ("scoredCount", definition["minimum_scored_cases"]),
        ("distinctOriginSessions", definition["minimum_origin_sessions"]),
        ("distinctSymbols", definition["minimum_symbols"]),
        ("outcomeCoveragePct", definition["minimum_paired_coverage_pct"]),
    ]:
        if metrics[name] is None or metrics[name] < minimum:
            failures.append(f"{name} requires at least {minimum}")
    required = [definition["price_reference"]]
    if version != definition["incumbent_version"]:
        required.append(definition["incumbent_version"])
    for reference in required:
        comparison = comparisons.get(reference)
        if comparison is None:
            failures.append(f"No paired point comparison against {reference}")
            continue
        for field, threshold in [
            ("scoredPairCount", definition["minimum_scored_cases"]),
            ("originSessionCount", definition["minimum_origin_sessions"]),
            ("distinctSymbols", definition["minimum_symbols"]),
            ("coveragePct", definition["minimum_paired_coverage_pct"]),
            ("relativeErrorImprovementPct", definition["minimum_relative_error_improvement_pct"]),
            ("sessionWinFraction", definition["minimum_session_win_fraction"]),
        ]:
            if comparison[field] is None or comparison[field] < threshold:
                failures.append(f"{reference}: {field} requires at least {threshold}")
    if metrics["confidenceSampleCount"]:
        if metrics["confidenceCoveragePct"] < definition["minimum_confidence_coverage_pct"]:
            failures.append("Insufficient confidence coverage")
        if metrics["brierScore"] >= definition["confidence_reference_brier"]:
            failures.append("Confidence does not beat neutral 50%")
        cohort_count = sum(
            count >= definition["minimum_cohort_samples"]
            for count in metrics["confidenceCohortCounts"].values()
        )
        if cohort_count < definition["minimum_confidence_cohorts"]:
            failures.append("Insufficient confidence cohort breadth")
        incumbent = comparisons.get(definition["incumbent_version"])
        if incumbent and incumbent["referenceBrierScore"] is not None:
            if (
                incumbent["modelBrierScore"] is None
                or incumbent["modelBrierScore"] >= incumbent["referenceBrierScore"]
            ):
                failures.append("Confidence does not beat the paired incumbent")
    if metrics["bandSampleCount"]:
        if metrics["bandSampleCount"] != metrics["scoredCount"]:
            failures.append("Incomplete range coverage")
        if metrics["bandCoveragePct"] < definition["minimum_range_coverage_pct"]:
            failures.append("Range coverage is below the frozen minimum")
    return {
        "passed": not failures,
        "failures": failures,
        "decision": "manual_review_required" if not failures else "not_ready",
    }


def forward_report(
    *,
    data_dir=None,
    days: int = 365,
    now: datetime | None = None,
    protocol_version: str = PROTOCOL_VERSION,
) -> dict[str, Any]:
    """Evaluate frozen cases as of now, weighting each origin session once."""
    if not 1 <= days <= 3650:
        raise ValueError("days must be between 1 and 3650")
    now = timestamp(now or utc_now())
    cutoff = now - timedelta(days=days)
    snapshot = ledger_snapshot(data_dir=data_dir)
    protocols = {row["version"]: row for row in snapshot["forecast_protocols"]}
    protocol = protocols.get(protocol_version)
    if protocol is None:
        return {
            "schemaVersion": 1,
            "asOf": stamp(now),
            "protocolVersion": protocol_version,
            "status": "not_started",
            "promotion": "manual_only",
            "models": {},
            "message": "No prospective forecasts have been recorded; "
            "historical snapshots are not backfilled.",
        }
    definition = json.loads(protocol["definition_json"])
    if fingerprint(canonical_json(definition)) != protocol["definition_sha256"]:
        raise ValueError("Frozen protocol failed its integrity check")
    history = [
        row
        for row in snapshot["forecast_cases"]
        if row["protocol_version"] == protocol_version and timestamp(row["recorded_at"]) <= now
    ]
    all_cases = [row for row in history if timestamp(row["recorded_at"]) >= cutoff]
    eligible = [row for row in all_cases if row["input_verified"]]
    first = {}
    for row in sorted(history, key=lambda r: (r["recorded_at"], r["id"])):
        if row["input_verified"]:
            first.setdefault((row["symbol"], row["origin_session"]), row)
    selected = {row["id"]: row for row in first.values() if timestamp(row["recorded_at"]) >= cutoff}
    scores = {
        row["prediction_id"]: row
        for row in snapshot["forecast_scores"]
        if timestamp(row["scored_at"]) <= now
        and row["rule_version"] == definition["scoring_rule_version"]
    }
    by_model = defaultdict(list)
    for prediction in snapshot["forecast_predictions"]:
        case = selected.get(prediction["case_id"])
        if case is None:
            continue
        if fingerprint(case["input_json"]) != case["input_sha256"]:
            raise ValueError("Frozen input snapshot failed its integrity check")
        if fingerprint(prediction["payload_json"]) != prediction["payload_sha256"]:
            raise ValueError("Frozen prediction failed its integrity check")
        by_model[prediction["model_version"]].append(
            {
                "case": case,
                "prediction": prediction,
                "score": scores.get(prediction["id"]),
                "mature": get_market_calendar(case["calendar"]).session_close(
                    case["target_session"]
                )
                <= now,
            }
        )
    models = {}
    for version, rows in sorted(by_model.items()):
        metrics = _metrics(rows)
        comparisons = {
            reference: _paired(rows, by_model[reference])
            for reference in (definition["price_reference"], definition["incumbent_version"])
            if reference != version
            and reference in by_model
            and rows[0]["prediction"]["kind"] == "point"
        }
        by_date = defaultdict(list)
        for row in rows:
            by_date[row["case"]["origin_session"]].append(row)
        models[version] = {
            "metrics": metrics,
            "comparisons": comparisons,
            "byOriginSession": {day: _metrics(group) for day, group in sorted(by_date.items())},
            "readiness": _readiness(version, metrics, comparisons, definition),
        }
    return {
        "schemaVersion": 1,
        "asOf": stamp(now),
        "protocolVersion": protocol_version,
        "status": (
            "evaluating"
            if any(m["metrics"]["scoredCount"] for m in models.values())
            else "collecting"
        ),
        "promotion": "manual_only",
        "protocol": definition,
        "protocolRegisteredAt": protocol["registered_at"],
        "protocolSha256": protocol["definition_sha256"],
        "recordedCaseCount": len(all_cases),
        "selectedCaseCount": len(selected),
        "excludedUnverifiedInputCases": len(all_cases) - len(eligible),
        "excludedRepeatedSessionCases": len(eligible) - len(selected),
        "modelDefinitions": {
            row["version"]: json.loads(row["definition_json"])
            for row in snapshot["forecast_models"]
            if row["version"] in models
        },
        "models": models,
        "limitations": [
            "Overlapping five-session outcomes and stocks are correlated; "
            "sample count is not independent trials.",
            "Eligibility thresholds are research hygiene, not proof of a trading edge.",
            "Passing criteria requires manual review of stability, data quality and range width; "
            "no model is auto-promoted.",
        ],
    }

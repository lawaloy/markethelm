"""Temporal calibration must never learn from held-out or unavailable outcomes."""

from copy import deepcopy
from datetime import timedelta

import pytest

import src.storage.market_bars as market_bars
from src.analysis.market_calendar import get_market_calendar
from src.analysis.projection_validation import (
    ValidationSettings,
    validate_samples,
    validation_data_dir,
)
from src.storage.market_bars import upsert_market_bars
from src.storage.projections_store import upsert_projections

SETTINGS = ValidationSettings(
    holdout_run_dates=2, min_training_run_dates=2, min_training_samples=4, min_cohort_samples=2
)


def _samples():
    calendar = get_market_calendar()
    sessions = calendar.sessions_in_range("2026-01-05", "2026-02-20")
    rows = []
    for index, session in enumerate(sessions[:15]):
        target = sessions[index + 5]
        generated = calendar.session_close(session).to_pydatetime() + timedelta(hours=1)
        available = calendar.session_close(sessions[index + 6]).to_pydatetime()
        for symbol, predicted, confidence in [("AAPL", 102.0, 80), ("MSFT", 98.0, 60)]:
            actual = 101.0 if index % 2 else 99.0
            rows.append(
                {
                    "runDate": session.date().isoformat(),
                    "targetDate": target.date().isoformat(),
                    "symbol": symbol,
                    "current": 100.0,
                    "predicted": predicted,
                    "targetLow": predicted - 0.1,
                    "targetHigh": predicted + 0.1,
                    "actual": actual,
                    "confidence": confidence,
                    "confidenceBand": "80-89" if confidence == 80 else "60-69",
                    "bandHit": False,
                    "actualProvenance": "verified_previous_close",
                    "generationProvenance": "timestamped",
                    "generatedAt": generated.isoformat(),
                    "outcomeAvailableAt": available.isoformat(),
                }
            )
    return rows


def test_temporal_split_purges_unavailable_outcomes_and_keeps_whole_run_dates():
    report = validate_samples(_samples(), SETTINGS)
    assert report["status"] == "evaluated"
    split = report["split"]
    assert split["holdoutSampleCount"] == 4
    assert len(split["holdoutRunDates"]) == 2
    assert split["purgedUnavailableOutcomeCount"] > 0
    assert split["trainingRunDates"][-1] < split["holdoutRunDates"][0]
    assert split["latestTrainingOutcomeAvailableAt"] < split["earliestHoldoutGeneratedAt"]
    assert report["promotionDecision"] == "research_only"


def test_equal_cutoff_and_late_recorded_outcomes_are_not_training_data():
    rows = _samples()
    cutoff = rows[-4]["generatedAt"]
    for row in rows[:4]:
        row["outcomeAvailableAt"] = cutoff
    report = validate_samples(rows, SETTINGS)
    assert report["status"] == "evaluated"
    assert rows[0]["runDate"] not in report["split"]["trainingRunDates"]
    assert rows[2]["runDate"] not in report["split"]["trainingRunDates"]


def test_changing_holdout_outcomes_cannot_change_fitted_parameters():
    rows = _samples()
    original = validate_samples(rows, SETTINGS)
    changed = deepcopy(rows)
    for row in changed[-4:]:
        row["actual"] = 150.0
    new = validate_samples(changed, SETTINGS)
    assert new["fittedOnEarlierData"] == original["fittedOnEarlierData"]
    assert new["heldOut"]["calibrationCandidate"] != original["heldOut"]["calibrationCandidate"]
    assert validate_samples(list(reversed(rows)), SETTINGS) == original


def test_benchmarks_use_identical_outcomes_and_report_paired_daily_comparisons():
    rows = _samples()
    report = validate_samples(rows, SETTINGS)
    benchmarks = report["allScoredBenchmarks"]
    assert benchmarks["incumbent"]["sampleCount"] == len(rows)
    assert benchmarks["unchangedPrice"]["sampleCount"] == len(rows)
    assert (
        benchmarks["unchangedPrice"]["meanAbsErrorPct"] < benchmarks["incumbent"]["meanAbsErrorPct"]
    )
    assert benchmarks["runDateWins"] == 0
    assert benchmarks["runDateLosses"] == 15
    assert benchmarks["equalRunDateMeanErrorImprovementPct"] < 0
    assert benchmarks["alwaysUpDirectionalAccuracyPct"] == pytest.approx(7 / 15 * 100)
    assert benchmarks["unchangedPrice"]["directionalAccuracyPct"] == 0
    assert benchmarks["unchangedPrice"]["brierScore"] is None


def test_frozen_confidence_and_residual_interval_use_training_only():
    report = validate_samples(_samples(), SETTINGS)
    fit = report["fittedOnEarlierData"]
    # Every training residual is either 1% or 3% of the origin price.
    assert fit["residualQuantile"] == pytest.approx(0.03)
    assert fit["residualQuantileRank"] == 14
    assert fit["fallbackConfidence"] == 0.5
    assert fit["confidenceByCohort"] == {"60-69": 0.5, "80-89": 0.5}
    heldout = report["heldOut"]
    assert heldout["calibrationCandidate"]["bandCoveragePct"] == 100.0
    assert (
        heldout["calibrationCandidate"]["bandSampleCount"]
        == heldout["incumbent"]["bandSampleCount"]
    )
    assert heldout["calibrationCandidate"]["brierScore"] == 0.25
    assert heldout["calibrationCandidate"]["brierScore"] < heldout["incumbent"]["brierScore"]
    assert (
        heldout["calibrationCandidate"]["meanAbsErrorPct"]
        == heldout["incumbent"]["meanAbsErrorPct"]
    )


def test_small_or_unseen_confidence_cohorts_use_the_training_fallback():
    rows = _samples()
    for row in rows[-4:]:
        row["confidenceBand"] = "90-100"
    report = validate_samples(rows, SETTINGS)
    assert "90-100" not in report["fittedOnEarlierData"]["confidenceByCohort"]
    assert report["heldOut"]["fallbackConfidenceSampleCount"] == 4
    assert report["heldOut"]["calibrationCandidate"]["meanConfidence"] == 50.0


@pytest.mark.parametrize(
    "field,value",
    [
        ("actualProvenance", "legacy_filename"),
        ("generationProvenance", "legacy_run_date"),
        ("generatedAt", "2026-01-05T21:00:00"),
        ("outcomeAvailableAt", None),
        ("outcomeAvailableAt", "2025-01-01T21:00:00+00:00"),
        ("current", float("nan")),
        ("current", 0),
    ],
)
def test_unverified_or_unusable_samples_are_excluded(field, value):
    rows = _samples()
    rows[0][field] = value
    report = validate_samples(rows, SETTINGS)
    assert report["excludedSampleCount"] == 1
    assert report["eligibleSampleCount"] == len(rows) - 1


def test_insufficient_training_does_not_shrink_the_holdout_or_fit_parameters():
    report = validate_samples(_samples()[-8:], SETTINGS)
    assert report["status"] == "insufficient_data"
    assert report["heldOut"] is None
    assert "fittedOnEarlierData" not in report
    assert report["failures"]
    assert validate_samples([], SETTINGS)["status"] == "insufficient_data"


@pytest.mark.parametrize(
    "settings",
    [
        {"holdout_run_dates": 0},
        {"min_training_samples": -1},
        {"interval_coverage": 1},
    ],
)
def test_invalid_protocol_settings_are_rejected(settings):
    with pytest.raises(ValueError):
        ValidationSettings(**settings)


def test_storage_adapter_uses_availability_of_the_evaluated_revision(tmp_path, monkeypatch):
    rows = _samples()
    forecast = rows[0]
    upsert_projections(
        [
            {
                "symbol": "AAPL",
                "current_price": 100,
                "target_mid": 102,
                "confidence": 80,
                "generated_at": forecast["generatedAt"],
            }
        ],
        forecast["runDate"],
        data_dir=tmp_path,
    )
    first_stamp = "2026-01-16T22:00:00+00:00"
    for day, price, stamp in [
        ("2026-01-12", 999, first_stamp),
        ("2026-01-14", 99, "2026-01-14T22:00:00+00:00"),
        ("2026-01-15", 99, "2026-01-15T22:00:00+00:00"),
    ]:
        monkeypatch.setattr(market_bars, "_utc_now", lambda: stamp)
        upsert_market_bars(
            [
                {
                    "symbol": "AAPL",
                    "close": 100,
                    "outcome_session": forecast["targetDate"],
                    "outcome_close": price,
                    "outcome_final": True,
                }
            ],
            day,
            data_dir=tmp_path,
        )
    report = validation_data_dir(tmp_path)
    assert report["eligibleSampleCount"] == 1
    # A single scored date cannot fit a candidate, but provenance must remain valid.
    assert report["status"] == "insufficient_data"
    assert report["sourceSummary"]["sampleCount"] == 1
    assert report["latestOutcomeAvailableAt"] == first_stamp


def test_explicit_sidecar_validation_refuses_hosted_database_override(tmp_path, monkeypatch):
    monkeypatch.setenv("MARKETHELM_DATABASE_URL", "postgresql://unused/unused")
    with pytest.raises(ValueError, match="Unset MARKETHELM_DATABASE_URL"):
        validation_data_dir(tmp_path)

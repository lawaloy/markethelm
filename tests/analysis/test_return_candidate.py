"""Train/predict agreement, temporal leakage protection and immutable shadow enrollment."""

from copy import deepcopy
from datetime import datetime, timedelta, timezone

import pytest

from src.analysis.forecast_models import builtin_models, canonical_json, live_models
from src.analysis.forward_evaluation import forward_report
from src.analysis.market_calendar import get_market_calendar
from src.analysis.projector import StockProjector
from src.analysis.return_candidate import MODEL_VERSION, ReturnCandidate, bundled_candidate
from src.analysis.return_training import train_candidate
from src.storage.forecast_ledger import ledger_snapshot, record_forecast_run
from tests.helpers.forecasts import projection, stock
from tests.helpers.forecasts import training_samples as _samples

FROZEN = datetime(2026, 11, 3, tzinfo=timezone.utc)


def _fit(rows=None):
    return train_candidate(
        _samples() if rows is None else rows, frozen_at=FROZEN, source={"fixture": True}
    )


def test_closed_form_fit_matches_fixed_ridge_objective_and_preserves_full_holdout():
    artifact, report = _fit()
    assert artifact["coefficients"] == pytest.approx([0.005, 0.0008 / 1.16])
    assert artifact["training"]["sample_count"] == 450
    assert report["split"]["holdoutSampleCount"] == 150
    assert report["split"]["purgedUnavailableOutcomeCount"] == 150
    assert (
        artifact["training"]["latest_outcome_available_at"]
        < artifact["training"]["available_before"]
    )
    assert report["promotion"] == "shadow_only"
    assert (
        report["heldOut"]["candidate"]["sampleCount"]
        == report["heldOut"]["incumbent"]["sampleCount"]
    )


def test_holdout_and_late_available_labels_cannot_change_candidate_parameters():
    rows = _samples()
    artifact, report = _fit(rows)
    dates = report["split"]["holdoutRunDates"]
    cutoff = report["split"]["earliestHoldoutGeneratedAt"]
    changed = deepcopy(rows)
    for row in changed:
        if row["runDate"] in dates or row["outcomeAvailableAt"] >= cutoff:
            row["actual"] = 150.0
    new, diagnostic = _fit(changed)
    assert new == artifact
    assert diagnostic["heldOut"] != report["heldOut"]
    assert _fit(list(reversed(rows))) == (artifact, report)


def test_origin_session_weights_do_not_change_when_one_session_has_more_stocks():
    rows = _samples()
    original, _ = _fit(rows)
    duplicates = [dict(row, symbol="EXTRA" + row["symbol"]) for row in rows[:30]]
    repeated, _ = _fit(rows + duplicates)
    assert repeated["coefficients"] == pytest.approx(original["coefficients"])


def test_small_training_data_is_rejected_without_changing_split_or_regularization():
    with pytest.raises(ValueError, match="Insufficient"):
        _fit(_samples()[-180:])


def test_frozen_candidate_has_no_outcome_inputs_or_unestimated_confidence():
    artifact, _ = _fit()
    candidate = ReturnCandidate(canonical_json(artifact))
    now = FROZEN + timedelta(days=1)
    p = projection(now)
    before = deepcopy(p)
    predicted = candidate.predict(p, {"outcome_close": 1_000_000})
    assert predicted == candidate.predict(p, {"outcome_close": 1})
    assert predicted == {"target_mid": candidate.target(p["current_price"], p["target_mid"])}
    assert p == before
    with pytest.raises(ValueError, match="before"):
        candidate.predict(projection(FROZEN), {})
    with pytest.raises(ValueError, match="before"):
        candidate.predict(projection(FROZEN - timedelta(seconds=1)), {})


@pytest.mark.parametrize("origin,incumbent", [(0, 100), (100, float("nan")), (float("inf"), 10)])
def test_dirty_candidate_inputs_are_rejected(origin, incumbent):
    with pytest.raises(ValueError):
        bundled_candidate().target(origin, incumbent)


def test_extreme_finite_signals_are_bounded_and_artifact_changes_require_new_versions():
    artifact, _ = _fit()
    artifact["coefficients"] = [10.0, 10.0]
    candidate = ReturnCandidate(canonical_json(artifact))
    assert candidate.target(100, 1000) == pytest.approx(105)
    artifact["coefficients"] = [-10.0, 10.0]
    assert ReturnCandidate(canonical_json(artifact)).target(100, 1) == pytest.approx(95)
    artifact["recipe"]["ridge_penalty"] = 0
    with pytest.raises(ValueError, match="Unsupported"):
        ReturnCandidate(canonical_json(artifact))


def test_new_live_cases_enroll_candidate_with_references_and_score_on_same_outcome(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("MARKETHELM_DATABASE_URL", "")
    candidate = bundled_candidate()
    calendar = get_market_calendar()
    anchor = calendar.date_to_session(candidate.frozen_at.date().isoformat(), direction="next")
    session = calendar.next_session(anchor)
    now = calendar.session_close(session).to_pydatetime() + timedelta(hours=1)
    assert len(live_models(candidate.frozen_at)) == 4
    assert len(live_models(now)) == 5
    quote = stock(quote_timestamp=calendar.session_close(session).isoformat())
    first = record_forecast_run({"AAPL": projection(now)}, [quote], data_dir=tmp_path, now=now)
    assert first["new_cases"] == 1
    before = ledger_snapshot(data_dir=tmp_path)
    assert len(before["forecast_predictions"]) == 5
    assert len({row["case_id"] for row in before["forecast_predictions"]}) == 1
    target = projection(now)["projection_date"]
    observed_session = calendar.next_session(target)
    later = calendar.session_close(observed_session).to_pydatetime() + timedelta(hours=1)
    outcome = stock(
        quote_timestamp=calendar.session_close(observed_session).isoformat(),
        outcome_session=target,
        outcome_close=104,
    )
    recorded = record_forecast_run({}, [outcome], data_dir=tmp_path, now=later)
    assert recorded["new_scores"] == 5
    report = forward_report(data_dir=tmp_path, now=later)
    shadow = report["models"][MODEL_VERSION]
    assert shadow["metrics"]["scoredCount"] == 1
    assert shadow["metrics"]["confidenceSampleCount"] == 0
    assert shadow["metrics"]["bandSampleCount"] == 0
    assert shadow["comparisons"]["heuristic-v1"]["scoredPairCount"] == 1
    assert shadow["comparisons"]["unchanged-price-v1"]["scoredPairCount"] == 1
    assert shadow["readiness"]["decision"] == "not_ready"
    assert StockProjector.MODEL_VERSION == "heuristic-v1"


@pytest.mark.parametrize("generation_offset", [-1, 1])
def test_default_retry_preserves_pre_upgrade_model_set_and_commits_new_outcomes(
    tmp_path, monkeypatch, generation_offset
):
    monkeypatch.setenv("MARKETHELM_DATABASE_URL", "")
    candidate = bundled_candidate()
    # Keep generation/recording on the same session anchor on either side of the freeze.
    generated = candidate.frozen_at + timedelta(seconds=generation_offset)
    p = projection(generated)
    quote = stock(quote_timestamp=(generated - timedelta(seconds=1)).isoformat())
    original = record_forecast_run(
        {"AAPL": p}, [quote], data_dir=tmp_path, now=generated, models=builtin_models()
    )
    assert original["new_cases"] == 1
    before = ledger_snapshot(data_dir=tmp_path)
    retry_time = candidate.frozen_at + timedelta(seconds=2)
    calendar = get_market_calendar()
    session = calendar.date_to_session(retry_time.astimezone(calendar.tz).date().isoformat())
    previous = calendar.previous_session(session).date().isoformat()
    observed = stock(
        symbol="EXTRA",
        quote_timestamp=retry_time.isoformat(),
        outcome_session=previous,
        outcome_close=90,
    )
    result = record_forecast_run({"AAPL": p}, [quote, observed], data_dir=tmp_path, now=retry_time)
    after = ledger_snapshot(data_dir=tmp_path)
    assert result["replayed_cases"] == 1 and result["new_cases"] == 0
    assert result["new_outcomes"] == 1
    assert after["forecast_cases"] == before["forecast_cases"]
    assert after["forecast_predictions"] == before["forecast_predictions"]
    assert len(after["forecast_predictions"]) == 4


def test_first_recording_after_freeze_uses_generation_time_for_enrollment(tmp_path, monkeypatch):
    monkeypatch.setenv("MARKETHELM_DATABASE_URL", "")
    frozen = bundled_candidate().frozen_at
    generated = frozen - timedelta(seconds=1)
    p = projection(generated)
    quote = stock(quote_timestamp=(generated - timedelta(seconds=1)).isoformat())
    result = record_forecast_run(
        {"AAPL": p}, [quote], data_dir=tmp_path, now=frozen + timedelta(seconds=1)
    )
    assert result["new_cases"] == 1
    assert len(ledger_snapshot(data_dir=tmp_path)["forecast_predictions"]) == 4

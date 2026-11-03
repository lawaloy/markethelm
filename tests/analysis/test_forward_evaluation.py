"""Prospective reports use recorded identities, matched samples and frozen gates."""

from __future__ import annotations

from datetime import timedelta

import pytest

from src.analysis.forecast_models import ForecastModel, builtin_models
from src.analysis.forward_evaluation import forward_report
from src.analysis.market_calendar import get_market_calendar
from src.analysis.projector import StockProjector
from src.storage.forecast_ledger import record_forecast_run
from src.storage.projections_store import upsert_projections
from tests.helpers.forecasts import LATER, NOW, outcome, projection, record, stock


def test_historical_snapshots_are_not_presented_as_prospective_forecasts(tmp_path, monkeypatch):
    monkeypatch.setenv("MARKETHELM_DATABASE_URL", "")
    upsert_projections([projection()], "2026-07-06", data_dir=tmp_path)
    result = forward_report(data_dir=tmp_path, now=NOW)
    assert result["status"] == "not_started"
    assert result["models"] == {}
    assert result["promotion"] == "manual_only"


def test_repeated_refreshes_and_unverified_inputs_do_not_inflate_primary_cohort(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("MARKETHELM_DATABASE_URL", "")
    record(tmp_path)
    next_time = NOW + timedelta(minutes=1)
    record(tmp_path, p=projection(next_time, target_mid=110), now=next_time)
    later = NOW + timedelta(minutes=2)
    record(tmp_path, p=projection(later), rows=[stock(quote_timestamp=None)], now=later)
    record_forecast_run({}, [outcome()], data_dir=tmp_path, now=LATER)
    report = forward_report(data_dir=tmp_path, now=LATER)
    assert report["recordedCaseCount"] == 3
    assert report["selectedCaseCount"] == 1
    assert report["excludedRepeatedSessionCases"] == 1
    assert report["excludedUnverifiedInputCases"] == 1
    incumbent = report["models"]["heuristic-v1"]
    assert incumbent["metrics"]["scoredCount"] == 1
    assert incumbent["metrics"]["meanAbsErrorPct"] == pytest.approx(2 / 104 * 100)
    comparison = incumbent["comparisons"]["unchanged-price-v1"]
    assert comparison["scoredPairCount"] == 1
    assert comparison["relativeErrorImprovementPct"] == pytest.approx(50)
    assert incumbent["readiness"]["passed"] is False
    assert incumbent["readiness"]["decision"] == "not_ready"
    assert list(incumbent["byOriginSession"]) == ["2026-07-06"]


def test_asof_report_does_not_see_future_scores_and_missing_target_closes_are_explicit(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("MARKETHELM_DATABASE_URL", "")
    record(tmp_path)
    record_forecast_run({}, [outcome()], data_dir=tmp_path, now=LATER)
    pending = forward_report(data_dir=tmp_path, now=NOW)
    metrics = pending["models"]["heuristic-v1"]["metrics"]
    assert metrics["pendingCount"] == 1 and metrics["scoredCount"] == 0
    due = forward_report(data_dir=tmp_path, now=LATER - timedelta(days=1))
    metrics = due["models"]["heuristic-v1"]["metrics"]
    assert metrics["missingOutcomeCount"] == 1 and metrics["outcomeCoveragePct"] == 0
    assert (
        forward_report(data_dir=tmp_path, now=LATER)["models"]["heuristic-v1"]["metrics"][
            "scoredCount"
        ]
        == 1
    )


def _fixed_candidate(projection, snapshot):
    return {"target_mid": snapshot["close"] * 1.02}


def test_full_numeric_gate_still_requires_manual_review_and_never_switches_application_model(
    tmp_path, monkeypatch
):
    """A synthetic perfect candidate proves the positive path without claiming market skill."""
    monkeypatch.setenv("MARKETHELM_DATABASE_URL", "")
    calendar = get_market_calendar()
    sessions = calendar.sessions_in_range("2026-01-05", "2026-03-02")
    candidate = ForecastModel(
        "synthetic-candidate-v1", "point", {"multiplier": 1.02}, _fixed_candidate
    )
    models = (*builtin_models(), candidate)
    for index, session in enumerate(sessions[:26]):
        now = calendar.session_close(session).to_pydatetime() + timedelta(hours=1)
        previous = calendar.previous_session(session).date().isoformat()
        rows, forecasts = [], {}
        for number in range(25):
            symbol = f"S{number:02d}"
            rows.append(
                stock(
                    symbol=symbol,
                    quote_timestamp=calendar.session_close(session).isoformat(),
                    outcome_session=previous,
                    outcome_close=102.0,
                )
            )
            if index < 20:
                forecasts[symbol] = projection(now, symbol=symbol, target_mid=104.0)
        record_forecast_run(forecasts, rows, data_dir=tmp_path, now=now, models=models)
    result = forward_report(data_dir=tmp_path, now=now)
    model = result["models"]["synthetic-candidate-v1"]
    assert model["metrics"]["scoredCount"] == 500
    assert model["metrics"]["distinctOriginSessions"] == 20
    assert model["metrics"]["distinctSymbols"] == 25
    assert model["readiness"]["passed"] is True
    assert model["readiness"]["decision"] == "manual_review_required"
    assert result["promotion"] == "manual_only"
    assert StockProjector.MODEL_VERSION == "heuristic-v1"
    # References and candidate share exactly the same 500 scored case identities.
    assert model["comparisons"]["heuristic-v1"]["scoredPairCount"] == 500
    assert model["comparisons"]["unchanged-price-v1"]["scoredPairCount"] == 500


@pytest.mark.parametrize("days", [0, -1, 3651])
def test_report_rejects_unbounded_or_invalid_windows(tmp_path, days):
    with pytest.raises(ValueError):
        forward_report(data_dir=tmp_path, days=days)


def test_lookback_window_cannot_promote_a_later_refresh_over_the_original(tmp_path, monkeypatch):
    monkeypatch.setenv("MARKETHELM_DATABASE_URL", "")
    record(tmp_path)
    later = NOW + timedelta(minutes=10)
    record(tmp_path, p=projection(later, target_mid=105), now=later)
    result = forward_report(data_dir=tmp_path, days=1, now=NOW + timedelta(days=1, minutes=5))
    assert result["recordedCaseCount"] == 1
    assert result["selectedCaseCount"] == 0
    assert result["excludedRepeatedSessionCases"] == 1

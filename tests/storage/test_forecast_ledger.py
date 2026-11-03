"""Immutable forecasts remain original across reruns, revisions and concurrent writes."""

from __future__ import annotations

import sqlite3
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest

import src.storage.database as database
from src.analysis.forecast_models import ForecastModel, builtin_models, protocol_definition
from src.storage.forecast_ledger import (
    ForecastConflict,
    ledger_connection,
    ledger_snapshot,
    outcome_symbols_due,
    record_forecast_run,
    score_pending,
)
from src.storage.market_bars import upsert_market_bars
from src.storage.projections_store import upsert_projections
from tests.helpers.forecasts import LATER, NOW, outcome, projection, record, stock


@pytest.fixture(params=["sidecar", "hosted"])
def ledger(tmp_path, monkeypatch, request):
    monkeypatch.setenv("MARKETHELM_DATABASE_URL", "")
    if request.param == "hosted":
        monkeypatch.setenv("MARKETHELM_DATABASE_URL", f"sqlite:///{(tmp_path/'app.db').as_posix()}")
        database.init_database()
    return tmp_path


def test_original_predictions_survive_identical_retries_and_mutable_snapshot_overwrites(ledger):
    first = record(ledger)
    before = ledger_snapshot(data_dir=ledger)
    again = record(ledger, now=NOW + timedelta(seconds=1))
    assert first["new_cases"] == 1
    assert again["new_cases"] == 0 and again["replayed_cases"] == 1
    upsert_projections([projection(target_mid=999)], "2026-07-06", data_dir=ledger)
    upsert_market_bars([stock(close=999)], "2026-07-06", data_dir=ledger)
    assert ledger_snapshot(data_dir=ledger) == before
    assert len(before["forecast_predictions"]) == 4
    assert len(before["forecast_cases"]) == 1


def test_conflicting_retry_rolls_back_and_new_generation_preserves_both_cases(ledger):
    record(ledger)
    before = ledger_snapshot(data_dir=ledger)
    with pytest.raises(ForecastConflict, match="overwrite"):
        record(ledger, p=projection(target_mid=103))
    assert ledger_snapshot(data_dir=ledger) == before
    now = NOW + timedelta(minutes=1)
    record(ledger, p=projection(now), now=now)
    after = ledger_snapshot(data_dir=ledger)
    assert len(after["forecast_cases"]) == 2
    assert len(after["forecast_predictions"]) == 8
    assert {row["id"] for row in before["forecast_predictions"]}.issubset(
        {row["id"] for row in after["forecast_predictions"]}
    )


def _candidate(projection, snapshot):
    return {"target_mid": snapshot["close"] * 1.01}


def test_new_model_versions_share_inputs_and_cannot_be_attached_late(ledger):
    candidate = ForecastModel("candidate-v1", "point", {"multiplier": 1.01}, _candidate)
    record(ledger, models=(*builtin_models(), candidate))
    before = ledger_snapshot(data_dir=ledger)
    assert len(before["forecast_predictions"]) == 5
    assert len({row["case_id"] for row in before["forecast_predictions"]}) == 1
    with pytest.raises(ForecastConflict, match="attach"):
        record(ledger)
    assert ledger_snapshot(data_dir=ledger) == before


def test_frozen_definition_change_requires_a_new_version(ledger):
    record(ledger)
    models = list(builtin_models())
    models[0] = replace(models[0], definition={"changed": True})
    with pytest.raises(ForecastConflict, match="Frozen definition changed"):
        record(ledger, models=models)


def test_concurrent_writers_only_append_one_complete_comparison_bundle(ledger):
    # Initialize DDL before racing application transactions.
    ledger_snapshot(data_dir=ledger)
    with ThreadPoolExecutor(max_workers=6) as pool:
        results = list(pool.map(lambda _: record(ledger), range(6)))
    assert sum(r["new_cases"] for r in results) == 1
    snapshot = ledger_snapshot(data_dir=ledger)
    assert len(snapshot["forecast_cases"]) == 1
    assert len(snapshot["forecast_predictions"]) == 4
    assert len(snapshot["forecast_models"]) == 4


def test_verified_exact_session_outcomes_score_once_and_corrections_do_not_rewrite(ledger):
    record(ledger)
    assert score_pending(data_dir=ledger, now=datetime(2026, 7, 13, 21, tzinfo=timezone.utc)) == 0
    result = record_forecast_run({}, [outcome()], data_dir=ledger, now=LATER)
    assert result["new_scores"] == 4
    first = ledger_snapshot(data_dir=ledger)
    predictions = {row["id"]: row for row in first["forecast_predictions"]}
    original = next(
        row
        for row in first["forecast_scores"]
        if predictions[row["prediction_id"]]["model_version"] == "heuristic-v1"
    )
    assert original["abs_error_pct"] == pytest.approx(2 / 104 * 100)
    assert original["brier_score"] == pytest.approx(0.04)
    assert original["direction_correct"] == 1 and original["band_hit"] == 1
    record_forecast_run(
        {}, [outcome(outcome_close=120)], data_dir=ledger, now=LATER + timedelta(minutes=1)
    )
    after = ledger_snapshot(data_dir=ledger)
    assert after["forecast_scores"] == first["forecast_scores"]
    assert len(after["forecast_outcomes"]) == len(first["forecast_outcomes"]) + 1
    assert score_pending(data_dir=ledger, now=LATER + timedelta(minutes=2)) == 0


@pytest.mark.parametrize(
    "change",
    [
        {"outcome_final": False},
        {"outcome_session": "2026-07-10"},
        {"outcome_close": float("nan")},
        {"quote_timestamp": "2026-07-14T20:00:00"},
        {"quote_timestamp": "2026-07-15T20:00:00+00:00"},
    ],
)
def test_unverified_wrong_session_or_future_outcomes_never_score(ledger, change):
    record(ledger)
    result = record_forecast_run({}, [outcome(**change)], data_dir=ledger, now=LATER)
    assert result["new_scores"] == 0
    assert not ledger_snapshot(data_dir=ledger)["forecast_scores"]


def test_historical_projection_replay_is_not_backfilled_as_prospective(ledger):
    result = record(ledger, now=LATER)
    assert result["new_cases"] == 0 and result["skipped_invalid"] == 1
    assert not ledger_snapshot(data_dir=ledger)["forecast_cases"]


def test_missing_quote_provenance_is_retained_but_marked_unverified(ledger):
    result = record(ledger, rows=[stock(quote_timestamp=None)])
    assert result["new_cases"] == 1 and result["unverified_input_cases"] == 1
    row = ledger_snapshot(data_dir=ledger)["forecast_cases"][0]
    assert row["input_verified"] == 0


def test_direct_update_and_delete_are_blocked_by_database_guards(ledger):
    record(ledger)
    record_forecast_run({}, [outcome()], data_dir=ledger, now=LATER)
    for table in (
        "forecast_protocols",
        "forecast_models",
        "forecast_cases",
        "forecast_predictions",
        "forecast_outcomes",
        "forecast_scores",
    ):
        for statement in (
            f"DELETE FROM {table}",
            f"UPDATE {table} SET "
            + (
                "version=version"
                if table in {"forecast_models", "forecast_protocols"}
                else "prediction_id=prediction_id" if table == "forecast_scores" else "id=id"
            ),
        ):
            with pytest.raises(sqlite3.IntegrityError, match="append-only"):
                with ledger_connection(ledger, write=True) as conn:
                    conn.execute(statement)
    assert len(ledger_snapshot(data_dir=ledger)["forecast_predictions"]) == 4


def test_due_symbols_follow_provider_previous_close_and_stop_after_observation(ledger):
    record(ledger)
    assert outcome_symbols_due(data_dir=ledger, now=NOW) == []
    assert outcome_symbols_due(data_dir=ledger, now=LATER) == ["AAPL"]
    record_forecast_run({}, [outcome()], data_dir=ledger, now=LATER)
    assert outcome_symbols_due(data_dir=ledger, now=LATER) == []
    assert (
        outcome_symbols_due(data_dir=ledger, now=datetime(2026, 7, 18, 21, tzinfo=timezone.utc))
        == []
    )


def test_mutating_model_parameters_during_prediction_rolls_back_the_whole_bundle(ledger):
    parameters = {"multiplier": 1.01}

    def changing_model(projection, snapshot):
        parameters["multiplier"] = 1.5
        return {"target_mid": snapshot["close"] * parameters["multiplier"]}

    model = ForecastModel("mutable-test-v1", "point", parameters, changing_model)
    with pytest.raises(ForecastConflict, match="parameters changed"):
        record(ledger, models=(*builtin_models(), model))
    assert ledger_snapshot(data_dir=ledger)["forecast_cases"] == []
    assert ledger_snapshot(data_dir=ledger)["forecast_models"] == []


def test_sqlite_replace_cannot_bypass_append_only_guards(ledger):
    record(ledger)
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        with ledger_connection(ledger, write=True) as conn:
            conn.execute("INSERT OR REPLACE INTO forecast_models SELECT * FROM forecast_models")
    assert len(ledger_snapshot(data_dir=ledger)["forecast_models"]) == 4


def test_migration_eight_upgrades_without_backfilling_old_mutable_projections(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("MARKETHELM_DATABASE_URL", f"sqlite:///{(tmp_path/'upgrade.db').as_posix()}")
    migrations = database._MIGRATIONS
    with monkeypatch.context() as legacy:
        legacy.setattr(database, "_MIGRATIONS", migrations[:8])
        legacy.setattr(database, "LATEST_SCHEMA_VERSION", 8)
        database.init_database()
        upsert_market_bars([stock()], "2026-07-06")
        upsert_projections([projection()], "2026-07-06")
    database.init_database()
    database.init_database()
    with database.get_connection() as conn:
        assert conn.execute("SELECT close FROM market_bars").fetchone()["close"] == 100
        assert conn.execute("SELECT target_mid FROM projections").fetchone()["target_mid"] == 102
        assert (
            conn.execute("SELECT version FROM schema_migrations ORDER BY version DESC").fetchone()[
                "version"
            ]
            == 9
        )
    assert ledger_snapshot(data_dir=None)["forecast_cases"] == []


def test_changed_scoring_implementation_cannot_mix_into_a_frozen_protocol(ledger, monkeypatch):
    record(ledger)
    definition = protocol_definition()
    monkeypatch.setattr(
        "src.storage.forecast_ledger.protocol_definition",
        lambda: {
            **definition,
            "scoring_implementation_sha256": "changed",
        },
    )
    # Recording rejects the changed frozen declaration before writing observations.
    with pytest.raises(ForecastConflict, match="Frozen definition changed"):
        record_forecast_run({}, [outcome()], data_dir=ledger, now=LATER)
    assert ledger_snapshot(data_dir=ledger)["forecast_scores"] == []

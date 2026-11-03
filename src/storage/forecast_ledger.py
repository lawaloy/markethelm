"""Append-only, versioned forecasts and first-observation outcome scoring."""

from __future__ import annotations

import json
import math
from collections import Counter
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import MappingProxyType
from typing import Sequence

import src.storage.market_bars as market_bars
from src.analysis.forecast_models import (
    PROTOCOL_VERSION,
    ForecastModel,
    canonical_json,
    fingerprint,
    live_models,
    protocol_definition,
)
from src.analysis.forecast_scoring import SCORING_VERSION, score_prediction
from src.analysis.market_calendar import (
    get_market_calendar,
    last_completed_session,
    previous_close_session_at,
    trading_session_after_timestamp,
)
from src.storage.forecast_schema import TABLES, ensure_sidecar_schema, schema_exists
from src.utils.tickers import normalize_ticker


class ForecastConflict(ValueError):
    """An existing immutable identity cannot be redefined."""


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def timestamp(value) -> datetime:
    value = (
        value
        if isinstance(value, datetime)
        else datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    )
    if value.utcoffset() is None:
        raise ValueError("timestamp must include a timezone")
    return value.astimezone(timezone.utc)


def stamp(value: datetime) -> str:
    return timestamp(value).isoformat(timespec="microseconds")


def _number(value, *, positive=False):
    try:
        result = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return result if math.isfinite(result) and (not positive or result > 0) else None


@contextmanager
def ledger_connection(data_dir: str | Path | None, *, write=False):
    with market_bars.market_bars_connection(data_dir=data_dir) as conn:
        if getattr(conn, "backend", "sqlite") == "sqlite":
            conn.execute("PRAGMA foreign_keys = ON")
            conn.execute("PRAGMA recursive_triggers = ON")
        if write:
            conn.execute("BEGIN IMMEDIATE")
            ensure_sidecar_schema(conn)
        elif getattr(conn, "backend", "sqlite") == "sqlite":
            conn.execute("BEGIN")
        else:
            conn.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
        yield conn


def _register(conn, table: str, version: str, definition: dict, now: datetime):
    encoded = canonical_json(definition)
    digest = fingerprint(encoded)
    conn.execute(
        f"INSERT INTO {table} (version, definition_json, definition_sha256, registered_at) "
        "VALUES (?, ?, ?, ?) ON CONFLICT(version) DO NOTHING",
        (version, encoded, digest, stamp(now)),
    )
    row = conn.execute(f"SELECT * FROM {table} WHERE version = ?", (version,)).fetchone()
    if row["definition_sha256"] != digest:
        raise ForecastConflict(f"Frozen definition changed for {version}; use a new version")
    if timestamp(row["registered_at"]) > now:
        raise ForecastConflict("Cannot record before the frozen experiment was registered")


def _snapshot(stock: dict) -> dict | None:
    symbol = normalize_ticker(stock.get("symbol"))
    price = _number(stock.get("close"), positive=True)
    change = _number(stock.get("change_percent"))
    if not symbol or price is None or change is None:
        return None
    result = {"symbol": symbol, "close": price, "change_percent": change, "provider": "finnhub"}
    for key in ("volume", "market_cap", "open", "high", "low", "previous_close"):
        result[key] = _number(stock.get(key))
    try:
        result["quote_timestamp"] = stamp(timestamp(stock.get("quote_timestamp")))
    except (ValueError, TypeError, OverflowError):
        result["quote_timestamp"] = None
    return result


def _prediction(model: ForecastModel, projection: dict, snapshot: dict) -> dict:
    raw = model.predict(MappingProxyType(projection), MappingProxyType(snapshot))
    for key in ("target_mid", "target_low", "target_high", "confidence"):
        if raw.get(key) is not None and _number(raw[key], positive=key != "confidence") is None:
            raise ValueError("Forecast values must be finite and valid")
    mid = _number(raw.get("target_mid"), positive=True)
    if model.kind == "point":
        if mid is None:
            raise ValueError("Point forecast requires a finite positive target")
        direction = int(mid > snapshot["close"]) - int(mid < snapshot["close"])
    elif model.kind == "direction" and raw.get("direction") in (-1, 0, 1):
        direction, mid = int(raw["direction"]), None
    else:
        raise ValueError("Unsupported forecast kind or direction")
    low = _number(raw.get("target_low"), positive=True)
    high = _number(raw.get("target_high"), positive=True)
    confidence = _number(raw.get("confidence"))
    if (low is None) != (high is None) or (low is not None and low > high):
        raise ValueError("Invalid forecast range")
    if confidence is not None and not 0 <= confidence <= 100:
        raise ValueError("Confidence must be between zero and 100")
    return {
        "kind": model.kind,
        "target_mid": mid,
        "target_low": low,
        "target_high": high,
        "confidence": confidence,
        "direction": direction,
    }


def _append_case(
    conn,
    projection: dict,
    snapshot: dict,
    models: Sequence[ForecastModel],
    now: datetime,
    *,
    automatic_enrollment: bool = False,
):
    generated = timestamp(projection.get("generated_at"))
    origin = last_completed_session(generated).isoformat()
    target = trading_session_after_timestamp(generated, 5).isoformat()
    if generated > now or origin != last_completed_session(now).isoformat():
        raise ValueError("Forecast generation is outside the current completed-session anchor")
    if projection.get("projection_date") != target:
        raise ValueError("Forecast target must be the exact fifth XNYS session")
    if (
        projection.get("projection_horizon_sessions") != 5
        or projection.get("projection_calendar") != "XNYS"
    ):
        raise ValueError("Forecast horizon/calendar do not match the frozen protocol")
    if projection.get("projection_model_version") != protocol_definition()["incumbent_version"]:
        raise ValueError("Forecast model provenance does not match the configured incumbent")
    if round(snapshot["close"], 2) != _number(projection.get("current_price"), positive=True):
        raise ValueError("Projection origin price does not match its inputs")
    calendar = get_market_calendar()
    if calendar.session_close(target) <= now:
        raise ValueError("Cannot append a forecast after its target close")
    quote = timestamp(snapshot["quote_timestamp"]) if snapshot["quote_timestamp"] else None
    verified = bool(
        quote
        and quote <= generated
        and quote.astimezone(calendar.tz).date().isoformat() == origin
        and quote >= calendar.session_close(origin).to_pydatetime() - timedelta(minutes=15)
    )
    input_json = canonical_json(snapshot)
    input_hash = fingerprint(input_json)
    case_id = fingerprint(
        canonical_json([PROTOCOL_VERSION, snapshot["symbol"], stamp(generated), target, input_hash])
    )
    existing = conn.execute("SELECT * FROM forecast_cases WHERE id = ?", (case_id,)).fetchone()
    if automatic_enrollment:
        versions = (
            json.loads(existing["model_versions_json"])
            if existing
            else [model.version for model in live_models(generated)]
        )
        available = {model.version: model for model in models}
        if set(versions) - available.keys():
            raise ForecastConflict("An enrolled model implementation is unavailable for replay")
        models = tuple(available[version] for version in versions)
    versions_json = canonical_json(sorted(model.version for model in models))
    try:
        predictions = [(model, _prediction(model, projection, snapshot)) for model in models]
    except (TypeError, ValueError, KeyError) as exc:
        raise ForecastConflict("Configured forecast model produced an invalid prediction") from exc
    if existing:
        if existing["model_versions_json"] != versions_json:
            raise ForecastConflict("Cannot attach a model to a previously recorded case")
        for model, payload in predictions:
            previous = conn.execute(
                "SELECT payload_sha256 FROM forecast_predictions "
                "WHERE case_id = ? AND model_version = ?",
                (case_id, model.version),
            ).fetchone()
            if previous is None or previous["payload_sha256"] != fingerprint(
                canonical_json(payload)
            ):
                raise ForecastConflict("Cannot overwrite a previously recorded prediction")
        return False, verified
    known = conn.execute(
        "SELECT id FROM forecast_outcomes WHERE symbol = ? "
        "AND session = ? AND calendar = 'XNYS' LIMIT 1",
        (snapshot["symbol"], target),
    ).fetchone()
    if known:
        raise ForecastConflict("Cannot add a forecast for an already observed outcome")
    conn.execute(
        """INSERT INTO forecast_cases
        (id, protocol_version, symbol, origin_session, target_session, calendar, horizon_sessions,
         generated_at, recorded_at, origin_price, input_json, input_sha256,
         input_verified, model_versions_json)
        VALUES (?, ?, ?, ?, ?, 'XNYS', 5, ?, ?, ?, ?, ?, ?, ?)""",
        (
            case_id,
            PROTOCOL_VERSION,
            snapshot["symbol"],
            origin,
            target,
            stamp(generated),
            stamp(now),
            snapshot["close"],
            input_json,
            input_hash,
            int(verified),
            versions_json,
        ),
    )
    for model, payload in predictions:
        encoded = canonical_json(payload)
        conn.execute(
            """INSERT INTO forecast_predictions
            (id, case_id, model_version, kind, target_mid, target_low, target_high,
             confidence, direction, payload_json, payload_sha256)
             VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                fingerprint(case_id + ":" + model.version),
                case_id,
                model.version,
                model.kind,
                payload["target_mid"],
                payload["target_low"],
                payload["target_high"],
                payload["confidence"],
                payload["direction"],
                encoded,
                fingerprint(encoded),
            ),
        )
    return True, verified


def _observe(conn, stocks: list[dict], now: datetime) -> int:
    expected = previous_close_session_at(now)
    if expected is None:
        return 0
    session = expected.isoformat()
    calendar = get_market_calendar()
    added = 0
    seen = set()
    for stock in stocks:
        if not isinstance(stock, dict) or stock.get("outcome_final") not in (True, 1):
            continue
        symbol = normalize_ticker(stock.get("symbol"))
        price = _number(stock.get("outcome_close"), positive=True)
        if not symbol or symbol in seen or price is None or stock.get("outcome_session") != session:
            continue
        try:
            quote = timestamp(stock.get("quote_timestamp"))
        except (TypeError, ValueError, OverflowError):
            continue
        quote_day = quote.astimezone(calendar.tz).date().isoformat()
        today = now.astimezone(calendar.tz).date().isoformat()
        if quote > now + timedelta(minutes=5) or quote_day not in {session, today}:
            continue
        if calendar.session_close(session) > now:
            continue
        seen.add(symbol)
        provenance = canonical_json(
            {
                "provider": "finnhub",
                "field": "pc",
                "session": session,
                "symbol": symbol,
                "close": price,
                "quote_timestamp": stamp(quote),
            }
        )
        cursor = conn.execute(
            """INSERT INTO forecast_outcomes
            (id, symbol, session, calendar, close, observed_at, quote_timestamp, provenance_json)
            VALUES (?, ?, ?, 'XNYS', ?, ?, ?, ?) ON CONFLICT(id) DO NOTHING""",
            (fingerprint(provenance), symbol, session, price, stamp(now), stamp(quote), provenance),
        )
        added += cursor.rowcount
    return added


def _score(conn, now: datetime) -> int:
    rows = conn.execute(
        """SELECT p.*, c.origin_price, c.protocol_version, o.id AS outcome_id, o.close AS actual
        FROM forecast_predictions p JOIN forecast_cases c ON c.id = p.case_id
        JOIN forecast_outcomes o ON o.symbol = c.symbol AND o.session = c.target_session
            AND o.calendar = c.calendar AND o.observed_at > c.recorded_at AND o.observed_at <= ?
        LEFT JOIN forecast_scores s ON s.prediction_id = p.id
        WHERE s.prediction_id IS NULL AND c.target_session <= ?
        ORDER BY o.observed_at, o.id, p.id""",
        (stamp(now), last_completed_session(now).isoformat()),
    ).fetchall()
    frozen = {
        row["version"]: json.loads(row["definition_json"])
        for row in conn.execute(
            "SELECT version, definition_json FROM forecast_protocols"
        ).fetchall()
    }
    scoring_hash = protocol_definition()["scoring_implementation_sha256"]
    seen = set()
    for row in rows:
        if row["id"] in seen:
            continue
        definition = frozen[row["protocol_version"]]
        if definition["scoring_rule_version"] != SCORING_VERSION:
            continue
        if definition["scoring_implementation_sha256"] != scoring_hash:
            continue
        try:
            result = score_prediction(row, row["origin_price"], row["actual"])
        except (ValueError, OverflowError):
            continue
        seen.add(row["id"])
        conn.execute(
            """INSERT INTO forecast_scores
            (prediction_id, outcome_id, rule_version, scored_at,
             abs_error_pct, direction_correct, band_hit, brier_score)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT(prediction_id) DO NOTHING""",
            (
                row["id"],
                row["outcome_id"],
                SCORING_VERSION,
                stamp(now),
                result["abs_error_pct"],
                result["direction_correct"],
                result["band_hit"],
                result["brier_score"],
            ),
        )
    return len(seen)


def record_forecast_run(
    projections: dict,
    stocks: list[dict],
    *,
    data_dir: str | Path | None,
    now: datetime | None = None,
    models: Sequence[ForecastModel] | None = None,
) -> dict:
    """Record live forecasts and available outcomes in a single fenced transaction.

    Exact retries are idempotent. A new generation time produces a new case;
    changing a frozen definition or output for an existing case is an error.
    Historical projections are never imported into this prospective ledger.
    """
    now = timestamp(now or utc_now())
    automatic_enrollment = models is None
    models = tuple(live_models(now) if automatic_enrollment else models)
    if len({model.version for model in models}) != len(models):
        raise ValueError("Forecast model versions must be unique")
    required = {
        protocol_definition()["incumbent_version"],
        protocol_definition()["price_reference"],
    }
    if not required.issubset({model.version for model in models}):
        raise ValueError("Incumbent and unchanged-price reference must share every case")
    manifests = [canonical_json(model.manifest()) for model in models]
    snapshots = {}
    for stock in stocks:
        if isinstance(stock, dict) and (snapshot := _snapshot(stock)):
            snapshots[snapshot["symbol"]] = snapshot
    counts = Counter()
    with ledger_connection(data_dir, write=True) as conn:
        _register(conn, "forecast_protocols", PROTOCOL_VERSION, protocol_definition(), now)
        for model, manifest in zip(models, manifests):
            _register(conn, "forecast_models", model.version, json.loads(manifest), now)
        # Observe first so a late/replayed forecast can never enter a known outcome's cohort.
        counts["new_outcomes"] = _observe(conn, stocks, now)
        for projection in projections.values():
            if not isinstance(projection, dict):
                counts["skipped_invalid"] += 1
                continue
            symbol = normalize_ticker(projection.get("symbol"))
            snapshot = snapshots.get(symbol)
            if snapshot is None:
                counts["skipped_invalid"] += 1
                continue
            try:
                created, verified = _append_case(
                    conn,
                    projection,
                    snapshot,
                    models,
                    now,
                    automatic_enrollment=automatic_enrollment,
                )
            except ForecastConflict:
                raise
            except (TypeError, ValueError, OverflowError):
                counts["skipped_invalid"] += 1
                continue
            counts["new_cases" if created else "replayed_cases"] += 1
            if not verified:
                counts["unverified_input_cases"] += 1
        if [canonical_json(model.manifest()) for model in models] != manifests:
            raise ForecastConflict("Model parameters changed while generating predictions")
        counts["new_scores"] = _score(conn, now)
    return {
        "protocol": PROTOCOL_VERSION,
        **{
            key: counts[key]
            for key in (
                "new_cases",
                "replayed_cases",
                "skipped_invalid",
                "unverified_input_cases",
                "new_outcomes",
                "new_scores",
            )
        },
    }


def score_pending(*, data_dir: str | Path | None, now: datetime | None = None) -> int:
    now = timestamp(now or utc_now())
    with ledger_connection(data_dir, write=True) as conn:
        return _score(conn, now)


def ledger_snapshot(*, data_dir: str | Path | None) -> dict[str, list[dict]]:
    with ledger_connection(data_dir) as conn:
        if not schema_exists(conn):
            return {table: [] for table in TABLES}
        return {
            table: [dict(row) for row in conn.execute(f"SELECT * FROM {table}").fetchall()]
            for table in TABLES
        }


def outcome_symbols_due(*, data_dir: str | Path | None, now: datetime | None = None) -> list[str]:
    """Only request pc values that the provider can verify in today's session."""
    now = timestamp(now or utc_now())
    previous = previous_close_session_at(now)
    if previous is None:
        return []
    with ledger_connection(data_dir) as conn:
        if not schema_exists(conn):
            return []
        rows = conn.execute(
            """SELECT DISTINCT c.symbol FROM forecast_cases c
            WHERE c.target_session = ? AND c.calendar = 'XNYS' AND c.input_verified = 1
              AND NOT EXISTS (SELECT 1 FROM forecast_outcomes o
                  WHERE o.symbol = c.symbol AND o.session = c.target_session
                    AND o.calendar = c.calendar)
            ORDER BY c.symbol""",
            (previous.isoformat(),),
        ).fetchall()
    return [row["symbol"] for row in rows]

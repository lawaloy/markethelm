"""Durable storage for daily projections and summary snapshots (CSV/JSON removed)."""

from __future__ import annotations

import json
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Union

import pandas as pd

import src.storage.market_bars as market_bars
from src.utils.tickers import normalize_ticker

_PROJECTIONS_DDL = (
    """CREATE TABLE IF NOT EXISTS projections (
    run_date TEXT NOT NULL,
    symbol TEXT NOT NULL,
    name TEXT,
    current_price REAL,
    target_low REAL,
    target_mid REAL,
    target_high REAL,
    expected_change_percent REAL,
    recommendation TEXT,
    confidence REAL,
    trend TEXT,
    momentum_score REAL,
    volatility_score REAL,
    risk_level TEXT,
    reason TEXT,
    projection_date TEXT,
    projection_horizon_sessions INTEGER,
    projection_calendar TEXT,
    generated_at TEXT,
    source TEXT NOT NULL DEFAULT 'tracker',
    written_at TEXT NOT NULL,
    PRIMARY KEY (run_date, symbol)
)""",
    """CREATE INDEX IF NOT EXISTS idx_projections_symbol_date
    ON projections(symbol, run_date DESC)""",
    """CREATE INDEX IF NOT EXISTS idx_projections_date
    ON projections(run_date DESC)""",
)

_SUMMARIES_DDL = (
    """CREATE TABLE IF NOT EXISTS daily_summaries (
    summary_date TEXT NOT NULL PRIMARY KEY,
    ai_summary TEXT,
    analysis_json TEXT NOT NULL DEFAULT '{}',
    exchange_comparison_json TEXT NOT NULL DEFAULT '{}',
    projection_summary_json TEXT NOT NULL DEFAULT '{}',
    payload_json TEXT NOT NULL DEFAULT '{}',
    source TEXT NOT NULL DEFAULT 'tracker',
    written_at TEXT NOT NULL
)""",
    """CREATE INDEX IF NOT EXISTS idx_daily_summaries_date
    ON daily_summaries(summary_date DESC)""",
)

_UPSERT_PROJECTION_SQL = """
INSERT INTO projections (
    run_date, symbol, name, current_price, target_low, target_mid, target_high,
    expected_change_percent, recommendation, confidence, trend,
    momentum_score, volatility_score, risk_level, reason,
    projection_date, projection_horizon_sessions, projection_calendar,
    generated_at, source, written_at
) VALUES (
    ?, ?, ?, ?, ?, ?, ?,
    ?, ?, ?, ?,
    ?, ?, ?, ?,
    ?, ?, ?,
    ?, ?, ?
)
ON CONFLICT(run_date, symbol) DO UPDATE SET
    name = excluded.name,
    current_price = excluded.current_price,
    target_low = excluded.target_low,
    target_mid = excluded.target_mid,
    target_high = excluded.target_high,
    expected_change_percent = excluded.expected_change_percent,
    recommendation = excluded.recommendation,
    confidence = excluded.confidence,
    trend = excluded.trend,
    momentum_score = excluded.momentum_score,
    volatility_score = excluded.volatility_score,
    risk_level = excluded.risk_level,
    reason = excluded.reason,
    projection_date = excluded.projection_date,
    projection_horizon_sessions = excluded.projection_horizon_sessions,
    projection_calendar = excluded.projection_calendar,
    generated_at = excluded.generated_at,
    source = excluded.source,
    written_at = excluded.written_at
"""

_UPSERT_SUMMARY_SQL = """
INSERT INTO daily_summaries (
    summary_date, ai_summary, analysis_json, exchange_comparison_json,
    projection_summary_json, payload_json, source, written_at
) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
ON CONFLICT(summary_date) DO UPDATE SET
    ai_summary = excluded.ai_summary,
    analysis_json = excluded.analysis_json,
    exchange_comparison_json = excluded.exchange_comparison_json,
    projection_summary_json = excluded.projection_summary_json,
    payload_json = excluded.payload_json,
    source = excluded.source,
    written_at = excluded.written_at
"""


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _json_dumps(value: Any) -> str:
    return json.dumps(value if value is not None else {}, allow_nan=False, default=str)


def _json_loads(text: Any, default: Any = None) -> Any:
    if text is None or text == "":
        return {} if default is None else default
    if isinstance(text, (dict, list)):
        return text
    try:
        return json.loads(text)
    except (TypeError, json.JSONDecodeError):
        return {} if default is None else default


def ensure_projections_schema(conn: Any) -> None:
    market_bars.ensure_market_bars_schema(conn)
    for statement in _PROJECTIONS_DDL:
        conn.execute(statement)
    for statement in _SUMMARIES_DDL:
        conn.execute(statement)


def _row_from_projection(
    stock: Dict[str, Any],
    run_date: str,
    *,
    source: str,
    written_at: str,
) -> Optional[tuple]:
    symbol = normalize_ticker(stock.get("symbol"))
    if not symbol:
        return None
    horizon = stock.get("projection_horizon_sessions")
    try:
        horizon_n = int(horizon) if horizon is not None and horizon != "" else None
    except (TypeError, ValueError):
        horizon_n = None
    return (
        run_date,
        symbol,
        market_bars._optional_text(stock.get("name")) or symbol,
        market_bars._finite_or_none(stock.get("current_price")),
        market_bars._finite_or_none(stock.get("target_low")),
        market_bars._finite_or_none(stock.get("target_mid")),
        market_bars._finite_or_none(stock.get("target_high")),
        market_bars._finite_or_none(stock.get("expected_change_percent")),
        market_bars._optional_text(stock.get("recommendation")),
        market_bars._finite_or_none(stock.get("confidence")),
        market_bars._optional_text(stock.get("trend")),
        market_bars._finite_or_none(stock.get("momentum_score")),
        market_bars._finite_or_none(stock.get("volatility_score")),
        market_bars._optional_text(stock.get("risk_level")),
        market_bars._optional_text(stock.get("reason")),
        market_bars._as_trade_date(stock.get("projection_date"))
        or market_bars._optional_text(stock.get("projection_date")),
        horizon_n,
        market_bars._optional_text(stock.get("projection_calendar")),
        market_bars._optional_text(stock.get("generated_at")),
        source,
        written_at,
    )


def upsert_projections(
    projections: Union[Dict[str, Dict[str, Any]], Sequence[Dict[str, Any]]],
    run_date: date | datetime | str,
    *,
    data_dir: Optional[str | Path] = None,
    source: str = "tracker",
) -> int:
    """Upsert projection rows for one run date. Returns rows written."""
    day = market_bars._as_trade_date(run_date)
    if not day:
        raise ValueError(f"Invalid run_date for projections: {run_date!r}")
    if isinstance(projections, dict):
        rows_in: Sequence[Dict[str, Any]] = list(projections.values())
    else:
        rows_in = list(projections)
    written_at = _utc_now()
    rows: List[tuple] = []
    for item in rows_in:
        if not isinstance(item, dict):
            continue
        row = _row_from_projection(item, day, source=source, written_at=written_at)
        if row is not None:
            rows.append(row)
    if not rows:
        return 0
    with market_bars.market_bars_connection(data_dir=data_dir) as conn:
        ensure_projections_schema(conn)
        conn.executemany(_UPSERT_PROJECTION_SQL, rows)
    return len(rows)


def load_projections(
    run_date: date | datetime | str,
    *,
    data_dir: Optional[str | Path] = None,
) -> List[Dict[str, Any]]:
    day = market_bars._as_trade_date(run_date)
    if not day:
        return []
    with market_bars.market_bars_connection(data_dir=data_dir) as conn:
        ensure_projections_schema(conn)
        rows = conn.execute(
            """
            SELECT run_date, symbol, name, current_price, target_low, target_mid,
                   target_high, expected_change_percent, recommendation, confidence,
                   trend, momentum_score, volatility_score, risk_level, reason,
                   projection_date, projection_horizon_sessions, projection_calendar,
                   generated_at, source, written_at
            FROM projections
            WHERE run_date = ?
            ORDER BY symbol
            """,
            (day,),
        ).fetchall()
    return [dict(row) for row in rows]


def projections_frame(
    run_date: date | datetime | str,
    *,
    data_dir: Optional[str | Path] = None,
):
    rows = load_projections(run_date, data_dir=data_dir)
    if not rows:
        return pd.DataFrame()
    frame = pd.DataFrame(rows)
    drop_cols = [col for col in ("written_at", "source", "run_date") if col in frame.columns]
    if drop_cols:
        frame = frame.drop(columns=drop_cols)
    return frame


def list_projection_dates(
    *,
    data_dir: Optional[str | Path] = None,
    limit: int = 365,
) -> List[str]:
    try:
        limit_n = int(limit)
    except (TypeError, ValueError):
        limit_n = 365
    if limit_n <= 0:
        return []
    with market_bars.market_bars_connection(data_dir=data_dir) as conn:
        ensure_projections_schema(conn)
        rows = conn.execute(
            """
            SELECT DISTINCT run_date
            FROM projections
            ORDER BY run_date DESC
            LIMIT ?
            """,
            (limit_n,),
        ).fetchall()
    return [str(row["run_date"]) for row in rows]


def upsert_daily_summary(
    summary_data: Dict[str, Any],
    summary_date: date | datetime | str,
    *,
    data_dir: Optional[str | Path] = None,
    source: str = "tracker",
) -> str:
    """Persist one day's summary document. Returns ``summary:YYYY-MM-DD``."""
    day = market_bars._as_trade_date(summary_date)
    if not day:
        raise ValueError(f"Invalid summary_date: {summary_date!r}")
    payload = dict(summary_data)
    payload["date"] = day
    # Prefer structured columns; keep full payload for forward-compat.
    analysis = payload.get("analysis") if isinstance(payload.get("analysis"), dict) else {}
    exchange = (
        payload.get("exchange_comparison")
        if isinstance(payload.get("exchange_comparison"), dict)
        else {}
    )
    projection_summary = (
        payload.get("projection_summary")
        if isinstance(payload.get("projection_summary"), dict)
        else {}
    )
    ai_summary = payload.get("ai_summary")
    if ai_summary is not None and not isinstance(ai_summary, str):
        ai_summary = str(ai_summary)
    # Drop nested projections dict from stored payload once relational store owns it.
    stored = {k: v for k, v in payload.items() if k != "projections"}
    written_at = _utc_now()
    with market_bars.market_bars_connection(data_dir=data_dir) as conn:
        ensure_projections_schema(conn)
        conn.execute(
            _UPSERT_SUMMARY_SQL,
            (
                day,
                ai_summary,
                _json_dumps(analysis),
                _json_dumps(exchange),
                _json_dumps(projection_summary),
                _json_dumps(stored),
                source,
                written_at,
            ),
        )
    return f"summary:{day}"


def load_daily_summary(
    summary_date: date | datetime | str,
    *,
    data_dir: Optional[str | Path] = None,
) -> Optional[Dict[str, Any]]:
    day = market_bars._as_trade_date(summary_date)
    if not day:
        return None
    with market_bars.market_bars_connection(data_dir=data_dir) as conn:
        ensure_projections_schema(conn)
        row = conn.execute(
            """
            SELECT summary_date, ai_summary, analysis_json, exchange_comparison_json,
                   projection_summary_json, payload_json, source, written_at
            FROM daily_summaries
            WHERE summary_date = ?
            """,
            (day,),
        ).fetchone()
    if row is None:
        return None
    payload = _json_loads(row["payload_json"], default={})
    if not isinstance(payload, dict) or not payload:
        payload = {
            "date": day,
            "analysis": _json_loads(row["analysis_json"], default={}),
            "exchange_comparison": _json_loads(row["exchange_comparison_json"], default={}),
            "projection_summary": _json_loads(row["projection_summary_json"], default={}),
        }
        if row["ai_summary"] is not None:
            payload["ai_summary"] = row["ai_summary"]
    else:
        payload = dict(payload)
        payload.setdefault("date", day)
        if "analysis" not in payload:
            payload["analysis"] = _json_loads(row["analysis_json"], default={})
        if "exchange_comparison" not in payload:
            payload["exchange_comparison"] = _json_loads(
                row["exchange_comparison_json"], default={}
            )
        if "projection_summary" not in payload and row["projection_summary_json"]:
            payload["projection_summary"] = _json_loads(row["projection_summary_json"], default={})
        if row["ai_summary"] is not None and "ai_summary" not in payload:
            payload["ai_summary"] = row["ai_summary"]
    return payload


def list_summary_dates(
    *,
    data_dir: Optional[str | Path] = None,
    limit: int = 365,
) -> List[str]:
    try:
        limit_n = int(limit)
    except (TypeError, ValueError):
        limit_n = 365
    if limit_n <= 0:
        return []
    with market_bars.market_bars_connection(data_dir=data_dir) as conn:
        ensure_projections_schema(conn)
        rows = conn.execute(
            """
            SELECT summary_date
            FROM daily_summaries
            ORDER BY summary_date DESC
            LIMIT ?
            """,
            (limit_n,),
        ).fetchall()
    return [str(row["summary_date"]) for row in rows]

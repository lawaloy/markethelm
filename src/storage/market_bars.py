"""Durable market bar storage for daily quotes (CSV daily_data removed)."""

from __future__ import annotations

import math
import sqlite3
from contextlib import contextmanager
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional, Sequence

import pandas as pd

import src.storage.projections_store as projections_store
from src.core.logger import get_logger
from src.utils.tickers import normalize_ticker

from .database import database_enabled, get_connection, init_database

logger = get_logger(__name__)

_MARKET_BARS_DDL = (
    """CREATE TABLE IF NOT EXISTS market_bars (
    trade_date TEXT NOT NULL,
    symbol TEXT NOT NULL,
    name TEXT,
    open REAL,
    high REAL,
    low REAL,
    close REAL NOT NULL,
    volume REAL,
    previous_close REAL,
    change REAL,
    change_percent REAL,
    market_cap REAL,
    exchange TEXT,
    index_name TEXT,
    quote_timestamp TEXT,
    outcome_session TEXT,
    outcome_close REAL,
    outcome_final INTEGER,
    source TEXT NOT NULL DEFAULT 'fetch',
    written_at TEXT NOT NULL,
    PRIMARY KEY (trade_date, symbol)
)""",
    """CREATE INDEX IF NOT EXISTS idx_market_bars_symbol_date
    ON market_bars(symbol, trade_date DESC)""",
    """CREATE INDEX IF NOT EXISTS idx_market_bars_date
    ON market_bars(trade_date DESC)""",
)

_UPSERT_SQL = """
INSERT INTO market_bars (
    trade_date, symbol, name, open, high, low, close, volume,
    previous_close, change, change_percent, market_cap, exchange, index_name,
    quote_timestamp, outcome_session, outcome_close, outcome_final,
    source, written_at
) VALUES (
    ?, ?, ?, ?, ?, ?, ?, ?,
    ?, ?, ?, ?, ?, ?,
    ?, ?, ?, ?,
    ?, ?
)
ON CONFLICT(trade_date, symbol) DO UPDATE SET
    name = excluded.name,
    open = excluded.open,
    high = excluded.high,
    low = excluded.low,
    close = excluded.close,
    volume = excluded.volume,
    previous_close = excluded.previous_close,
    change = excluded.change,
    change_percent = excluded.change_percent,
    market_cap = excluded.market_cap,
    exchange = excluded.exchange,
    index_name = excluded.index_name,
    quote_timestamp = excluded.quote_timestamp,
    outcome_session = excluded.outcome_session,
    outcome_close = excluded.outcome_close,
    outcome_final = excluded.outcome_final,
    source = excluded.source,
    written_at = excluded.written_at
"""


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _finite_or_none(value: Any) -> Optional[float]:
    if value is None or value == "":
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(number):
        return None
    return number


def _as_trade_date(value: date | datetime | str | None) -> Optional[str]:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    text = str(value).strip()
    if not text:
        return None
    try:
        return date.fromisoformat(text[:10]).isoformat()
    except ValueError:
        return None


def _optional_text(value: Any) -> Optional[str]:
    if value is None:
        return None
    if isinstance(value, float) and not math.isfinite(value):
        return None
    text = str(value).strip()
    return text or None


def _optional_bool_int(value: Any) -> Optional[int]:
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        return 1 if value else 0
    if isinstance(value, (int, float)):
        if isinstance(value, float) and not math.isfinite(value):
            return None
        return 1 if value else 0
    text = str(value).strip().lower()
    if text in {"1", "true", "yes"}:
        return 1
    if text in {"0", "false", "no"}:
        return 0
    return None


def row_from_stock(
    stock: Dict[str, Any],
    trade_date: str,
    *,
    source: str = "fetch",
    written_at: Optional[str] = None,
) -> Optional[tuple]:
    """Normalize one stock dict into an upsert parameter tuple."""
    if not isinstance(stock, dict):
        return None
    symbol = normalize_ticker(stock.get("symbol"))
    close = _finite_or_none(stock.get("close", stock.get("price")))
    if not symbol or close is None:
        return None
    return (
        trade_date,
        symbol,
        _optional_text(stock.get("name")),
        _finite_or_none(stock.get("open")),
        _finite_or_none(stock.get("high")),
        _finite_or_none(stock.get("low")),
        close,
        _finite_or_none(stock.get("volume")),
        _finite_or_none(stock.get("previous_close", stock.get("pc"))),
        _finite_or_none(stock.get("change")),
        _finite_or_none(stock.get("change_percent")),
        _finite_or_none(stock.get("market_cap")),
        _optional_text(stock.get("exchange")),
        _optional_text(stock.get("index_name")),
        _optional_text(stock.get("quote_timestamp")),
        _optional_text(stock.get("outcome_session")),
        _finite_or_none(stock.get("outcome_close")),
        _optional_bool_int(stock.get("outcome_final")),
        _optional_text(source) or "fetch",
        written_at or _utc_now(),
    )


def ensure_market_bars_schema(conn: Any) -> None:
    for statement in _MARKET_BARS_DDL:
        conn.execute(statement)


def default_sidecar_path(data_dir: str | Path) -> Path:
    return Path(data_dir) / "market_bars.sqlite"


@contextmanager
def _sidecar_connection(data_dir: str | Path) -> Iterator[sqlite3.Connection]:
    path = default_sidecar_path(data_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), timeout=30)
    conn.row_factory = sqlite3.Row
    try:
        ensure_market_bars_schema(conn)
        # File-mode sidecar also hosts projections + daily_summaries.

        projections_store.ensure_projections_schema(conn)
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


@contextmanager
def market_bars_connection(data_dir: Optional[str | Path] = None) -> Iterator[Any]:
    """
    Prefer the configured app database; otherwise use DATA_DIR/market_bars.sqlite.

    Sidecar mode lets local/file deployments start accumulating durable bars
    without enabling multi-user auth.
    """
    if database_enabled():
        init_database()
        with get_connection() as conn:
            yield conn
        return
    if data_dir is None:
        raise RuntimeError(
            "market bars sidecar requires data_dir when MARKETHELM_DATABASE_URL is unset"
        )
    with _sidecar_connection(data_dir) as conn:
        yield conn


def upsert_market_bars(
    stocks: Sequence[Dict[str, Any]],
    trade_date: date | datetime | str,
    *,
    data_dir: Optional[str | Path] = None,
    source: str = "fetch",
) -> int:
    """
    Upsert daily bars. Returns the number of rows written.

    Soft-skips invalid rows. Raises on connection/SQL failures so callers can
    decide whether to treat persistence as best-effort.
    """
    day = _as_trade_date(trade_date)
    if not day:
        raise ValueError(f"Invalid trade_date for market bars: {trade_date!r}")
    written_at = _utc_now()
    rows: List[tuple] = []
    for stock in stocks:
        row = row_from_stock(stock, day, source=source, written_at=written_at)
        if row is not None:
            rows.append(row)
    if not rows:
        return 0

    with market_bars_connection(data_dir=data_dir) as conn:
        # Hosted DB gets the table from migration 6; still idempotent for safety.
        ensure_market_bars_schema(conn)
        conn.executemany(_UPSERT_SQL, rows)
    return len(rows)


def load_market_bars(
    trade_date: date | datetime | str,
    *,
    data_dir: Optional[str | Path] = None,
) -> List[Dict[str, Any]]:
    """Load all bars for a trade date (empty list when none)."""
    day = _as_trade_date(trade_date)
    if not day:
        return []
    with market_bars_connection(data_dir=data_dir) as conn:
        ensure_market_bars_schema(conn)
        rows = conn.execute(
            """
            SELECT trade_date, symbol, name, open, high, low, close, volume,
                   previous_close, change, change_percent, market_cap, exchange,
                   index_name, quote_timestamp, outcome_session, outcome_close,
                   outcome_final, source, written_at
            FROM market_bars
            WHERE trade_date = ?
            ORDER BY symbol
            """,
            (day,),
        ).fetchall()
    return [dict(row) for row in rows]


def market_bars_frame(
    trade_date: date | datetime | str,
    *,
    data_dir: Optional[str | Path] = None,
):
    """Return a pandas DataFrame of bars for ``trade_date`` (possibly empty)."""

    rows = load_market_bars(trade_date, data_dir=data_dir)
    if not rows:
        return pd.DataFrame()
    frame = pd.DataFrame(rows)
    # Drop storage-only columns that callers of the old CSV shape did not expect.
    drop_cols = [col for col in ("written_at", "source") if col in frame.columns]
    if drop_cols:
        frame = frame.drop(columns=drop_cols)
    return frame


def list_market_bar_dates(
    *,
    data_dir: Optional[str | Path] = None,
    limit: int = 365,
) -> List[str]:
    """Newest-first trade dates present in market_bars."""
    try:
        limit_n = int(limit)
    except (TypeError, ValueError):
        limit_n = 365
    if limit_n <= 0:
        return []
    with market_bars_connection(data_dir=data_dir) as conn:
        ensure_market_bars_schema(conn)
        rows = conn.execute(
            """
            SELECT DISTINCT trade_date
            FROM market_bars
            ORDER BY trade_date DESC
            LIMIT ?
            """,
            (limit_n,),
        ).fetchall()
    return [str(row["trade_date"]) for row in rows]


def latest_saved_quotes(
    *,
    data_dir: Optional[str | Path] = None,
    snapshot_limit: int = 7,
) -> Dict[str, Dict[str, Any]]:
    """Newest recorded quote per symbol from recent snapshots.

    Fetches can run on a weekend while Finnhub's quote still belongs to the
    preceding market session. Rank by the provider timestamp, not the storage
    date, and retain that timestamp so clients never present a saved quote as
    a live price. Limit the scan so abandoned symbols do not linger forever.
    """
    with market_bars_connection(data_dir=data_dir) as conn:
        ensure_market_bars_schema(conn)
        rows = conn.execute(
            """
            SELECT trade_date, symbol, close, quote_timestamp, written_at
            FROM market_bars
            WHERE trade_date IN (
                SELECT DISTINCT trade_date FROM market_bars
                ORDER BY trade_date DESC LIMIT ?
            )
            ORDER BY trade_date DESC
            """,
            (snapshot_limit,),
        ).fetchall()

    newest: Dict[str, Dict[str, Any]] = {}
    ranks: Dict[str, tuple[int, datetime, str, str]] = {}
    for row in rows:
        symbol = normalize_ticker(row["symbol"])
        price = _finite_or_none(row["close"])
        if not symbol or price is None:
            continue
        trade_date = str(row["trade_date"])
        quote_time = _optional_text(row["quote_timestamp"])
        try:
            quote_at = (
                datetime.fromisoformat(quote_time.replace("Z", "+00:00")) if quote_time else None
            )
            if quote_at is not None and quote_at.tzinfo is not None:
                quote_at = quote_at.astimezone(timezone.utc)
        except ValueError:
            quote_at = None
        has_provider_time = quote_at is not None and quote_at.tzinfo is not None
        if not has_provider_time:
            quote_time = None
            quote_at = datetime.fromisoformat(trade_date).replace(tzinfo=timezone.utc)
        written_at = _optional_text(row["written_at"]) or ""
        rank = (1 if has_provider_time else 0, quote_at, trade_date, written_at)
        if symbol in ranks and rank <= ranks[symbol]:
            continue
        ranks[symbol] = rank
        newest[symbol] = {"price": price, "as_of": quote_time or trade_date}
    return newest

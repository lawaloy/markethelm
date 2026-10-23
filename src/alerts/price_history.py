"""Load per-symbol close history for technical alert rules.

Preferred source is the live market-data provider (Finnhub daily candles).
Durable ``market_bars`` (hosted DB or ``DATA_DIR/market_bars.sqlite``) is the
local fallback. Legacy ``daily_data_*.csv`` snapshots are no longer read; import
them with ``scripts/backfill_market_data.py`` first.
"""

from __future__ import annotations

import math
import os
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence

from src.core.logger import get_logger
from src.services.api_client import FinnhubClient
from src.utils.tickers import normalize_ticker

logger = get_logger(__name__)

# RSI(14) needs 15 closes; Wilder smoothing benefits from extra warm-up bars.
# ~90 calendar days covers holidays/weekends while staying within free-tier cost.
DEFAULT_PROVIDER_LOOKBACK_DAYS = 90
DEFAULT_PROVIDER_MIN_BARS = 15


def resolve_market_data_dir(data_dir: Optional[str | Path] = None) -> Path:
    """Same resolution order as DataStorage: explicit → DATA_DIR → ./data."""
    if data_dir is not None:
        return Path(data_dir)
    return Path(os.getenv("DATA_DIR") or "data")


def _finite_close(raw) -> Optional[float]:
    if raw is None or raw == "":
        return None
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(value):
        return None
    return value


def closes_from_candle_payload(payload: object) -> List[float]:
    """Extract chronological closes from a Finnhub-style candle response."""
    if not isinstance(payload, dict):
        return []
    status = str(payload.get("s") or "ok").lower()
    if status != "ok":
        return []
    raw_closes = payload.get("c")
    raw_times = payload.get("t")
    if not isinstance(raw_closes, list) or not raw_closes:
        return []

    if isinstance(raw_times, list) and len(raw_times) == len(raw_closes):
        paired: List[tuple[float, float]] = []
        for ts, close in zip(raw_times, raw_closes):
            value = _finite_close(close)
            if value is None:
                continue
            try:
                stamp = float(ts)
            except (TypeError, ValueError):
                continue
            if not math.isfinite(stamp):
                continue
            paired.append((stamp, value))
        paired.sort(key=lambda item: item[0])
        return [value for _ts, value in paired]

    closes: List[float] = []
    for close in raw_closes:
        value = _finite_close(close)
        if value is not None:
            closes.append(value)
    return closes


def load_symbol_closes_from_provider(
    symbol: str,
    *,
    days: int = DEFAULT_PROVIDER_LOOKBACK_DAYS,
    client=None,
) -> List[float]:
    """Fetch daily closes from Finnhub candles. Empty list on any soft failure."""
    ticker = normalize_ticker(symbol)
    if not ticker:
        return []
    try:
        lookback = int(days)
    except (TypeError, ValueError):
        lookback = DEFAULT_PROVIDER_LOOKBACK_DAYS
    if lookback < DEFAULT_PROVIDER_MIN_BARS:
        lookback = DEFAULT_PROVIDER_LOOKBACK_DAYS

    try:
        if client is None:
            client = FinnhubClient()
        payload = client.get_candle_data(ticker, resolution="D", days=lookback)
    except (OSError, ValueError) as exc:
        logger.info("Provider candle history unavailable for %s: %s", ticker, exc)
        return []

    closes = closes_from_candle_payload(payload)
    if len(closes) < DEFAULT_PROVIDER_MIN_BARS:
        logger.info(
            "Provider candle history for %s has only %d usable closes",
            ticker,
            len(closes),
        )
    return closes


def load_symbol_closes_from_market_bars(
    symbol: str,
    *,
    data_dir: Optional[str | Path] = None,
    max_dates: int = 120,
) -> List[float]:
    """
    Chronological closes for ``symbol`` from durable ``market_bars``.

    Uses the hosted app database when ``MARKETHELM_DATABASE_URL`` is set,
    otherwise the sidecar at ``DATA_DIR/market_bars.sqlite``. Returns an empty
    list on any soft failure so RSI evaluation can degrade gracefully.
    """
    ticker = normalize_ticker(symbol)
    if not ticker:
        return []
    try:
        limit_n = int(max_dates)
    except (TypeError, ValueError):
        limit_n = 120
    if limit_n <= 0:
        return []

    root = resolve_market_data_dir(data_dir)
    try:
        from src.storage.market_bars import (
            ensure_market_bars_schema,
            market_bars_connection,
        )

        with market_bars_connection(data_dir=root) as conn:
            ensure_market_bars_schema(conn)
            rows = conn.execute(
                """
                SELECT close
                FROM market_bars
                WHERE symbol = ?
                ORDER BY trade_date DESC
                LIMIT ?
                """,
                (ticker, limit_n),
            ).fetchall()
    except Exception as exc:
        logger.info("market_bars close history unavailable for %s: %s", ticker, exc)
        return []

    closes: List[float] = []
    for row in reversed(rows):
        raw = row["close"] if not isinstance(row, (tuple, list)) else row[0]
        value = _finite_close(raw)
        if value is not None:
            closes.append(value)
    return closes


def load_symbol_closes_from_local(
    symbol: str,
    *,
    data_dir: Optional[str | Path] = None,
    max_files: int = 120,
) -> List[float]:
    """Closes from durable ``market_bars`` (empty list when none are saved)."""
    return load_symbol_closes_from_market_bars(symbol, data_dir=data_dir, max_dates=max_files)


def load_symbol_closes(
    symbol: str,
    *,
    data_dir: Optional[str | Path] = None,
    max_files: int = 120,
    prefer_provider: bool = True,
    client=None,
) -> List[float]:
    """
    Chronological closes for technical rules.

    Tries the live provider first (when ``prefer_provider``), then durable
    ``market_bars``. Returns an empty list when neither has data.
    """
    if prefer_provider:
        provider_closes = load_symbol_closes_from_provider(symbol, client=client)
        if len(provider_closes) >= DEFAULT_PROVIDER_MIN_BARS:
            return provider_closes
        if provider_closes:
            # Partial provider series — still prefer it when longer than local.
            local_closes = load_symbol_closes_from_local(
                symbol, data_dir=data_dir, max_files=max_files
            )
            return provider_closes if len(provider_closes) >= len(local_closes) else local_closes

    local_closes = load_symbol_closes_from_local(symbol, data_dir=data_dir, max_files=max_files)
    if not local_closes:
        logger.warning(
            "No close history for %s: provider candles and market_bars are both empty; "
            "RSI rules for this symbol cannot trigger until quotes are fetched.",
            symbol,
        )
    return local_closes


def merge_latest_close(
    closes: Sequence[float],
    latest: Optional[float],
) -> List[float]:
    """
    Append or replace the final close with a live/snapshot quote.

    Keeps RSI evaluation aligned with the price used for price_threshold rules
    on the same tick without inventing a new session file.
    """
    series = [float(value) for value in closes]
    if latest is None:
        return series
    try:
        value = float(latest)
    except (TypeError, ValueError):
        return series
    if not math.isfinite(value):
        return series
    if series and series[-1] == value:
        return series
    if series:
        # Same trading session: replace last bar with the fresher quote.
        return [*series[:-1], value]
    return [value]


def closes_by_symbol(
    symbols: Iterable[str],
    stocks: Sequence[dict],
    *,
    data_dir: Optional[str | Path] = None,
    prefer_provider: bool = True,
    client=None,
) -> Dict[str, List[float]]:
    """Load history with one shared provider client and overlay snapshot closes."""
    latest_by_symbol: Dict[str, float] = {}
    for stock in stocks:
        if not isinstance(stock, dict):
            continue
        ticker = normalize_ticker(stock.get("symbol"))
        if not ticker:
            continue
        close = _finite_close(stock.get("close", stock.get("price")))
        if close is not None:
            latest_by_symbol[ticker] = close

    result: Dict[str, List[float]] = {}
    for raw_symbol in symbols:
        ticker = normalize_ticker(raw_symbol)
        if not ticker or ticker in result:
            continue
        if prefer_provider and client is None:
            try:
                client = FinnhubClient()
            except (OSError, ValueError) as exc:
                logger.info("Provider candle history unavailable: %s", exc)
                prefer_provider = False
        history = load_symbol_closes(
            ticker,
            data_dir=data_dir,
            prefer_provider=prefer_provider,
            client=client,
        )
        result[ticker] = merge_latest_close(history, latest_by_symbol.get(ticker))
    return result

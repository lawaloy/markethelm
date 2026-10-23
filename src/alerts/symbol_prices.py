"""Resolve latest prices for alert symbol pickers."""

from __future__ import annotations

import math
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Dict, List

from src.core.logger import get_logger
from src.storage.market_bars import latest_saved_quotes
from src.utils.tickers import normalize_ticker

logger = get_logger(__name__)

_MAX_LIVE_FETCH = 15
_MAX_WORKERS = 4


def _load_env() -> None:
    try:
        from src.cli.alerts_commands import _load_env as cli_load_env

        cli_load_env()
    except Exception:
        pass


def saved_quote_details() -> Dict[str, Dict[str, object]]:
    """Saved quotes with their provider time, in file or hosted DB mode."""
    from dashboard.backend.services.data_loader import get_data_loader

    try:
        loader = get_data_loader()
        data_dir = loader.data_dir
        if not isinstance(data_dir, (str, Path)):
            return {}
        return latest_saved_quotes(data_dir=data_dir)
    except (ValueError, OSError, RuntimeError):
        # Unreadable data dirs / loader boot failures must not crash quote pickers.
        return {}


def prices_from_saved_daily_data() -> Dict[str, float]:
    """Prices from the newest recorded quote for each recently saved symbol."""
    return {symbol: float(row["price"]) for symbol, row in saved_quote_details().items()}


def resolve_symbol_prices(
    symbols: List[str],
    *,
    fetch_missing: bool = True,
) -> Dict[str, float]:
    """
    Return prices for the requested symbols.
    Uses saved daily data first, then optional live Finnhub quotes for gaps.
    """
    # Strip surrounding whitespace so " AAPL " matches saved "AAPL".
    normalized = list(
        dict.fromkeys(key for key in (normalize_ticker(symbol) for symbol in symbols) if key)
    )
    if not normalized:
        return {}

    saved = prices_from_saved_daily_data()
    prices: Dict[str, float] = {symbol: saved[symbol] for symbol in normalized if symbol in saved}
    if not fetch_missing:
        return prices

    missing = [symbol for symbol in normalized if symbol not in prices][:_MAX_LIVE_FETCH]
    if not missing:
        return prices

    _load_env()
    try:
        from src.services.data_fetcher import StockDataFetcher

        fetcher = StockDataFetcher(include_profile=False)
    except Exception as exc:
        logger.warning("Live quote fetch unavailable: %s", exc)
        return prices

    with ThreadPoolExecutor(max_workers=_MAX_WORKERS) as pool:
        futures = {pool.submit(_fetch_one_quote, fetcher, symbol): symbol for symbol in missing}
        for future in as_completed(futures):
            symbol, close = future.result()
            if close is not None:
                prices[symbol] = close

    return prices


def _fetch_one_quote(fetcher, symbol: str) -> tuple[str, float | None]:
    try:
        row = fetcher.fetch_symbol_data(symbol)
    except Exception as exc:
        logger.debug("Quote fetch failed for %s: %s", symbol, exc)
        return symbol, None
    if not row:
        return symbol, None
    close = row.get("close", row.get("price"))
    if close is None:
        return symbol, None
    try:
        value = float(close)
    except (TypeError, ValueError):
        return symbol, None
    if not math.isfinite(value):
        return symbol, None
    return symbol, value

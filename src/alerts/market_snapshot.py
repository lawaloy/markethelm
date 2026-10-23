"""Load shared market prices once per evaluation tick."""

from __future__ import annotations

import math
from typing import Any, Dict, List, Optional, Tuple

from src.alerts.alert_runner import _fetch_missing_watch_quotes, _load_env, _stocks_from_daily_df
from src.core.logger import get_logger
from src.utils.tickers import normalize_ticker

logger = get_logger(__name__)


def load_market_snapshot(
    watch_symbols: Optional[List[str]] = None,
    *,
    fetch_missing_quotes: bool = True,
) -> Tuple[Optional[str], Dict[str, float], List[Dict[str, Any]]]:
    """
    Return (last_data_date, symbol->close prices, stock rows).
    Prices include saved daily data plus optional live quotes for missing symbols.
    """
    _load_env()
    last_date: Optional[str] = None
    stocks: List[Dict[str, Any]] = []

    try:
        from dashboard.backend.services.data_loader import get_data_loader

        loader = get_data_loader()
        last_date = loader.get_latest_date()
        stocks = _stocks_from_daily_df(loader.load_daily_data())
    except (ValueError, OSError, RuntimeError) as exc:
        # Missing data is ValueError; unreadable/corrupt storage or loader boot
        # must not crash scheduled/hosted alert checks.
        logger.warning("Market snapshot loader unavailable: %s", exc)
        stocks = []

    # Strip/reject blank and sentinel tickers so " AAPL " matches saved AAPL
    # and float('nan') does not become a fake NAN watch fetch.
    symbols = list(
        dict.fromkeys(key for key in (normalize_ticker(s) for s in (watch_symbols or [])) if key)
    )
    if fetch_missing_quotes and symbols:
        stocks = _fetch_missing_watch_quotes(stocks, symbols)

    prices: Dict[str, float] = {}
    for stock in stocks:
        symbol = normalize_ticker(stock.get("symbol"))
        close = stock.get("close", stock.get("price"))
        if not symbol or close is None:
            continue
        try:
            value = float(close)
        except (TypeError, ValueError):
            continue
        # Defense in depth: keep snapshot prices JSON-safe for alert jobs.
        if not math.isfinite(value):
            continue
        prices[symbol] = value

    return last_date, prices, stocks

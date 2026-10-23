"""Evaluate configured alerts against the latest saved daily market data."""

from __future__ import annotations

import math
from typing import Any, Dict, List

from src.alerts.alert_engine import AlertEngine
from src.alerts.alert_paths import get_enabled_watch_symbols
from src.core.logger import get_logger
from src.utils.tickers import normalize_ticker

logger = get_logger(__name__)

# Match Settings / resolve_symbol_prices live-fetch budget so a large watch
# list cannot burn the shared Finnhub quota on every evaluate tick.
_MAX_LIVE_WATCH_FETCH = 15


def _load_env() -> None:
    try:
        from src.cli.alerts_commands import _load_env as cli_load_env

        cli_load_env()
    except Exception:
        pass


def _stocks_from_daily_df(df) -> List[Dict[str, Any]]:
    stocks: List[Dict[str, Any]] = []
    for _, row in df.iterrows():
        # Drop pandas None/NaN sentinels that stringify into fake tickers (NONE/NAN).
        symbol = normalize_ticker(row.get("symbol"))
        close = row.get("close", row.get("price"))
        if not symbol or close is None:
            continue
        try:
            close_value = float(close)
        except (TypeError, ValueError):
            logger.warning("Skipping invalid saved quote for %s: %r", symbol, close)
            continue
        # float("inf") is not NaN; reject all non-finite closes.
        if not math.isfinite(close_value):
            logger.warning("Skipping invalid saved quote for %s: %r", symbol, close)
            continue
        stocks.append({"symbol": symbol, "close": close_value})
    return stocks


def _fetch_missing_watch_quotes(
    stocks: List[Dict[str, Any]], watch_symbols: List[str]
) -> List[Dict[str, Any]]:
    """Fetch live quotes for watch symbols missing from saved daily data."""
    if not watch_symbols:
        return stocks

    present = {key for key in (normalize_ticker(stock.get("symbol")) for stock in stocks) if key}
    missing = [
        key
        for key in (normalize_ticker(symbol) for symbol in watch_symbols)
        if key and key not in present
    ]
    # Preserve caller order while deduping normalized tickers.
    missing = list(dict.fromkeys(missing))
    if not missing:
        return stocks
    if len(missing) > _MAX_LIVE_WATCH_FETCH:
        logger.warning(
            "Capping live watch quote fetches from %d to %d",
            len(missing),
            _MAX_LIVE_WATCH_FETCH,
        )
        missing = missing[:_MAX_LIVE_WATCH_FETCH]

    try:
        from src.services.data_fetcher import StockDataFetcher

        fetcher = StockDataFetcher(include_profile=False)
    except Exception as exc:
        logger.warning("Could not fetch watch symbols (API unavailable): %s", exc)
        return stocks

    enriched = list(stocks)
    for symbol in missing:
        try:
            row = fetcher.fetch_symbol_data(symbol)
        except Exception as exc:
            logger.warning("Failed to fetch quote for watch symbol %s: %s", symbol, exc)
            continue
        if not row:
            continue
        close = row.get("close", row.get("price"))
        if close is None:
            continue
        try:
            close_value = float(close)
        except (TypeError, ValueError):
            logger.warning("Skipping invalid quote for watch symbol %s: %r", symbol, close)
            continue
        if not math.isfinite(close_value):
            logger.warning("Skipping invalid quote for watch symbol %s: %r", symbol, close)
            continue
        enriched.append({"symbol": symbol, "close": close_value})
        logger.info("Fetched live quote for watch symbol %s", symbol)

    return enriched


def evaluate_alerts_from_latest_data(*, fetch_missing_quotes: bool = True) -> Dict[str, Any]:
    """
    Run all enabled watches against the newest market_bars snapshot.
    When fetch_missing_quotes is True, also pulls live Finnhub quotes for
    watch symbols that are not in the saved CSV (common with REFRESH_TOP_N=10).
    """
    _load_env()
    engine = AlertEngine.from_config()
    if not engine:
        return {
            "triggered": 0,
            "events": [],
            "last_data_date": None,
            "message": "No active watches configured.",
        }

    from dashboard.backend.services.data_loader import get_data_loader

    last_date: str | None = None
    stocks: List[Dict[str, Any]] = []
    try:
        loader = get_data_loader()
        last_date = loader.get_latest_date()
        stocks = _stocks_from_daily_df(loader.load_daily_data())
    except ValueError:
        stocks = []

    watch_symbols = get_enabled_watch_symbols()
    if fetch_missing_quotes and watch_symbols:
        stocks = _fetch_missing_watch_quotes(stocks, watch_symbols)

    if not stocks:
        return {
            "triggered": 0,
            "events": [],
            "last_data_date": last_date,
            "message": "No market data available.",
        }

    events = engine.evaluate(stocks)
    return {
        "triggered": len(events),
        "events": events,
        "last_data_date": last_date,
        "message": None if events else "No alerts triggered on latest data.",
    }

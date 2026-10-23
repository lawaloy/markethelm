"""Shared helper to seed durable daily bars / projections / summaries in tests."""

from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import Any, Dict, Sequence, Union

from src.storage.market_bars import upsert_market_bars
from src.storage.projections_store import upsert_daily_summary, upsert_projections


def seed_daily_bars(
    data_dir: Union[str, Path],
    trade_date: Union[str, date],
    rows: Sequence[Dict[str, Any]],
) -> int:
    """Upsert quote rows for one trade date into market_bars under ``data_dir``."""
    return upsert_market_bars(list(rows), trade_date, data_dir=data_dir, source="test")


def seed_simple_bars(
    data_dir: Union[str, Path],
    trade_date: Union[str, date],
    *,
    symbol: str = "AAPL",
    close: float = 100.0,
    **extra: Any,
) -> int:
    row: Dict[str, Any] = {"symbol": symbol, "close": close, **extra}
    return seed_daily_bars(data_dir, trade_date, [row])


def seed_projections(
    data_dir: Union[str, Path],
    run_date: Union[str, date],
    rows: Sequence[Dict[str, Any]],
) -> int:
    """Upsert projection rows for one run date into market_bars under ``data_dir``."""

    return upsert_projections(list(rows), run_date, data_dir=data_dir, source="test")


def seed_summary(
    data_dir: Union[str, Path],
    summary_date: Union[str, date],
    summary_data: Dict[str, Any],
) -> str:
    """Persist one daily summary into market_bars under ``data_dir``."""

    return upsert_daily_summary(dict(summary_data), summary_date, data_dir=data_dir, source="test")

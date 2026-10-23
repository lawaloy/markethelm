"""
Shared company name resolution (pytickersymbols).
Used when saving data so names are stored at write time.
"""

from __future__ import annotations

import math
from typing import Any, Dict, Optional

from src.utils.tickers import normalize_ticker

try:
    import pytickersymbols
except ImportError:
    pytickersymbols = None

# Cached symbol->name lookup - built once, reused
_name_cache: Dict[str, str] = {}

# str(NaN)/None/NA leaks that must not become display names.
_INVALID_NAME_SENTINELS = frozenset(
    {"", "nan", "<na>", "none", "nat", "null", "inf", "-inf", "infinity", "-infinity"}
)


def _clean_display_name(raw: Any) -> Optional[str]:
    """Return a usable company name, or None for blank / non-finite / sentinel values."""
    if raw is None:
        return None
    if isinstance(raw, float) and not math.isfinite(raw):
        return None
    try:
        text = str(raw).strip()
    except Exception:
        return None
    if not text or text.lower() in _INVALID_NAME_SENTINELS:
        return None
    return text


def resolve_company_name(symbol: str, fallback: str = "") -> str:
    """
    Resolve company name when API returns symbol. Uses pytickersymbols.
    Returns real name for S&P 500, NASDAQ 100, Dow Jones symbols.
    """
    key = normalize_ticker(symbol)
    if key is None:
        return _clean_display_name(fallback) or ""

    # Prefer a real display name over the ticker itself (any case/padding).
    fb = _clean_display_name(fallback)
    if fb and normalize_ticker(fb) != key:
        return fb

    if key in _name_cache:
        return _name_cache[key]
    if pytickersymbols is None:
        _name_cache[key] = key
        return key
    try:
        data = pytickersymbols.PyTickerSymbols()
        for index_name in ["S&P 500", "NASDAQ 100", "Dow Jones"]:
            try:
                for s in data.get_stocks_by_index(index_name):
                    if normalize_ticker(s.get("symbol")) != key:
                        continue
                    # Catalog NaN/Inf/"nan" is truthy in Python — clean before cache/return.
                    cleaned = _clean_display_name(s.get("name"))
                    if cleaned:
                        _name_cache[key] = cleaned
                        return cleaned
            except Exception:
                continue
    except Exception:
        pass
    _name_cache[key] = key
    return key


def enrich_stock_data_with_names(data: list) -> list:
    """
    Enrich each stock dict with resolved company name when name is missing or equals symbol.
    Modifies in place and returns the same list.
    """
    for row in data:
        # Skip poison / hand-edited non-dict rows so save_daily_data can continue.
        if not isinstance(row, dict):
            continue
        key = normalize_ticker(row.get("symbol"))
        if key is None:
            continue
        # Canonicalize stored symbol so downstream joins stay clean.
        row["symbol"] = key
        cleaned = _clean_display_name(row.get("name", key))
        name_as_ticker = normalize_ticker(cleaned) == key if cleaned else True
        if not cleaned or name_as_ticker:
            row["name"] = resolve_company_name(key, cleaned or key)
        else:
            row["name"] = cleaned
    return data

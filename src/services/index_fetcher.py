"""
MarketHelm - Index Fetcher Module

Fetches all stocks from major market indices using Python packages.
Uses pytickersymbols package for reliable, maintained index lists.
"""

import json
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, List, Optional

from ..core.logger import get_logger
from ..utils.tickers import normalize_ticker

# Try to import pytickersymbols package
try:
    from pytickersymbols import PyTickerSymbols

    PYTICKERSYMBOLS_AVAILABLE = True
except ImportError:
    PyTickerSymbols = None
    PYTICKERSYMBOLS_AVAILABLE = False

logger = get_logger(__name__)


class IndexFetcher:
    """Fetches all stocks from major market indices."""

    def __init__(self, cache_dir: str = "data/cache"):
        """
        Initialize index fetcher with caching.
        Uses pytickersymbols package for index constituents.

        Args:
            cache_dir: Directory to store cached index lists
        """
        self.cache_dir = Path(cache_dir)
        try:
            self.cache_dir.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            # Read-only / permission failures must not abort IndexFetcher construction;
            # cache load/save already soft-fail when the directory is unusable.
            logger.warning("Index cache directory unavailable (%s): %s", self.cache_dir, exc)
        self.cache_duration = timedelta(days=7)  # Cache for 7 days

        # Initialize pytickersymbols package
        if PYTICKERSYMBOLS_AVAILABLE:
            self.ticker_symbols = PyTickerSymbols()
            self.package_available = True
            logger.debug("pytickersymbols package loaded successfully")
        else:
            self.ticker_symbols = None
            self.package_available = False
            logger.warning(
                "pytickersymbols package not found. Install with: pip install pytickersymbols"
            )

    def _get_minimal_fallback(self, index_name: str) -> List[str]:
        """Get minimal fallback list (only used if package and Wikipedia both fail)."""
        fallbacks = {
            "S&P 500": [
                "AAPL",
                "MSFT",
                "AMZN",
                "NVDA",
                "GOOGL",
                "META",
                "TSLA",
                "BRK.B",
                "V",
                "UNH",
            ],
            "NASDAQ-100": [
                "AAPL",
                "MSFT",
                "AMZN",
                "NVDA",
                "GOOGL",
                "META",
                "TSLA",
                "AVGO",
                "COST",
                "NFLX",
            ],
            "Dow Jones": ["AAPL", "MSFT", "UNH", "GS", "HD", "CAT", "MCD", "V", "HON", "TRV"],
        }
        return fallbacks.get(index_name, [])

    @staticmethod
    def _normalize_symbol_list(raw_symbols: List[Any]) -> List[str]:
        """Strip/uppercase tickers, drop sentinels/blanks, and dedupe preserving order."""
        normalized: List[str] = []
        seen = set()
        for raw in raw_symbols or []:
            key = normalize_ticker(raw)
            if key is None or key in seen:
                continue
            seen.add(key)
            normalized.append(key)
        return normalized

    def _load_from_cache(self, index_name: str) -> Optional[List[str]]:
        """Load index symbols from cache if available and fresh."""
        cache_file = (
            self.cache_dir
            / f"{index_name.replace(' ', '_').replace('&', '').replace('-', '_')}_symbols.json"
        )

        if cache_file.exists():
            try:
                with open(cache_file, "r") as f:
                    cache_data = json.load(f)
                if not isinstance(cache_data, dict):
                    return None
                cached_date = datetime.fromisoformat(cache_data["date"])

                if datetime.now() - cached_date < self.cache_duration:
                    raw_symbols = cache_data.get("symbols") or []
                    # String "AAPL" would normalize to ['A','P','L'] and poison
                    # Finnhub traffic — treat wrong-type cache as a miss.
                    if not isinstance(raw_symbols, list):
                        return None
                    symbols = self._normalize_symbol_list(raw_symbols)
                    logger.debug(f"Loaded {index_name} symbols from cache ({len(symbols)} symbols)")
                    return symbols
            except Exception as e:
                logger.debug(f"Failed to load cache: {e}")

        return None

    def _save_to_cache(self, index_name: str, symbols: List[str]):
        """Save index symbols to cache (atomic replace so crashes cannot truncate)."""
        cache_file = (
            self.cache_dir
            / f"{index_name.replace(' ', '_').replace('&', '').replace('-', '_')}_symbols.json"
        )
        tmp_file = cache_file.with_suffix(cache_file.suffix + ".tmp")

        try:
            cache_data = {
                "date": datetime.now().isoformat(),
                "symbols": self._normalize_symbol_list(symbols),
            }
            # Write to a sibling temp file first. Opening cache_file with "w"
            # would truncate a valid cache before json.dump finishes — a crash
            # or serialize error would then force tiny fallback symbol lists.
            with open(tmp_file, "w", encoding="utf-8") as f:
                json.dump(cache_data, f)
                f.flush()
            tmp_file.replace(cache_file)
            logger.debug(f"Cached {index_name} symbols ({len(cache_data['symbols'])} symbols)")
        except Exception as e:
            logger.warning(f"Failed to save cache: {e}")
            try:
                if tmp_file.exists():
                    tmp_file.unlink()
            except OSError:
                pass

    # Wikipedia scraping removed - we use pytickersymbols package only

    def get_sp500_symbols(self) -> List[str]:
        """
        Get all S&P 500 stock symbols using pytickersymbols package.
        """
        # Try cache first
        cached = self._load_from_cache("S&P 500")
        if cached:
            return cached

        # Use pytickersymbols package
        if self.package_available:
            try:
                logger.info("Fetching S&P 500 symbols from pytickersymbols package...")
                stocks = list(self.ticker_symbols.get_stocks_by_index("S&P 500"))
                symbols = self._normalize_symbol_list([stock.get("symbol") for stock in stocks])

                if symbols and len(symbols) > 400:  # Sanity check
                    self._save_to_cache("S&P 500", symbols)
                    logger.info(f"Fetched {len(symbols)} S&P 500 symbols from package")
                    return symbols
                else:
                    logger.warning(f"Package returned unexpected data: {len(symbols)} symbols")
            except Exception as e:
                logger.warning(f"Failed to fetch from package: {e}")

        # Last resort: minimal fallback (Wikipedia scraping was removed)
        logger.warning("Using minimal fallback list - package unavailable or returned bad data")
        return self._get_minimal_fallback("S&P 500")

    def get_nasdaq100_symbols(self) -> List[str]:
        """Get all NASDAQ-100 stock symbols using pytickersymbols package."""
        cached = self._load_from_cache("NASDAQ-100")
        if cached:
            return cached

        # Use pytickersymbols package
        if self.package_available:
            try:
                logger.info("Fetching NASDAQ-100 symbols from pytickersymbols package...")
                stocks = list(self.ticker_symbols.get_stocks_by_index("NASDAQ 100"))
                symbols = self._normalize_symbol_list([stock.get("symbol") for stock in stocks])

                if symbols and len(symbols) > 90:
                    self._save_to_cache("NASDAQ-100", symbols)
                    logger.info(f"Fetched {len(symbols)} NASDAQ-100 symbols from package")
                    return symbols
            except Exception as e:
                logger.warning(f"Failed to fetch from package: {e}")

        logger.warning("Using minimal fallback list - package unavailable or returned bad data")
        return self._get_minimal_fallback("NASDAQ-100")

    def get_dow30_symbols(self) -> List[str]:
        """Get all Dow Jones Industrial Average (30 stocks) symbols via pytickersymbols."""
        cached = self._load_from_cache("Dow Jones")
        if cached:
            return cached

        # Use pytickersymbols package
        if self.package_available:
            try:
                logger.info("Fetching Dow 30 symbols from pytickersymbols package...")
                # Try different index name variations
                symbols: List[str] = []
                for index_name_variant in ["DOW JONES", "Dow Jones", "DJIA"]:
                    try:
                        stocks = list(self.ticker_symbols.get_stocks_by_index(index_name_variant))
                        symbols = self._normalize_symbol_list(
                            [stock.get("symbol") for stock in stocks]
                        )
                        if symbols and len(symbols) >= 30:
                            break
                    except Exception:
                        continue

                if symbols and len(symbols) >= 30:
                    self._save_to_cache("Dow Jones", symbols)
                    logger.info(f"Fetched {len(symbols)} Dow 30 symbols from package")
                    return symbols
            except Exception as e:
                logger.warning(f"Failed to fetch from package: {e}")

        logger.warning("Using minimal fallback list - package unavailable or returned bad data")
        return self._get_minimal_fallback("Dow Jones")

    def get_index_symbols(self, index_name: str) -> List[str]:
        """
        Get all symbols for a given index.

        Args:
            index_name: Name of the index (S&P 500, NASDAQ-100, Dow Jones)

        Returns:
            List of stock symbols
        """
        index_name_upper = index_name.upper()

        if "S&P" in index_name_upper or "SP500" in index_name_upper or "SP 500" in index_name_upper:
            return self.get_sp500_symbols()
        elif "NASDAQ" in index_name_upper and "100" in index_name_upper:
            return self.get_nasdaq100_symbols()
        elif "DOW" in index_name_upper or "DJIA" in index_name_upper:
            return self.get_dow30_symbols()
        else:
            logger.warning(f"Unknown index: {index_name}")
            return []

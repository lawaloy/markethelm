"""
MarketHelm - Data Fetcher Module

Fetches daily stock data using official APIs (Finnhub).
Professional implementation with proper rate limiting and error handling.
"""

import json
import os
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, List, Optional

import src.services.stock_screener as stock_screener

from ..core.config import get_indices_to_track
from ..core.logger import get_logger
from .api_client import FinnhubClient
from .index_fetcher import IndexFetcher

logger = get_logger(__name__)


class StockDataFetcher:
    """Fetches and processes stock market data using official APIs."""

    def __init__(self, api_client: Optional[FinnhubClient] = None, include_profile: bool = True):
        """
        Initialize data fetcher.

        Args:
            api_client: Optional FinnhubClient instance (creates new one if not provided)
            include_profile: Whether to fetch company profile data (name, market cap)
        """
        self.today = datetime.now().date()
        self.yesterday = self.today - timedelta(days=1)
        self.include_profile = include_profile

        try:
            self.api_client = api_client or FinnhubClient()
            logger.info("Using Finnhub API for stock data")
        except ValueError as e:
            logger.error(f"Failed to initialize API client: {e}")
            logger.error("Please set FINNHUB_API_KEY environment variable")
            logger.error("Get free API key at: https://finnhub.io/register")
            raise

    def fetch_symbol_data(
        self, symbol: str, period: str = "5d", max_retries: int = 3
    ) -> Optional[Dict]:
        """
        Fetch data for a single symbol using official API.

        Args:
            symbol: Stock ticker symbol
            period: Time period (kept for compatibility, not used)
            max_retries: Maximum number of retry attempts

        Returns:
            Dictionary with stock data or None if fetch fails
        """
        for attempt in range(max_retries):
            try:
                data = self.api_client.get_stock_data(symbol, include_profile=self.include_profile)
                return data
            except Exception as e:
                if attempt < max_retries - 1:
                    wait_time = 0.5 * (attempt + 1)  # Exponential backoff
                    logger.debug(
                        f"Retry {attempt + 1}/{max_retries} for {symbol} after {wait_time}s..."
                    )
                    time.sleep(wait_time)
                    continue
                logger.warning(
                    f"Error fetching data for {symbol} after {max_retries} attempts: {e}"
                )
                return None

    # Legacy method removed - use fetch_all_indices() instead

    def fetch_all_indices(
        self, use_screener: bool = True, max_symbols_per_index: Optional[int] = None
    ) -> Dict[str, List[Dict]]:
        """
        Fetch data for all configured indices.

        Args:
            use_screener: If True, screen stocks first and only fetch qualified ones
            max_symbols_per_index: If set, limit symbols per index before fetching

        Returns:
            Dictionary mapping index names to their stock data
        """
        indices_to_track = get_indices_to_track()
        all_data = {}
        index_fetcher = IndexFetcher()

        for index_name in indices_to_track:
            logger.info(f"Processing {index_name}...")

            # Get all symbols from this index
            symbols = index_fetcher.get_index_symbols(index_name)

            if len(symbols) > 100:
                logger.info(f"  Capping symbols for {index_name} to first 100 (of {len(symbols)})")
                symbols = symbols[:100]

            # Truthiness used to treat 0 as "no limit" (full-index Fanout) and
            # negative values as reverse slices (symbols[:-n]). Require an
            # explicit positive int; non-positive skips the index fetch.
            if max_symbols_per_index is not None:
                try:
                    symbol_limit = int(max_symbols_per_index)
                except (TypeError, ValueError, OverflowError):
                    logger.warning(
                        "Invalid max_symbols_per_index=%r; skipping %s",
                        max_symbols_per_index,
                        index_name,
                    )
                    symbols = []
                else:
                    if symbol_limit <= 0:
                        logger.warning(
                            "Non-positive max_symbols_per_index=%r; skipping %s",
                            max_symbols_per_index,
                            index_name,
                        )
                        symbols = []
                    elif len(symbols) > symbol_limit:
                        logger.info(f"  Limiting symbols for {index_name} to first {symbol_limit}")
                        symbols = symbols[:symbol_limit]

            if not symbols:
                logger.warning(f"Could not fetch symbols for {index_name}")
                continue

            # If using screener, screen first
            if use_screener and len(symbols) > 20:

                # Load filter config
                filter_config_path = Path(__file__).parent.parent / "config" / "filters.json"
                filters = None
                if filter_config_path.exists():
                    try:
                        with open(filter_config_path, "r") as f:
                            filters = json.load(f)
                    except Exception:
                        pass

                try:
                    # Share API client to avoid creating multiple instances
                    screener = stock_screener.StockScreener(filters, api_client=self.api_client)
                    logger.info(
                        f"  Screening {len(symbols)} stocks from {index_name} "
                        f"(parallel processing)..."
                    )
                    qualified_symbols = screener.get_qualified_symbols(symbols, max_workers=2)
                    logger.info(
                        f"  Selected {len(qualified_symbols)} qualified stocks " f"for tracking"
                    )
                    symbols = qualified_symbols
                except Exception as e:
                    # Screener/API edges must not abort the whole index fetch;
                    # keep the already-capped unscreened symbol list.
                    logger.warning(
                        "  Screener failed for %s; using unscreened symbols: %s",
                        index_name,
                        e,
                    )

            # Fetch data for each symbol using parallel processing
            # Rate limiter is thread-safe and handles 60 calls/min limit automatically
            # Use small worker count and batch pauses to stay under limits
            results = []
            completed = 0
            failed_count = 0
            try:
                max_workers = int(os.getenv("STOCK_FETCH_MAX_WORKERS", "2"))
            except ValueError:
                max_workers = 2
            max_workers = max(1, min(4, max_workers))

            logger.info(
                f"  Fetching data for {len(symbols)} stocks in parallel ({max_workers} workers)..."
            )

            with ThreadPoolExecutor(max_workers=max_workers) as executor:
                # Submit all fetch tasks with slight staggering
                future_to_symbol = {}
                for i, symbol in enumerate(symbols):
                    # Stagger every 5 submissions to avoid burst
                    if i > 0 and i % 5 == 0:
                        time.sleep(0.3)
                    future_to_symbol[executor.submit(self.fetch_symbol_data, symbol)] = symbol

                # Process completed tasks as they finish
                for future in as_completed(future_to_symbol):
                    symbol = future_to_symbol[future]
                    completed += 1

                    if completed % 25 == 0:
                        logger.info(
                            f"  Progress: {completed}/{len(symbols)} symbols... ({failed_count} "
                            "failed)"
                        )
                        # Batch pause to stay under minute budget
                        time.sleep(5)

                    try:
                        data = future.result()
                        if data:
                            data["index_name"] = index_name
                            results.append(data)
                        else:
                            failed_count += 1
                    except Exception as e:
                        failed_count += 1
                        logger.debug(f"Error fetching {symbol}: {e}")

            all_data[index_name] = results
            logger.info(f"  {index_name}: {len(results)} stocks fetched")

        return all_data

    # Legacy methods removed - use fetch_all_indices() instead

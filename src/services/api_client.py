"""
MarketHelm - API Client Module

Professional API client for fetching stock data using official APIs.
Uses Finnhub API (official, reliable, free tier: 60 calls/minute).
"""

import math
import os
import threading
import time
from collections import deque
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

import requests
from dotenv import load_dotenv
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from ..analysis.market_calendar import get_market_calendar, previous_close_session_at
from ..core.logger import get_logger

# Load environment variables
load_dotenv()

logger = get_logger(__name__)

# Cap Finnhub Retry-After so a huge/poisoned header cannot stall workers for days.
MAX_RETRY_AFTER_SECONDS = 300


def _finite_number(value: Any, default: Optional[float] = 0.0) -> Optional[float]:
    """Coerce to a finite float; non-numeric / NaN / Inf → default (or None)."""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    if not math.isfinite(number):
        return default
    return number


def _quote_outcome_provenance(
    value: Any, captured_at: Optional[datetime] = None
) -> Tuple[Optional[str], Optional[str], bool]:
    """Qualify Finnhub ``pc`` as the prior XNYS session's official close."""
    timestamp = _finite_number(value, default=None)
    if timestamp is None or timestamp < 0:
        return None, None, False
    try:
        quote_at = datetime.fromtimestamp(timestamp, tz=timezone.utc)
        observed_at = captured_at or datetime.now(timezone.utc)
        if observed_at.tzinfo is None or observed_at.utcoffset() is None:
            return quote_at.isoformat(), None, False
        if quote_at > observed_at.astimezone(timezone.utc) + timedelta(minutes=5):
            return quote_at.isoformat(), None, False
        session = previous_close_session_at(observed_at)
        if session is None:
            return quote_at.isoformat(), None, False
        calendar = get_market_calendar()
        quote_date = quote_at.astimezone(calendar.tz).date()
        collection_session = calendar.date_to_session(
            observed_at.astimezone(calendar.tz).date().isoformat(), direction="none"
        ).date()
        if quote_date not in {session, collection_session}:
            return quote_at.isoformat(), None, False
    except (OSError, OverflowError, ValueError):
        return None, None, False
    return quote_at.isoformat(), session.isoformat(), True


class RateLimiter:
    """
    Thread-safe rate limiter using token bucket algorithm.
    Ensures we never exceed calls_per_minute even with parallel workers.
    """

    def __init__(self, calls_per_minute: int = 60):
        """
        Initialize rate limiter with token bucket.

        Args:
            calls_per_minute: Maximum API calls per minute
        """
        self.calls_per_minute = calls_per_minute
        # Start conservative to avoid initial burst
        self.tokens = min(10, calls_per_minute // 6)  # Start with 10 tokens max
        self.last_refill = time.time()
        self.refill_rate = calls_per_minute / 60.0  # Tokens per second
        # Rolling window budget to cap total calls per minute (belt and suspenders)
        self.budget_max_calls = max(1, int(calls_per_minute * 0.92))  # e.g., 55 for 60 limit
        self.budget_window_sec = 60.0
        self.call_times = deque()

        # Thread safety
        try:
            import threading

            self.lock = threading.Lock()
        except ImportError:
            self.lock = None

    def wait_if_needed(self):
        """Wait if necessary to respect rate limits (thread-safe)."""
        if self.lock:
            self.lock.acquire()

        try:
            now = time.time()
            # Enforce rolling budget. Use >= so an exact window-aged entry is
            # dropped after sleep(wait_budget); `>` left it in and allowed
            # budget_max_calls + 1 recorded calls.
            while self.call_times and now - self.call_times[0] >= self.budget_window_sec:
                self.call_times.popleft()
            if len(self.call_times) >= self.budget_max_calls:
                wait_budget = self.budget_window_sec - (now - self.call_times[0])
                if wait_budget > 0:
                    time.sleep(wait_budget)
                # After sleeping, purge again
                now = time.time()
                while self.call_times and now - self.call_times[0] >= self.budget_window_sec:
                    self.call_times.popleft()

            current_time = time.time()

            # Refill tokens based on time elapsed
            time_elapsed = current_time - self.last_refill
            tokens_to_add = time_elapsed * self.refill_rate
            self.tokens = min(self.calls_per_minute, self.tokens + tokens_to_add)
            self.last_refill = current_time

            # If no tokens available, wait until we have one
            if self.tokens < 1.0:
                wait_time = (1.0 - self.tokens) / self.refill_rate
                time.sleep(wait_time)
                # Refill after waiting
                current_time = time.time()
                time_elapsed = current_time - self.last_refill
                self.tokens = min(
                    self.calls_per_minute, self.tokens + time_elapsed * self.refill_rate
                )
                self.last_refill = current_time

            # Consume a token
            self.tokens -= 1.0
            self.call_times.append(current_time)

        finally:
            if self.lock:
                self.lock.release()


class FinnhubClient:
    """
    Professional API client for Finnhub (official stock data API).

    Free tier: 60 calls/minute
    Documentation: https://finnhub.io/docs/api
    """

    def __init__(self, api_key: Optional[str] = None):
        """
        Initialize Finnhub client.

        Args:
            api_key: Finnhub API key (or set FINNHUB_API_KEY env var)
        """
        self.api_key = api_key or os.getenv("FINNHUB_API_KEY")

        if not self.api_key:
            raise ValueError(
                "Finnhub API key required. "
                "Set FINNHUB_API_KEY environment variable or pass api_key parameter. "
                "Get free API key at: https://finnhub.io/register"
            )

        self.base_url = "https://finnhub.io/api/v1"
        self.rate_limiter = RateLimiter(calls_per_minute=60)  # Free tier limit

        # Profile cache: symbol -> (profile, timestamp) - avoids redundant API calls
        self._profile_cache: Dict[str, Tuple[Dict, float]] = {}
        self._profile_cache_ttl = 86400  # 24 hours
        self._profile_cache_lock = threading.Lock()

        # Configure session with retry strategy
        # Note: 429 errors are handled manually, not by retry strategy
        self.session = requests.Session()
        retry_strategy = Retry(
            total=2,  # Reduced retries
            backoff_factor=2,
            status_forcelist=[500, 502, 503, 504],  # Removed 429 - handle manually
            allowed_methods=["GET"],
            respect_retry_after_header=True,
        )
        adapter = HTTPAdapter(max_retries=retry_strategy)
        self.session.mount("http://", adapter)
        self.session.mount("https://", adapter)

        logger.info("Finnhub API client initialized")

    def _make_request(self, endpoint: str, params: Dict) -> Dict:
        """
        Make API request with rate limiting and error handling.

        Args:
            endpoint: API endpoint
            params: Request parameters

        Returns:
            JSON response as dictionary

        Raises:
            requests.RequestException: On API errors
        """
        # Respect rate limits
        self.rate_limiter.wait_if_needed()

        # Add API key to params
        params = params.copy()
        params["token"] = self.api_key

        url = f"{self.base_url}/{endpoint}"

        max_retries = 3
        for attempt in range(max_retries):
            try:
                response = self.session.get(url, params=params, timeout=10)

                # Handle 429 rate limit errors explicitly
                if response.status_code == 429:
                    raw_retry_after = response.headers.get("Retry-After", 60)
                    try:
                        retry_after = int(raw_retry_after)
                    except (TypeError, ValueError, OverflowError):
                        # HTTP-date or garbage headers must not abort the fetch path.
                        retry_after = 60
                    if retry_after < 0:
                        retry_after = 60
                    retry_after = min(retry_after, MAX_RETRY_AFTER_SECONDS)
                    if attempt < max_retries - 1:
                        logger.warning(
                            "Rate limit exceeded (429). Waiting %s seconds...",
                            retry_after,
                        )
                        time.sleep(retry_after)
                        # Reset rate limiter tokens after waiting
                        self.rate_limiter.tokens = self.rate_limiter.calls_per_minute
                        self.rate_limiter.last_refill = time.time()
                        continue
                    raise requests.exceptions.HTTPError(
                        f"Rate limit exceeded after {max_retries} attempts"
                    )

                response.raise_for_status()

                data = response.json()

                # Check for API errors
                if isinstance(data, dict) and "error" in data:
                    raise ValueError(f"Finnhub API error: {data['error']}")

                return data

            except requests.exceptions.HTTPError as e:
                if e.response and e.response.status_code == 429:
                    # Already handled above, but catch here too
                    if attempt < max_retries - 1:
                        continue
                raise
            except requests.exceptions.RequestException as e:
                if attempt < max_retries - 1:
                    wait_time = 2**attempt  # Exponential backoff: 1s, 2s, 4s
                    logger.debug("Request failed, retrying in %ss: %s", wait_time, e)
                    time.sleep(wait_time)
                    continue
                logger.error("API request failed after %s attempts: %s", max_retries, e)
                raise

        raise requests.exceptions.RequestException(f"Request failed after {max_retries} attempts")

    def get_quote(self, symbol: str) -> Dict:
        """
        Get real-time quote for a symbol.

        Args:
            symbol: Stock ticker symbol (e.g., "AAPL")

        Returns:
            Dictionary with quote data (c, h, l, o, pc, t)
        """
        return self._make_request("quote", {"symbol": symbol})

    def get_company_profile(self, symbol: str) -> Dict:
        """
        Get company profile from Finnhub stock/profile2.
        Caches responses for 24h to avoid redundant API calls when re-fetching.

        Returns (on success): name, ticker, exchange, marketCapitalization,
        finnhubIndustry, country, currency, ipo, logo, weburl, etc.

        Args:
            symbol: Stock ticker symbol

        Returns:
            Profile dict with 'name' key (company name) when successful
        """
        sym = symbol.upper()
        with self._profile_cache_lock:
            cached = self._profile_cache.get(sym)
            if cached:
                profile, ts = cached
                if time.time() - ts < self._profile_cache_ttl:
                    return profile

        profile = self._make_request("stock/profile2", {"symbol": symbol})
        with self._profile_cache_lock:
            self._profile_cache[sym] = (profile, time.time())
        return profile

    def get_candle_data(self, symbol: str, resolution: str = "D", days: int = 5) -> Dict:
        """
        Get historical candle (OHLCV) data.

        Args:
            symbol: Stock ticker symbol
            resolution: Data resolution (D=day, 1=1min, 5=5min, etc.)
            days: Number of days of history

        Returns:
            Dictionary with candle data (c, h, l, o, t, v)
        """
        end_time = int(time.time())
        start_time = int((datetime.now() - timedelta(days=days)).timestamp())

        return self._make_request(
            "stock/candle",
            {"symbol": symbol, "resolution": resolution, "from": start_time, "to": end_time},
        )

    def get_stock_data_for_screening(self, symbol: str) -> Optional[Dict]:
        """
        Get lightweight stock data for screening (quote only - 1 API call).
        Use this for initial screening to minimize API calls.

        Args:
            symbol: Stock ticker symbol

        Returns:
            Dictionary with basic stock data or None if fetch fails
        """
        try:
            # Get quote only (1 API call instead of 3)
            quote = self.get_quote(symbol)

            if not quote or quote.get("c") is None:
                logger.debug(f"No quote data for {symbol}")
                return None

            current_price = _finite_number(quote.get("c"), default=None)
            if current_price is None:
                logger.debug(f"Non-finite quote close for {symbol}")
                return None

            previous_close = _finite_number(quote.get("pc"), default=None)
            if previous_close is None:
                previous_close = current_price
            change = current_price - previous_close
            change_pct = (change / previous_close * 100) if previous_close > 0 else 0.0
            volume = _finite_number(quote.get("v"), default=0.0) or 0.0

            return {
                "symbol": symbol,
                "close": current_price,
                "volume": volume,
                "change_percent": change_pct,
                "market_cap": 0,  # Will be fetched later for qualified stocks
                "name": symbol,  # Will be fetched later
            }

        except Exception as e:
            logger.debug(f"Error fetching screening data for {symbol}: {e}")
            return None

    def get_stock_data(self, symbol: str, include_profile: bool = True) -> Optional[Dict]:
        """
        Get stock data for a symbol.
        By default includes company profile (quote + profile = 2 API calls).
        Set include_profile=False for faster quote-only refreshes.

        Args:
            symbol: Stock ticker symbol

        Returns:
            Dictionary with complete stock data or None if fetch fails
        """
        try:
            # Get quote (real-time price) - 1 API call
            quote = self.get_quote(symbol)
            quote_captured_at = datetime.now(timezone.utc)

            if not quote or quote.get("c") is None:
                logger.debug(f"No quote data for {symbol}")
                return None

            profile = {}
            if include_profile:
                try:
                    profile = self.get_company_profile(symbol)
                except Exception as e:
                    logger.debug(f"Could not fetch profile for {symbol}: {e}")
                    profile = {}

            # Finnhub profile2 returns 'name' with company name (e.g. "Apple Inc").
            # Use it when present; only fall back to symbol when profile failed or is empty.
            name = (profile.get("name") or symbol) if profile else symbol

            current_price = _finite_number(quote.get("c"), default=None)
            if current_price is None:
                logger.debug(f"Non-finite quote close for {symbol}")
                return None

            official_previous_close = _finite_number(quote.get("pc"), default=None)
            previous_close = official_previous_close
            if previous_close is None:
                previous_close = current_price
            change = current_price - previous_close
            change_pct = (change / previous_close * 100) if previous_close > 0 else 0.0
            volume = _finite_number(quote.get("v"), default=0.0) or 0.0
            market_cap = _finite_number(profile.get("marketCapitalization", 0), default=0.0) or 0.0
            quote_timestamp, outcome_session, outcome_final = _quote_outcome_provenance(
                quote.get("t"), quote_captured_at
            )
            outcome_final = (
                outcome_final
                and official_previous_close is not None
                and official_previous_close > 0
            )
            if not outcome_final:
                outcome_session = None

            return {
                "symbol": symbol,
                "date": datetime.now().date(),
                "open": _finite_number(quote.get("o"), default=current_price),
                "close": current_price,
                "high": _finite_number(quote.get("h"), default=current_price),
                "low": _finite_number(quote.get("l"), default=current_price),
                "volume": volume,
                "previous_close": previous_close,
                "change": change,
                "change_percent": change_pct,
                "name": name,
                "exchange": profile.get("exchange", "Unknown"),
                "market_cap": market_cap,
                "quote_timestamp": quote_timestamp,
                "outcome_session": outcome_session,
                "outcome_close": official_previous_close if outcome_final else None,
                "outcome_final": outcome_final,
            }

        except Exception as e:
            logger.warning(f"Error fetching data for {symbol}: {e}")
            return None

    def batch_get_stock_data(
        self, symbols: List[str], include_profile: bool = True
    ) -> Dict[str, Optional[Dict]]:
        """
        Get stock data for multiple symbols efficiently.
        Respects rate limits automatically.

        Args:
            symbols: List of stock ticker symbols

        Returns:
            Dictionary mapping symbols to their data (or None if failed)
        """
        results = {}

        for symbol in symbols:
            results[symbol] = self.get_stock_data(symbol, include_profile=include_profile)

        return results

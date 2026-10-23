"""
Data loading service for reading CSV and JSON files
"""

import os
import re
from datetime import datetime
from datetime import datetime as _datetime
from datetime import timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional

import pandas as pd

import dashboard.backend.user_paths as user_paths
import src.storage.market_bars as market_bars
import src.storage.projections_store as projections_store
from src.analysis.backtesting import backtest_data_dir
from src.utils.tickers import normalize_ticker

_ISO_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _default_data_dir() -> Path:
    """Resolve data directory: DATA_DIR env, repo data/ when developing, else user data dir."""

    env_dir = os.getenv("DATA_DIR")
    if env_dir:
        return Path(env_dir).resolve()
    here = Path(__file__).resolve()
    if "site-packages" in here.parts:
        return user_paths.user_config_dir() / "data"
    # Project root is 4 levels up (dashboard/backend/services/data_loader.py)
    project_root = here.parent.parent.parent.parent
    return project_root / "data"


def _reject_nonfinite_json_constant(constant: str) -> None:
    """Fail closed on NaN/Infinity JSON constants (not strict JSON)."""
    raise ValueError(f"non-finite JSON constant: {constant}")


def _is_iso_date(date_str: str) -> bool:
    """True when date_str is a strict YYYY-MM-DD calendar date."""
    if not _ISO_DATE_RE.fullmatch(date_str or ""):
        return False

    try:
        _datetime.strptime(date_str, "%Y-%m-%d")
    except ValueError:
        return False
    return True


def _is_weekday(date_str: str) -> bool:
    """True if date (YYYY-MM-DD) is Mon–Fri (stock market open)."""
    try:
        dt = datetime.strptime(date_str, "%Y-%m-%d")
        return dt.weekday() < 5  # 0=Mon, 4=Fri, 5=Sat, 6=Sun
    except ValueError:
        return True  # Keep if we can't parse


def get_most_recent_trading_day() -> str:
    """Return the most recent trading day (YYYY-MM-DD). If today is weekend, returns last Friday."""
    today = datetime.now().date()
    # weekday: Mon=0, Fri=4, Sat=5, Sun=6
    if today.weekday() == 5:  # Saturday -> Friday
        today = today - timedelta(days=1)
    elif today.weekday() == 6:  # Sunday -> Friday
        today = today - timedelta(days=2)
    return today.strftime("%Y-%m-%d")


class DataLoader:
    """Loads and caches stock market data from durable stores and JSON/CSV files."""

    def __init__(self, data_dir: Optional[Path] = None):
        if data_dir is None:
            self.data_dir = _default_data_dir()
        else:
            self.data_dir = Path(data_dir).resolve()

        if not self.data_dir.exists():
            raise ValueError(f"Data directory not found: {self.data_dir}")

    def get_latest_date(self) -> Optional[str]:
        """Get the date of the most recent trading-day data (skips weekends when closed)."""
        dates = self.get_available_dates()
        for d in dates:
            if _is_weekday(d):
                return d
        return dates[0] if dates else None

    def needs_fetch_for_latest_trading_day(self) -> bool:
        """True if we don't have data for the most recent trading day (e.g. last Friday)."""
        target = get_most_recent_trading_day()
        dates = self.get_available_dates()
        return target not in dates

    def load_daily_data(self, date: Optional[str] = None) -> pd.DataFrame:
        """Load daily stock data from durable ``market_bars`` storage."""

        try:
            if date is None:
                dates = market_bars.list_market_bar_dates(data_dir=self.data_dir, limit=64)
                day = None
                for candidate in dates:
                    if _is_weekday(candidate):
                        day = candidate
                        break
                if day is None and dates:
                    day = dates[0]
                if day is None:
                    raise ValueError("No daily data found")
            else:
                if not _is_iso_date(date):
                    raise ValueError(f"Daily data not found for date: {date}")
                day = date

            frame = market_bars.market_bars_frame(day, data_dir=self.data_dir)
        except ValueError:
            raise
        except Exception as exc:
            raise ValueError(f"Daily data unreadable for date: {date or 'latest'}") from exc

        if frame is None or frame.empty:
            if date is None:
                raise ValueError("No daily data found")
            raise ValueError(f"Daily data not found for date: {date}")
        return frame

    def load_projections(self, date: Optional[str] = None) -> pd.DataFrame:
        """Load projections from durable ``projections`` storage."""

        try:
            if date is None:
                dates = projections_store.list_projection_dates(data_dir=self.data_dir, limit=64)
                day = None
                for candidate in dates:
                    if _is_weekday(candidate):
                        day = candidate
                        break
                if day is None and dates:
                    day = dates[0]
                if day is None:
                    raise ValueError("No projection files found")
            else:
                if not _is_iso_date(date):
                    raise ValueError(f"Projections file not found for date: {date}")
                day = date

            frame = projections_store.projections_frame(day, data_dir=self.data_dir)
        except ValueError:
            raise
        except Exception as exc:
            raise ValueError(f"Projections unreadable for date: {date or 'latest'}") from exc

        if frame is None or frame.empty:
            if date is None:
                raise ValueError("No projection files found")
            raise ValueError(f"Projections file not found for date: {date}")
        return frame

    def load_summary(self, date: Optional[str] = None) -> Dict:
        """Load summary from durable ``daily_summaries`` storage."""

        try:
            if date is None:
                dates = projections_store.list_summary_dates(data_dir=self.data_dir, limit=64)
                day = None
                for candidate in dates:
                    if _is_weekday(candidate):
                        day = candidate
                        break
                if day is None and dates:
                    day = dates[0]
                if day is None:
                    raise ValueError("No summary files found")
            else:
                if not _is_iso_date(date):
                    raise ValueError(f"Summary file not found for date: {date}")
                day = date

            data = projections_store.load_daily_summary(day, data_dir=self.data_dir)
        except ValueError:
            raise
        except Exception as exc:
            raise ValueError(f"Summary file unreadable for date: {date or 'latest'}") from exc

        if not isinstance(data, dict):
            if date is None:
                raise ValueError("No summary files found")
            raise ValueError(f"Summary file unreadable for date: {date}")
        return data

    def get_available_dates(self) -> List[str]:
        """Get list of all available quote dates from ``market_bars`` (newest first)."""

        try:
            return market_bars.list_market_bar_dates(data_dir=self.data_dir, limit=3650)
        except OSError as exc:
            raise ValueError(f"Data directory unreadable: {self.data_dir}") from exc
        except Exception as exc:
            raise ValueError(f"Data directory unreadable: {self.data_dir}") from exc

    def load_historical_data(self, symbol: str, days: int = 30) -> List[Dict]:
        """Load historical data for a specific symbol"""
        sym = normalize_ticker(symbol)
        if not sym:
            return []

        dates = self.get_available_dates()
        cutoff_date = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")

        historical_data = []
        for date in dates:
            if date < cutoff_date:
                break

            try:
                # Load daily data — match padded / mixed-case CSV symbols.
                daily_df = self.load_daily_data(date)
                stock_data = daily_df[daily_df["symbol"].map(normalize_ticker) == sym]

                if stock_data.empty:
                    continue

                stock_record = stock_data.iloc[0].to_dict()

                # Try to load projections for this date
                try:
                    proj_df = self.load_projections(date)
                    proj_data = proj_df[proj_df["symbol"].map(normalize_ticker) == sym]

                    if not proj_data.empty:
                        proj_record = proj_data.iloc[0].to_dict()
                        stock_record["projection"] = {
                            "target_price": proj_record.get("target_mid", None),
                            "confidence": proj_record.get("confidence", None),
                            "recommendation": proj_record.get("recommendation", None),
                            "expected_change": proj_record.get("expected_change_percent", None),
                        }
                except Exception:
                    # No projection data for this date
                    pass

                stock_record["date"] = date
                historical_data.append(stock_record)

            except Exception:
                continue

        return historical_data

    def compute_projection_accuracy(self, days: int = 90) -> Dict[str, Any]:
        """Evaluate saved projections on an exact five-session XNYS horizon."""
        return backtest_data_dir(self.data_dir, days=days)


# Singleton instance with caching
_data_loader = None


def get_data_loader() -> DataLoader:
    """Get the singleton DataLoader instance"""
    global _data_loader
    if _data_loader is None:
        _data_loader = DataLoader()
    return _data_loader

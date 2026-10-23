"""
MarketHelm - Data Storage Module

Handles durable market bars plus summary/projection file persistence.
"""

import math
import os
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional

import pandas as pd

import src.storage.market_bars as market_bars
import src.storage.projections_store as projections_store

from ..core.logger import get_logger
from ..utils.company_names import enrich_stock_data_with_names

logger = get_logger(__name__)


def _json_safe_value(value: Any) -> Any:
    """Coerce nested non-finite floats to None for strict JSON writers."""
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, dict):
        return {key: _json_safe_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe_value(item) for item in value]
    return value


def _md_reason(value: Any, max_len: int) -> str:
    """Truncate reason text; None/NaN/non-strings must not raise on slice/len."""
    if value is None:
        return ""
    if isinstance(value, float) and not math.isfinite(value):
        return ""
    # pandas may surface missing cells as NaN floats before string columns.
    if pd.isna(value):
        return ""
    text = str(value)
    if len(text) > max_len:
        return text[:max_len] + "..."
    return text


def _md_money(value: Any) -> str:
    """Format a money cell; None/NaN/Inf must not raise on :.2f."""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return "—"
    if not math.isfinite(number):
        return "—"
    return f"${number:.2f}"


def _md_pct(value: Any, precision: int = 1, signed: bool = True) -> str:
    """Format a percent cell; non-finite values render as an em dash."""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return "—"
    if not math.isfinite(number):
        return "—"
    if signed:
        return f"{number:+.{precision}f}%"
    return f"{number:.{precision}f}%"


def _atomic_replace(path: Path, write) -> None:
    """Write to a sibling temp file then replace, preserving prior contents on failure."""
    tmp = path.with_name(path.name + ".tmp")
    try:
        write(tmp)
        tmp.replace(path)
    except Exception:
        if tmp.exists():
            try:
                tmp.unlink()
            except OSError:
                pass
        raise


def _data_date_for_filename() -> datetime.date:
    """Return the collection date used to keep each snapshot distinct."""
    return datetime.now().date()


class DataStorage:
    """Manages storage of stock market data (market_bars + projection/summary files)."""

    def __init__(self, data_dir: Optional[str] = None):
        """
        Initialize data storage.

        Args:
            data_dir: Directory to store data files. When omitted, uses
                ``DATA_DIR`` if set so Fetch New / CLI writes land in the same
                persistent directory the dashboard and worker read.
        """
        if data_dir is None:
            data_dir = os.getenv("DATA_DIR") or "data"
        self.data_dir = Path(data_dir)
        self.data_dir.mkdir(exist_ok=True)

    def save_daily_data(self, data: List[Dict], date: datetime.date = None) -> str:
        """
        Persist daily stock quotes to durable ``market_bars`` storage.

        Enriches company names at save time (pytickersymbols) when name==symbol.
        Does not write ``daily_data_*.csv``.

        Args:
            data: List of stock data dictionaries
            date: Date for the data (defaults to today)

        Returns:
            Logical location string ``market_bars:YYYY-MM-DD``
        """
        if not data:
            return None

        enrich_stock_data_with_names(data)
        if date is None:
            date = _data_date_for_filename()

        written = market_bars.upsert_market_bars(
            data,
            date,
            data_dir=self.data_dir,
            source="fetch",
        )
        if written <= 0:
            raise ValueError("No valid market bars to persist (missing symbols/closes)")
        return f"market_bars:{date.strftime('%Y-%m-%d')}"

    def load_daily_data(self, trade_date: date = None) -> Optional[pd.DataFrame]:
        """
        Load daily stock data from ``market_bars``.

        Args:
            trade_date: Date to load (defaults to latest available bar date)

        Returns:
            DataFrame with stock data or None if not found
        """

        if trade_date is None:
            dates = market_bars.list_market_bar_dates(data_dir=self.data_dir, limit=1)
            if not dates:
                return None
            day = dates[0]
        else:
            day = (
                trade_date.strftime("%Y-%m-%d") if isinstance(trade_date, date) else str(trade_date)
            )

        try:
            frame = market_bars.market_bars_frame(day, data_dir=self.data_dir)
        except Exception as e:
            logger.error("Error loading market bars for %s: %s", day, e)
            return None
        if frame is None or frame.empty:
            return None
        return frame

    def save_summary(self, summary_data: Dict, date: datetime.date = None) -> str:
        """
        Persist daily summary statistics to durable ``daily_summaries`` storage.

        Does not write ``summary_*.json``.

        Args:
            summary_data: Dictionary with summary statistics
            date: Date for the summary (defaults to today)

        Returns:
            Logical location string ``summary:YYYY-MM-DD``
        """
        if date is None:
            date = _data_date_for_filename()

        payload = _json_safe_value(dict(summary_data))
        if not isinstance(payload, dict):
            raise ValueError("Summary payload must be a JSON object")
        return projections_store.upsert_daily_summary(
            payload, date, data_dir=self.data_dir, source="tracker"
        )

    def save_projections(self, projections: Dict, date: datetime.date = None) -> str:
        """
        Persist stock projections to durable ``projections`` storage.

        Optionally writes a human-readable Markdown report beside DATA_DIR.
        Does not write ``projections_*.csv``.

        Args:
            projections: Dictionary of stock projections
            date: Date for the projections (defaults to today)

        Returns:
            Logical location string ``projections:YYYY-MM-DD``
        """
        if not projections:
            return None

        if date is None:
            date = _data_date_for_filename()

        written = projections_store.upsert_projections(
            projections,
            date,
            data_dir=self.data_dir,
            source="tracker",
        )
        if written <= 0:
            raise ValueError("No valid projections to persist (missing symbols)")

        # Best-effort human report from the just-written rows.
        try:
            df = projections_store.projections_frame(date, data_dir=self.data_dir)
            if not df.empty:
                md_path = self.data_dir / f"projections_{date.strftime('%Y-%m-%d')}.md"
                self._generate_projection_markdown(df, md_path, date)
        except Exception as e:
            logger.warning("Could not generate markdown report: %s", e)

        return f"projections:{date.strftime('%Y-%m-%d')}"

    def _generate_projection_markdown(
        self, df: pd.DataFrame, output_path: Path, date: datetime.date
    ):
        """Generate a formatted Markdown report from projections DataFrame."""

        # Parse dates
        projection_date = pd.to_datetime(df["projection_date"].iloc[0]).strftime("%B %d, %Y")
        generated_date = datetime.now().strftime("%B %d, %Y at %I:%M %p")

        # Calculate statistics
        total_stocks = len(df)
        avg_confidence = df["confidence"].mean()
        avg_expected_change = df["expected_change_percent"].mean()

        # Get counts
        rec_counts = df["recommendation"].value_counts().to_dict()
        trend_counts = df["trend"].value_counts().to_dict()
        risk_counts = df["risk_level"].value_counts().to_dict()

        # Filter by recommendation
        strong_buys = df[df["recommendation"] == "STRONG BUY"].nlargest(10, "confidence")
        buys = df[df["recommendation"] == "BUY"].nlargest(10, "confidence")
        strong_sells = df[df["recommendation"] == "STRONG SELL"].nlargest(10, "confidence")

        # Top movers
        top_gainers = df.nlargest(10, "expected_change_percent")
        top_decliners = df.nsmallest(10, "expected_change_percent")

        # High confidence picks
        high_confidence = df[df["confidence"] >= 85].nlargest(10, "expected_change_percent")

        # Build markdown content
        md = []
        md.append("# Stock Market Projections Report")
        md.append("")
        horizon = 5
        if "projection_horizon_sessions" in df.columns:
            raw_horizon = df["projection_horizon_sessions"].iloc[0]
            try:
                if raw_horizon is not None and pd.notna(raw_horizon):
                    horizon = int(raw_horizon)
            except (TypeError, ValueError):
                horizon = 5
        calendar = "XNYS"
        if "projection_calendar" in df.columns:
            raw_calendar = df["projection_calendar"].iloc[0]
            if raw_calendar is not None and pd.notna(raw_calendar):
                text = str(raw_calendar).strip()
                if text:
                    calendar = text
        md.append(
            f"**Projection Period:** {horizon} {calendar} trading sessions "
            f"(Target Date: {projection_date})"
        )
        md.append("")
        md.append(f"**Generated:** {generated_date}")
        md.append("")
        md.append(f"**Total Stocks Analyzed:** {total_stocks}")
        md.append("")
        md.append("---")
        md.append("")

        # Executive Summary
        md.append("## Executive Summary")
        md.append("")
        # Means can be NaN when every row is non-finite; avoid :.2f TypeError
        # on None and keep a readable Neutral sentiment fallback.
        conf_text = (
            f"{float(avg_confidence):.1f}%"
            if pd.notna(avg_confidence) and math.isfinite(float(avg_confidence))
            else "—"
        )
        if pd.notna(avg_expected_change) and math.isfinite(float(avg_expected_change)):
            avg_change = float(avg_expected_change)
            direction = f"{'+' if avg_change >= 0 else ''}{avg_change:.2f}%"
            sentiment = (
                "Bullish" if avg_change > 0.5 else "Bearish" if avg_change < -0.5 else "Neutral"
            )
        else:
            direction = "—"
            sentiment = "Neutral"

        md.append(f"- **Average Confidence Level:** {conf_text}")
        md.append(f"- **Expected Market Direction:** {direction}")
        md.append(f"- **Market Sentiment:** {sentiment}")
        md.append("")

        # Recommendation distribution
        md.append("### Recommendation Distribution")
        md.append("")
        md.append("```text")
        total_recs = sum(rec_counts.values())
        for rec in ["STRONG BUY", "BUY", "HOLD", "SELL", "STRONG SELL"]:
            count = rec_counts.get(rec, 0)
            pct = (count / total_recs * 100) if total_recs > 0 else 0
            bar = "█" * int(pct / 2)
            md.append(f"{rec:12} │ {bar} {count:3d} ({pct:5.1f}%)")
        md.append("```")
        md.append("")

        # Trend breakdown
        md.append("### Market Sentiment Breakdown")
        md.append("")
        md.append("| Trend | Count | Percentage |")
        md.append("| ----- | ----- | ---------- |")
        for trend in ["Bullish", "Neutral", "Bearish"]:
            count = trend_counts.get(trend, 0)
            pct = (count / total_stocks * 100) if total_stocks > 0 else 0
            md.append(f"| {trend} | {count} | {pct:.1f}% |")
        md.append("")

        # Risk profile
        md.append("### Risk Profile")
        md.append("")
        md.append("| Risk Level | Count | Percentage |")
        md.append("| ---------- | ----- | ---------- |")
        for risk in ["Low", "Medium", "High"]:
            count = risk_counts.get(risk, 0)
            pct = (count / total_stocks * 100) if total_stocks > 0 else 0
            md.append(f"| {risk} | {count} | {pct:.1f}% |")
        md.append("")
        md.append("---")
        md.append("")

        # Strong Buys
        md.append("## STRONG BUY Opportunities")
        md.append("")
        md.append(f"{len(strong_buys)} stocks identified with STRONG BUY rating")
        md.append("")

        if len(strong_buys) > 0:
            md.append("| Symbol | Current → Target | Change | Confidence | Reason |")
            md.append("| ------ | ---------------- | ------ | ---------- | ------ |")
            for _, stock in strong_buys.iterrows():
                reason_short = _md_reason(stock.get("reason"), 55)
                md.append(
                    f"| **{stock['symbol']}** | "
                    f"{_md_money(stock.get('current_price'))} → "
                    f"{_md_money(stock.get('target_mid'))} | "
                    f"{_md_pct(stock.get('expected_change_percent'))} | "
                    f"{stock['confidence']}% | {reason_short} |"
                )
        md.append("")
        md.append("---")
        md.append("")

        # Buy Opportunities
        md.append("## BUY Opportunities")
        md.append("")
        md.append(f"{len(buys)} stocks identified with BUY rating")
        md.append("")

        if len(buys) > 0:
            md.append("| Symbol | Current | Target | Change | Confidence | Risk |")
            md.append("| ------ | ------- | ------ | ------ | ---------- | ---- |")
            for _, stock in buys.iterrows():
                md.append(
                    f"| **{stock['symbol']}** | {_md_money(stock.get('current_price'))} | "
                    f"{_md_money(stock.get('target_mid'))} | "
                    f"{_md_pct(stock.get('expected_change_percent'))} | "
                    f"{stock['confidence']}% | {stock['risk_level']} |"
                )
        md.append("")
        md.append("---")
        md.append("")

        # Strong Sells
        md.append("## STRONG SELL Warnings")
        md.append("")
        md.append(f"{len(strong_sells)} stocks identified with STRONG SELL rating")
        md.append("")

        if len(strong_sells) > 0:
            md.append("| Symbol | Current | Target | Change | Confidence | Risk | Reason |")
            md.append("| ------ | ------- | ------ | ------ | ---------- | ---- | ------ |")
            for _, stock in strong_sells.iterrows():
                reason_short = _md_reason(stock.get("reason"), 50)
                md.append(
                    f"| **{stock['symbol']}** | {_md_money(stock.get('current_price'))} | "
                    f"{_md_money(stock.get('target_mid'))} | "
                    f"{_md_pct(stock.get('expected_change_percent'))} | "
                    f"{stock['confidence']}% | {stock['risk_level']} | {reason_short} |"
                )
        md.append("")
        md.append("---")
        md.append("")

        # Top Gainers
        md.append("## Top Expected Price Gainers")
        md.append("")
        md.append("Stocks projected to increase the most (regardless of recommendation)")
        md.append("")
        md.append("| Symbol | Current | Target | Expected Gain | Confidence | Recommendation |")
        md.append("| ------ | ------- | ------ | ------------- | ---------- | -------------- |")
        for _, stock in top_gainers.iterrows():
            md.append(
                f"| **{stock['symbol']}** | {_md_money(stock.get('current_price'))} | "
                f"{_md_money(stock.get('target_mid'))} | "
                f"{_md_pct(stock.get('expected_change_percent'))} | "
                f"{stock['confidence']}% | {stock['recommendation']} |"
            )
        md.append("")

        # Top Decliners
        md.append("## Top Expected Price Decliners")
        md.append("")
        md.append("Stocks projected to decline the most")
        md.append("")
        md.append("| Symbol | Current | Target | Expected Decline | Confidence | Recommendation |")
        md.append("| ------ | ------- | ------ | ---------------- | ---------- | -------------- |")
        for _, stock in top_decliners.iterrows():
            md.append(
                f"| **{stock['symbol']}** | {_md_money(stock.get('current_price'))} | "
                f"{_md_money(stock.get('target_mid'))} | "
                f"{_md_pct(stock.get('expected_change_percent'))} | "
                f"{stock['confidence']}% | {stock['recommendation']} |"
            )
        md.append("")
        md.append("---")
        md.append("")

        # High Confidence Picks
        md.append("## High Confidence Picks (85%+)")
        md.append("")
        md.append(
            f"{len(high_confidence)} stocks with highest confidence and best upside potential"
        )
        md.append("")

        if len(high_confidence) > 0:
            md.append(
                "| Symbol | Current → Target | Expected Change | Confidence | Recommendation | "
                "Trend |"
            )
            md.append(
                "| ------ | ---------------- | --------------- | ---------- | -------------- | "
                "----- |"
            )
            for _, stock in high_confidence.iterrows():
                md.append(
                    f"| **{stock['symbol']}** | "
                    f"{_md_money(stock.get('current_price'))} → "
                    f"{_md_money(stock.get('target_mid'))} | "
                    f"{_md_pct(stock.get('expected_change_percent'))} | "
                    f"{stock['confidence']}% | {stock['recommendation']} | {stock['trend']} |"
                )
        md.append("")
        md.append("---")
        md.append("")

        # Disclaimer
        md.append("## Disclaimer")
        md.append("")
        md.append("> These projections are for informational purposes only. Not financial advice.")
        md.append(">")
        md.append("> Always conduct your own research and consult with financial advisors.")
        md.append("")
        md.append("---")
        md.append("")
        md.append(f"*Generated on {generated_date} by MarketHelm*")

        # Write to file with trailing newline
        with open(output_path, "w", encoding="utf-8") as f:
            f.write("\n".join(md) + "\n")

    def get_historical_data(self, start_date: date = None, end_date: date = None) -> pd.DataFrame:
        """
        Load historical data for a date range.

        Args:
            start_date: Start date (defaults to 30 days ago)
            end_date: End date (defaults to today)

        Returns:
            Combined DataFrame with historical data
        """
        if end_date is None:
            end_date = datetime.now().date()
        if start_date is None:
            start_date = end_date - timedelta(days=30)

        all_data = []
        current_date = start_date

        while current_date <= end_date:
            df = self.load_daily_data(current_date)
            if df is not None and not df.empty:
                all_data.append(df)
            current_date += timedelta(days=1)

        if all_data:
            return pd.concat(all_data, ignore_index=True)
        return pd.DataFrame()

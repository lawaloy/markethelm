"""
MarketHelm - AI Summarizer Module

Uses OpenAI API to generate natural language summaries of market data.
"""

import math
import os
from typing import Any, Dict, Optional

from dotenv import load_dotenv

from ..core.logger import get_logger

try:
    from openai import OpenAI
except ImportError:
    OpenAI = None

# Load environment variables
load_dotenv()

logger = get_logger(__name__)


def _finite_float(value: Any, default: float = 0.0) -> float:
    """Coerce null/NaN/Inf (or non-numeric) values to a finite float for formatting."""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    if not math.isfinite(number):
        return default
    return number


class AISummarizer:
    """Generates AI-powered summaries of stock market data."""

    def __init__(self):
        """Initialize the AI summarizer with API key from environment."""
        self.api_key = os.getenv("OPENAI_API_KEY")
        self.enabled = self.api_key is not None

        if not self.enabled:
            logger.info("OpenAI API key not found. Using demo summary mode.")
            logger.info(
                "To enable AI summaries: Set OPENAI_API_KEY environment variable or add to .env "
                "file"
            )

    def generate_demo_summary(self, analysis: Dict, exchange_comparison: Dict) -> str:
        """
        Generate a simple template-based summary (demo mode, no API needed).

        Args:
            analysis: Analysis dictionary from StockAnalyzer
            exchange_comparison: Exchange comparison dictionary

        Returns:
            Generated summary text
        """
        summary_data = analysis.get("summary", {})
        # Over-fetch a few rows so non-dict / blank-symbol sentinels cannot
        # hide the first usable mover from the demo highlight lines.
        top_gainers = analysis.get("top_gainers", [])[:6]
        top_losers = analysis.get("top_losers", [])[:6]

        # Build a simple summary
        summary_parts = []

        # Overall market sentiment
        gainers = summary_data.get("gainers", 0)
        losers = summary_data.get("losers", 0)
        avg_change = _finite_float(summary_data.get("average_change_percent", 0))

        if gainers > losers:
            sentiment = "positive"
        elif losers > gainers:
            sentiment = "negative"
        else:
            sentiment = "mixed"

        summary_parts.append(
            f"Today's market showed {sentiment} sentiment with {gainers} gainers and {losers} "
            f"losers, averaging {avg_change:.2f}% change overall."
        )

        # Highlight top movers (skip non-dict / blank symbols — same soft-fail
        # posture as generate_summary's OpenAI prompt path).
        for top_gainer in top_gainers:
            if not isinstance(top_gainer, dict):
                continue
            symbol = str(top_gainer.get("symbol") or "").strip()
            if not symbol:
                continue
            change = _finite_float(top_gainer.get("change_percent"))
            summary_parts.append(f"{symbol} led gains with a {change:.2f}% increase.")
            break

        for top_loser in top_losers:
            if not isinstance(top_loser, dict):
                continue
            symbol = str(top_loser.get("symbol") or "").strip()
            if not symbol:
                continue
            change = _finite_float(top_loser.get("change_percent"))
            summary_parts.append(f"{symbol} declined {abs(change):.2f}%, marking the largest drop.")
            break

        # Exchange performance (skip non-finite averages when ranking)
        def _exchange_avg(item):
            return _finite_float((item[1] or {}).get("average_change_percent", 0))

        best_exchange = max(exchange_comparison.items(), key=_exchange_avg, default=None)
        if best_exchange:
            exchange_name, stats = best_exchange
            exchange_avg = _finite_float(stats.get("average_change_percent", 0))
            summary_parts.append(
                f"The {exchange_name} exchange performed best with an average {exchange_avg:.2f}% "
                "gain."
            )

        return " ".join(summary_parts)

    def generate_summary(self, analysis: Dict, exchange_comparison: Dict) -> Optional[str]:
        """
        Generate a natural language summary of the market data.

        Args:
            analysis: Analysis dictionary from StockAnalyzer
            exchange_comparison: Exchange comparison dictionary

        Returns:
            Generated summary text or None if API key not available
        """
        if not self.enabled:
            # Return demo summary instead of None
            return self.generate_demo_summary(analysis, exchange_comparison)

        try:
            if OpenAI is None:
                raise ImportError("The optional OpenAI package is not installed")

            client = OpenAI(api_key=self.api_key)

            # Prepare data for the prompt
            summary_data = analysis.get("summary", {})
            top_gainers = analysis.get("top_gainers", [])[:3]  # Top 3
            top_losers = analysis.get("top_losers", [])[:3]  # Top 3

            # Coerce non-finite rollups so prompt formatting cannot throw into
            # the bare except and return None when OPENAI_API_KEY is set.
            avg_change = _finite_float(summary_data.get("average_change_percent"))
            # Build prompt
            prompt = (
                "Write a brief, professional market summary (2-3 sentences) "
                "based on this stock market data:\n"
                f"""
Date: {analysis.get('date', 'Today')}
Total Stocks: {summary_data.get('total_stocks', 0)}
Gainers: {summary_data.get('gainers', 0)}, Losers: {summary_data.get('losers', 0)}
Average Change: {avg_change:.2f}%

Top Gainers:
"""
            )
            for stock in top_gainers:
                if not isinstance(stock, dict):
                    continue
                change = _finite_float(stock.get("change_percent"))
                prompt += (
                    f"- {stock.get('symbol', 'N/A')} "
                    f"({stock.get('name', 'N/A')}): +{change:.2f}%\n"
                )

            prompt += "\nTop Losers:\n"
            for stock in top_losers:
                if not isinstance(stock, dict):
                    continue
                change = _finite_float(stock.get("change_percent"))
                prompt += (
                    f"- {stock.get('symbol', 'N/A')} "
                    f"({stock.get('name', 'N/A')}): {change:.2f}%\n"
                )

            prompt += "\nExchange Performance:\n"
            for exchange, stats in list(exchange_comparison.items())[:3]:
                if not isinstance(stats, dict):
                    continue
                exchange_avg = _finite_float(stats.get("average_change_percent"))
                gainers = stats.get("gainers", 0)
                losers = stats.get("losers", 0)
                prompt += (
                    f"- {exchange}: Avg {exchange_avg:.2f}% "
                    f"({gainers} gainers, {losers} losers)\n"
                )

            prompt += "\nWrite a concise, informative summary in a professional tone."

            response = client.chat.completions.create(
                model="gpt-3.5-turbo",
                messages=[
                    {
                        "role": "system",
                        "content": "You are a financial market analyst. Write clear, concise "
                        "market summaries.",
                    },
                    {"role": "user", "content": prompt},
                ],
                max_tokens=150,
                temperature=0.7,
            )

            return response.choices[0].message.content.strip()

        except ImportError:
            logger.warning("OpenAI package not installed. Install with: pip install openai")
            return None
        except Exception as e:
            logger.warning(f"Failed to generate AI summary: {str(e)}")
            return None

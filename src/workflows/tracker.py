"""
MarketHelm workflow — core business logic.

Orchestrates screening, fetch, analysis, and storage.
Reusable across CLI, web, and API.
"""

import math
from datetime import datetime
from typing import Any, Dict, List, Optional

import pandas as pd

from ..alerts.alert_engine import AlertEngine
from ..analysis.ai_summarizer import AISummarizer
from ..analysis.analyzer import StockAnalyzer
from ..analysis.projector import StockProjector
from ..core.config import get_indices_to_track
from ..core.logger import configure_logging, get_logger
from ..services.data_fetcher import StockDataFetcher
from ..storage.data_storage import DataStorage
from ..utils.tickers import normalize_ticker

logger = get_logger(__name__)


def _finite_numeric_series(series: pd.Series) -> pd.Series:
    """Coerce to numeric and drop null / non-finite values."""
    values = pd.to_numeric(series, errors="coerce")
    return values[values.map(lambda value: pd.notna(value) and math.isfinite(float(value)))]


def _volume_sort_key(row: Any) -> float:
    """Finite volume for top-N ranking; non-dict / dirty / Inf-NaN sort last."""
    if not isinstance(row, dict):
        return float("-inf")
    raw = row.get("volume", 0)
    if raw is None:
        return float("-inf")
    try:
        number = float(raw)
    except (TypeError, ValueError):
        return float("-inf")
    return number if math.isfinite(number) else float("-inf")


class StockTrackerWorkflow:
    """
    Core workflow for stock tracking.

    This class handles the business logic for:
    - Fetching stock data from indices
    - Analyzing market data
    - Generating projections and recommendations
    - Generating AI summaries
    - Saving results

    It returns structured data that can be consumed by any interface (CLI, Web, API).
    """

    def __init__(self, include_profile: bool = True):
        """Initialize workflow components."""
        self.fetcher = StockDataFetcher(include_profile=include_profile)
        self.storage = DataStorage()
        self.analyzer = StockAnalyzer()
        self.ai_summarizer = AISummarizer()
        self.projector = StockProjector()
        self.alert_engine = AlertEngine.from_config()
        logger.debug("Workflow components initialized")

    def run(self, use_screener: bool = True, top_n_stocks: Optional[int] = None) -> Dict[str, Any]:
        """
        Execute the complete stock tracking workflow.

        Args:
            use_screener: Whether to use stock screener for filtering
            top_n_stocks: If specified, limit to top N stocks by volume (for day trading)

        Returns:
            Dictionary containing:
            - success: bool
            - data: List of stock data
            - analysis: Analysis results
            - projections: Stock projections and recommendations
            - projection_summary: Summary of projections
            - ai_summary: AI-generated summary (if available)
            - file_paths: Saved file paths
            - index_comparison: Index performance comparison
            - error: Error message (if failed)
        """
        try:
            logger.info("Starting stock tracking workflow")

            # Step 1: Fetch data
            logger.info("Step 1: Fetching stock data")
            fetch_result = self._fetch_data(use_screener, top_n_stocks)

            if not fetch_result["success"]:
                return fetch_result

            all_data = fetch_result["data"]
            all_index_data = fetch_result["index_data"]

            # Step 2: Save data
            logger.info("Step 2: Saving data")
            save_result = self._save_data(all_data)

            # Step 3: Analyze data
            logger.info("Step 3: Analyzing data")
            analysis_result = self._analyze_data(all_data, all_index_data)

            # Step 4: Generate projections
            logger.info("Step 4: Generating projections")
            projection_result = self._generate_projections(all_data)

            # Step 5: Generate AI summary
            logger.info("Step 5: Generating summary")
            ai_summary = self._generate_summary(
                analysis_result["analysis"], analysis_result["index_comparison"]
            )

            # Step 6: Save summary and projections
            summary_result = self._save_summary(
                analysis_result["analysis"],
                analysis_result["index_comparison"],
                ai_summary,
                projection_result["projections"],
                projection_result["projection_summary"],
            )

            # Step 7: Evaluate alerts (if configured)
            self._run_alerts(all_data)

            logger.info("Workflow completed successfully")

            return {
                "success": True,
                "data": all_data,
                "analysis": analysis_result["analysis"],
                "index_comparison": analysis_result["index_comparison"],
                "projections": projection_result["projections"],
                "projection_summary": projection_result["projection_summary"],
                "ai_summary": ai_summary,
                "file_paths": {
                    "data": save_result.get("file_path"),
                    "summary": summary_result.get("file_path"),
                },
                "metadata": {
                    "date": datetime.now().date(),
                    "total_stocks": len(all_data),
                    "screener_enabled": use_screener,
                    "ai_enabled": self.ai_summarizer.enabled,
                    "projections_enabled": True,
                },
            }

        except Exception as e:
            logger.error(f"Workflow failed: {str(e)}", exc_info=True)
            return {"success": False, "error": str(e)}

    def _run_alerts(self, data: List[Dict]) -> None:
        """Evaluate configured alerts against current data."""
        if not self.alert_engine:
            return
        try:
            events = self.alert_engine.evaluate(data)
            if events:
                logger.info(f"Alerts triggered: {len(events)}")
        except Exception as e:
            logger.warning(f"Alert evaluation failed: {e}")

    def _fetch_data(self, use_screener: bool, top_n_stocks: Optional[int] = None) -> Dict[str, Any]:
        """Fetch stock data from configured indices."""
        try:
            # Non-positive limits are truthy for negatives and would slice from the
            # tail (`[: -N]`), selecting lowest-volume names. Treat as unlimited.
            if top_n_stocks is not None and top_n_stocks <= 0:
                top_n_stocks = None
            indices_to_track = get_indices_to_track()
            logger.info(f"Fetching data from indices: {', '.join(indices_to_track)}")
            logger.info(f"Stock screener enabled: {use_screener}")
            num_indices = len(indices_to_track)
            if top_n_stocks:
                logger.info(f"Day trading mode: Will select top {top_n_stocks} stocks by volume")
                # Limit per-index to avoid over-fetching (e.g. 10 stocks, 2 indices → 5 per index)
                max_symbols_per_index = (
                    max(1, (top_n_stocks + num_indices - 1) // num_indices)
                    if not use_screener
                    else None
                )
            else:
                max_symbols_per_index = None
            all_index_data = self.fetcher.fetch_all_indices(
                use_screener=use_screener, max_symbols_per_index=max_symbols_per_index
            )

            # Combine all data
            all_data = []
            for index_name, data in all_index_data.items():
                all_data.extend(data)
                logger.info(f"  {index_name}: {len(data)} stocks fetched")

            logger.info(f"Total stocks fetched: {len(all_data)}")

            # Day trading optimization: Select top N by volume
            if top_n_stocks and len(all_data) > top_n_stocks:
                logger.info(f"Filtering to top {top_n_stocks} stocks by volume...")
                # Sort by volume (highest first); None/Inf/NaN/non-dict must not
                # TypeError the refresh pipeline or poison ranking.
                all_data_sorted = sorted(all_data, key=_volume_sort_key, reverse=True)
                all_data = all_data_sorted[:top_n_stocks]
                logger.info(f"Selected top {len(all_data)} most active stocks")
                top_5_symbols = []
                for stock in all_data[:5]:
                    if not isinstance(stock, dict):
                        continue
                    volume = _volume_sort_key(stock)
                    volume_txt = f"{int(volume):,}" if math.isfinite(volume) else "?"
                    top_5_symbols.append(f"{stock.get('symbol')} ({volume_txt})")
                logger.debug(f"Top 5 by volume: {top_5_symbols}")

            from ..alerts.alert_paths import get_enabled_watch_symbols

            watch_symbols = get_enabled_watch_symbols()
            if watch_symbols:
                # Normalize so padded index symbols match stripped watch keys;
                # drop None/NaN sentinels that would otherwise become "NONE"/"NAN".
                # Non-dict fetch rows must not AttributeError the whole refresh
                # when top-N filtering is off (or when dirty rows remain).
                existing = {
                    key
                    for key in (
                        normalize_ticker(stock.get("symbol"))
                        for stock in all_data
                        if isinstance(stock, dict)
                    )
                    if key
                }
                for symbol in watch_symbols:
                    if symbol in existing:
                        continue
                    fetched = self.fetcher.fetch_symbol_data(symbol)
                    if fetched:
                        all_data.append(fetched)
                        existing.add(symbol)
                        logger.info(f"Included watch symbol {symbol} in fetch")

            if not all_data:
                logger.warning("No data was fetched")
                return {"success": False, "error": "No data fetched from any index"}

            return {"success": True, "data": all_data, "index_data": all_index_data}

        except Exception as e:
            logger.error(f"Data fetch failed: {str(e)}")
            return {"success": False, "error": f"Data fetch failed: {str(e)}"}

    def _save_data(self, data: List[Dict]) -> Dict[str, Any]:
        """Persist stock data to durable market_bars storage."""
        try:
            file_path = self.storage.save_daily_data(data)
            if file_path:
                logger.info(f"Data saved to: {file_path}")
                return {"success": True, "file_path": file_path}
            else:
                logger.error("Failed to save data")
                return {"success": False, "error": "Failed to save data"}
        except Exception as e:
            logger.error(f"Save failed: {str(e)}")
            return {"success": False, "error": str(e)}

    def _analyze_data(
        self, all_data: List[Dict], index_data: Dict[str, List[Dict]]
    ) -> Dict[str, Any]:
        """Analyze stock data and generate index comparison."""
        try:
            # Run analysis
            analysis = self.analyzer.analyze_daily_data(all_data)

            # Generate index comparison
            index_comparison = {}
            for index_name, data in index_data.items():
                if data:
                    df = pd.DataFrame(data)
                    finite_changes = _finite_numeric_series(df["change_percent"])
                    finite_volumes = _finite_numeric_series(df["volume"])
                    avg_change = float(finite_changes.mean()) if not finite_changes.empty else 0.0
                    total_volume = float(finite_volumes.sum()) if not finite_volumes.empty else 0.0
                    if not math.isfinite(avg_change):
                        avg_change = 0.0
                    if not math.isfinite(total_volume):
                        total_volume = 0.0
                    index_comparison[index_name] = {
                        "stock_count": len(df),
                        "average_change_percent": round(avg_change, 2),
                        "total_volume": int(total_volume),
                        "gainers": int((finite_changes > 0).sum()),
                        "losers": int((finite_changes < 0).sum()),
                    }

            logger.debug("Data analysis completed")

            return {"analysis": analysis, "index_comparison": index_comparison}

        except Exception as e:
            logger.error(f"Analysis failed: {str(e)}")
            return {"analysis": {}, "index_comparison": {}}

    def _generate_summary(self, analysis: Dict, index_comparison: Dict) -> Optional[str]:
        """Generate AI or demo summary."""
        try:
            ai_summary = self.ai_summarizer.generate_summary(analysis, index_comparison)
            if ai_summary:
                if self.ai_summarizer.enabled:
                    logger.info("AI summary generated")
                else:
                    logger.info("Demo summary generated")
            return ai_summary
        except Exception as e:
            logger.error(f"Summary generation failed: {str(e)}")
            return None

    def _generate_projections(self, data: List[Dict]) -> Dict[str, Any]:
        """Generate stock projections and recommendations."""
        try:
            # Generate projections for all stocks
            projections = self.projector.generate_projections(data)

            # Generate projection summary
            projection_summary = self.projector.generate_projection_summary(projections)

            logger.info(f"Generated {len(projections)} projections")

            return {"projections": projections, "projection_summary": projection_summary}

        except Exception as e:
            logger.error(f"Projection generation failed: {str(e)}")
            return {"projections": {}, "projection_summary": {}}

    def _save_summary(
        self,
        analysis: Dict,
        index_comparison: Dict,
        ai_summary: Optional[str],
        projections: Dict = None,
        projection_summary: Dict = None,
    ) -> Dict[str, Any]:
        """Persist analysis summary and projections to durable DB storage."""
        try:
            summary_data = {"analysis": analysis, "exchange_comparison": index_comparison}
            if ai_summary:
                summary_data["ai_summary"] = ai_summary

            if projections:
                summary_data["projections"] = projections

            if projection_summary:
                summary_data["projection_summary"] = projection_summary

            summary_path = self.storage.save_summary(summary_data)

            projection_location = None
            if projections:
                projection_location = self.storage.save_projections(projections)
                if projection_location:
                    logger.info(f"Projections saved to: {projection_location}")

            if summary_path:
                logger.info(f"Summary saved to: {summary_path}")
                return {
                    "success": True,
                    "file_path": summary_path,
                    "projection_location": projection_location,
                }
            else:
                return {"success": False, "error": "Failed to save summary"}
        except Exception as e:
            logger.error(f"Summary save failed: {str(e)}")
            return {"success": False, "error": str(e)}


if __name__ == "__main__":
    """
    Direct execution for programmatic/testing use.

    This runs the workflow and returns structured data without CLI formatting.
    For CLI output, use: python main.py or python -m src.cli.commands
    """
    import json

    configure_logging()

    print("Running MarketHelm workflow (programmatic mode)...")
    print("-" * 60)

    workflow = StockTrackerWorkflow()
    result = workflow.run(use_screener=True)

    if result.get("success"):
        print("\n✓ Workflow completed successfully!")
        print(f"✓ Data saved: {result.get('data_saved', {}).get('file_path')}")
        print(f"✓ Summary saved: {result.get('summary_saved', {}).get('file_path')}")

        # Print structured result (JSON)
        print("\n" + "=" * 60)
        print("RESULT (JSON):")
        print("=" * 60)
        print(
            json.dumps(
                {
                    "success": result["success"],
                    "metadata": result["metadata"],
                    "summary": result.get("analysis", {}).get("summary", {}),
                    "ai_summary": (
                        result.get("ai_summary", "N/A")[:200] + "..."
                        if result.get("ai_summary")
                        else "N/A"
                    ),
                },
                indent=2,
                default=str,
            )
        )
    else:
        print(f"\n✗ Workflow failed: {result.get('error')}")
        exit(1)

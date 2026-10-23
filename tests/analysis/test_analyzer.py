"""Tests for analysis analyzer module."""

import json
import math
import unittest
from datetime import date

import pandas as pd

from src.analysis.analyzer import StockAnalyzer


class TestStockAnalyzer(unittest.TestCase):
    """Test cases for stock analyzer."""

    def setUp(self):
        """Set up test fixtures."""
        self.analyzer = StockAnalyzer()

        # Create sample stock data
        self.sample_data = pd.DataFrame(
            {
                "symbol": ["AAPL", "GOOGL", "MSFT", "TSLA", "AMZN"],
                "date": [date.today()] * 5,
                "close": [150.0, 2800.0, 300.0, 200.0, 3200.0],
                "previous_close": [148.0, 2750.0, 298.0, 205.0, 3150.0],
                "change": [2.0, 50.0, 2.0, -5.0, 50.0],
                "change_percent": [1.35, 1.82, 0.67, -2.44, 1.59],
                "volume": [50000000, 30000000, 40000000, 60000000, 35000000],
                "market_cap": [
                    2500000000000,
                    1800000000000,
                    2300000000000,
                    600000000000,
                    1600000000000,
                ],
                "name": ["Apple Inc", "Alphabet Inc", "Microsoft", "Tesla", "Amazon"],
                "exchange": ["NASDAQ"] * 5,
            }
        )

    def test_analyzer_initialization(self):
        """Test that analyzer initializes correctly."""
        self.assertIsInstance(self.analyzer, StockAnalyzer)

    def test_analyze_returns_dict(self):
        """Test that analyze returns a dictionary."""
        data_list = self.sample_data.to_dict("records")
        result = self.analyzer.analyze_daily_data(data_list)
        self.assertIsInstance(result, dict)

    def test_analyze_has_top_gainers(self):
        """Test that analysis includes top gainers."""
        data_list = self.sample_data.to_dict("records")
        result = self.analyzer.analyze_daily_data(data_list)
        self.assertIn("top_gainers", result)
        gainers = result["top_gainers"]
        self.assertGreater(len(gainers), 0)
        self.assertEqual(gainers[0]["symbol"], "GOOGL")

    def test_analyze_has_top_losers(self):
        """Test that analysis includes top losers."""
        data_list = self.sample_data.to_dict("records")
        result = self.analyzer.analyze_daily_data(data_list)
        self.assertIn("top_losers", result)
        losers = result["top_losers"]
        self.assertGreater(len(losers), 0)
        self.assertEqual(losers[0]["symbol"], "TSLA")

    def test_analyze_has_statistics(self):
        """Test that analysis includes statistics."""
        data_list = self.sample_data.to_dict("records")
        result = self.analyzer.analyze_daily_data(data_list)
        self.assertIn("summary", result)
        summary = result["summary"]
        self.assertIn("total_stocks", summary)
        self.assertIn("gainers", summary)
        self.assertIn("losers", summary)
        self.assertIn("average_change_percent", summary)
        self.assertEqual(summary["total_stocks"], 5)
        self.assertEqual(summary["gainers"], 4)
        self.assertEqual(summary["losers"], 1)

    def test_empty_dataframe(self):
        """Test handling of empty dataframe."""
        empty_data = []
        result = self.analyzer.analyze_daily_data(empty_data)
        self.assertIsInstance(result, dict)
        self.assertEqual(len(result), 0)

    def test_analyze_includes_unchanged_top_volume_and_exchange_stats(self):
        """Exchange rollups, flat names, and volume leaders must stay populated."""
        rows = [
            {
                "symbol": "AAPL",
                "name": "Apple",
                "change_percent": 1.5,
                "close": 180.0,
                "volume": 50_000_000,
                "exchange_code": "NASDAQ",
            },
            {
                "symbol": "MSFT",
                "name": "Microsoft",
                "change_percent": 0.0,
                "close": 400.0,
                "volume": 40_000_000,
                "exchange_code": "NASDAQ",
            },
            {
                "symbol": "JPM",
                "name": "JPMorgan",
                "change_percent": -0.8,
                "close": 150.0,
                "volume": 10_000_000,
                "exchange_code": "NYSE",
            },
            {
                "symbol": "EMPTY",
                "name": "EmptyEx",
                "change_percent": 0.2,
                "close": 20.0,
                "volume": 1_000,
                "exchange_code": "",
            },
        ]

        result = self.analyzer.analyze_daily_data(rows)

        summary = result["summary"]
        self.assertEqual(summary["total_stocks"], 4)
        self.assertEqual(summary["gainers"], 2)
        self.assertEqual(summary["losers"], 1)
        self.assertEqual(summary["unchanged"], 1)
        self.assertEqual(result["top_volume"][0]["symbol"], "AAPL")
        self.assertEqual(result["top_volume"][1]["symbol"], "MSFT")

        exchange_stats = result["exchange_statistics"]
        self.assertEqual(exchange_stats["NASDAQ"]["stock_count"], 2)
        self.assertEqual(exchange_stats["NASDAQ"]["avg_change_percent"], 0.75)
        self.assertEqual(exchange_stats["NASDAQ"]["total_volume"], 90_000_000)
        self.assertEqual(exchange_stats["NYSE"]["stock_count"], 1)
        self.assertEqual(exchange_stats["NYSE"]["avg_change_percent"], -0.8)
        self.assertIn("", exchange_stats)

    def test_analyze_without_exchange_code_omits_exchange_statistics(self):
        rows = [
            {
                "symbol": "AAPL",
                "name": "Apple",
                "change_percent": 1.0,
                "close": 180.0,
                "volume": 1_000,
            }
        ]
        result = self.analyzer.analyze_daily_data(rows)
        self.assertEqual(result["exchange_statistics"], {})

    def test_analyze_all_nan_change_percent_yields_finite_summary(self):
        """All-NaN change_percent must not write nan into summary JSON."""
        rows = [
            {
                "symbol": "AAPL",
                "name": "Apple",
                "change_percent": float("nan"),
                "close": 180.0,
                "volume": 1_000,
            },
            {
                "symbol": "MSFT",
                "name": "Microsoft",
                "change_percent": float("nan"),
                "close": 350.0,
                "volume": 2_000,
            },
        ]
        result = self.analyzer.analyze_daily_data(rows)
        summary = result["summary"]
        self.assertEqual(summary["average_change_percent"], 0.0)
        self.assertEqual(summary["max_change_percent"], 0.0)
        self.assertEqual(summary["min_change_percent"], 0.0)
        self.assertEqual(summary["total_stocks"], 2)
        self.assertEqual(summary["gainers"], 0)
        self.assertEqual(summary["losers"], 0)
        self.assertEqual(summary["unchanged"], 0)
        self.assertEqual(result["top_gainers"], [])
        self.assertEqual(result["top_losers"], [])

    def test_analyze_skips_non_finite_rows_in_leaderboards_and_counts(self):
        """NaN/inf change or volume must not rank or skew gainer/loser tallies."""
        rows = [
            {
                "symbol": "GOOD",
                "name": "Good Co",
                "change_percent": 2.5,
                "close": 100.0,
                "volume": 5_000,
            },
            {
                "symbol": "NANPCT",
                "name": "NaN Pct",
                "change_percent": float("nan"),
                "close": 50.0,
                "volume": 9_999_999,
            },
            {
                "symbol": "INFVOL",
                "name": "Inf Vol",
                "change_percent": 9.0,
                "close": 10.0,
                "volume": float("inf"),
            },
            {
                "symbol": "LOSER",
                "name": "Loser Co",
                "change_percent": -1.0,
                "close": 20.0,
                "volume": 1_000,
            },
        ]

        result = self.analyzer.analyze_daily_data(rows)
        summary = result["summary"]

        self.assertEqual(summary["total_stocks"], 4)
        self.assertEqual(summary["gainers"], 2)  # GOOD + INFVOL (finite change)
        self.assertEqual(summary["losers"], 1)
        self.assertEqual(summary["unchanged"], 0)
        # Leaderboards rank all finite-change rows (same semantics as before).
        self.assertEqual(
            [row["symbol"] for row in result["top_gainers"]],
            ["INFVOL", "GOOD", "LOSER"],
        )
        self.assertEqual(
            [row["symbol"] for row in result["top_losers"]],
            ["LOSER", "GOOD", "INFVOL"],
        )
        # Inf volume is excluded; NaN-change row still ranks by finite volume.
        self.assertEqual(
            [row["symbol"] for row in result["top_volume"]],
            ["NANPCT", "GOOD", "LOSER"],
        )
        self.assertNotIn("INFVOL", [row["symbol"] for row in result["top_volume"]])
        self.assertNotIn("NANPCT", [row["symbol"] for row in result["top_gainers"]])
        self.assertTrue(all(math.isfinite(row["change_percent"]) for row in result["top_gainers"]))
        self.assertTrue(all(math.isfinite(row["volume"]) for row in result["top_volume"]))


if __name__ == "__main__":
    unittest.main()

    def test_analyze_skips_nan_rows_in_top_lists(self):
        """Non-finite change_percent/volume must not appear in top_* lists."""

        rows = [
            {
                "symbol": "GOOD",
                "name": "Good Co",
                "change_percent": 2.5,
                "close": 100.0,
                "volume": 5_000,
            },
            {
                "symbol": "NANPCT",
                "name": "Bad Pct",
                "change_percent": float("nan"),
                "close": 50.0,
                "volume": 9_000,
            },
            {
                "symbol": "INFVOL",
                "name": "Bad Vol",
                "change_percent": -1.0,
                "close": 40.0,
                "volume": float("inf"),
            },
            {
                "symbol": "LOSER",
                "name": "Loser Co",
                "change_percent": -3.0,
                "close": 30.0,
                "volume": 1_000,
            },
        ]
        result = self.analyzer.analyze_daily_data(rows)

        gainer_symbols = [row["symbol"] for row in result["top_gainers"]]
        loser_symbols = [row["symbol"] for row in result["top_losers"]]
        volume_symbols = [row["symbol"] for row in result["top_volume"]]

        # Finite change_percent rows only (INFVOL has finite change, NaN pct excluded).
        self.assertEqual(gainer_symbols, ["GOOD", "INFVOL", "LOSER"])
        self.assertEqual(loser_symbols, ["LOSER", "INFVOL", "GOOD"])
        self.assertNotIn("NANPCT", gainer_symbols + loser_symbols)

        # Finite volume only (INFVOL excluded); NANPCT still ranks by volume.
        self.assertEqual(volume_symbols, ["NANPCT", "GOOD", "LOSER"])
        self.assertNotIn("INFVOL", volume_symbols)
        nanpct_volume = next(row for row in result["top_volume"] if row["symbol"] == "NANPCT")
        self.assertEqual(nanpct_volume["change_percent"], 0.0)
        self.assertEqual(nanpct_volume["volume"], 9_000)

        # Summary counts exclude NaN comparisons (NaN is neither >0, <0, nor ==0).
        summary = result["summary"]
        self.assertEqual(summary["total_stocks"], 4)
        self.assertEqual(summary["gainers"], 1)
        self.assertEqual(summary["losers"], 2)
        self.assertEqual(summary["unchanged"], 0)

        # Top lists must be strict-JSON serializable (no NaN/Infinity literals).
        encoded = json.dumps(result["top_gainers"] + result["top_losers"] + result["top_volume"])
        self.assertNotIn("NaN", encoded)
        self.assertNotIn("Infinity", encoded)
        for row in result["top_gainers"] + result["top_losers"] + result["top_volume"]:
            self.assertTrue(math.isfinite(float(row["change_percent"])))
            if "close" in row:
                self.assertTrue(math.isfinite(float(row["close"])))
            if "volume" in row:
                self.assertTrue(math.isfinite(float(row["volume"])))


if __name__ == "__main__":
    unittest.main()

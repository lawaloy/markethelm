"""CLI for deterministic projection backtests."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
from typing import Optional, Sequence

from ..analysis.backtesting import backtest_data_dir
from ..analysis.market_calendar import DEFAULT_CALENDAR
from ..core.logger import configure_logging


def _positive_int(value: str) -> int:
    try:
        number = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"expected a positive integer, got {value!r}") from exc
    if number < 1:
        raise argparse.ArgumentTypeError(f"expected a positive integer, got {number}")
    return number


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="markethelm backtest",
        description="Evaluate saved projections against exact exchange sessions.",
    )
    parser.add_argument(
        "--data-dir",
        type=Path,
        default=Path(os.getenv("DATA_DIR", "data")),
        help="Directory containing market_bars.sqlite (quotes + projections).",
    )
    parser.add_argument("--days", type=_positive_int, default=365)
    parser.add_argument("--horizon-sessions", type=_positive_int, default=5)
    parser.add_argument("--calendar", default=DEFAULT_CALENDAR)
    parser.add_argument(
        "--output",
        type=Path,
        help="Write the JSON report to this path instead of stdout.",
    )
    parser.add_argument(
        "--all-samples",
        action="store_true",
        help="Do not cap the sample list at 300 rows.",
    )
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    configure_logging()
    args = build_parser().parse_args(argv)
    try:
        report = backtest_data_dir(
            args.data_dir,
            days=args.days,
            horizon_sessions=args.horizon_sessions,
            calendar_name=args.calendar,
            max_samples=None if args.all_samples else 300,
        )
    except ValueError as exc:
        build_parser().error(str(exc))

    payload = json.dumps(report, indent=2, allow_nan=False) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(payload, encoding="utf-8")
    else:
        print(payload, end="")
    return 0

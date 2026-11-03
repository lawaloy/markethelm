"""Inspect and score the immutable prospective forecast ledger."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
from typing import Sequence

from src.analysis.forecast_models import PROTOCOL_VERSION
from src.analysis.forward_evaluation import forward_report
from src.core.logger import configure_logging, get_logger
from src.storage.forecast_ledger import score_pending

logger = get_logger(__name__)


def main(argv: Sequence[str] | None = None) -> int:
    configure_logging()
    parser = argparse.ArgumentParser(prog="markethelm forecasts", description=__doc__)
    parser.add_argument("command", choices=("report", "score"))
    parser.add_argument("--data-dir", type=Path, default=Path(os.getenv("DATA_DIR", "data")))
    parser.add_argument("--days", type=int, default=365)
    parser.add_argument("--protocol", default=PROTOCOL_VERSION)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(argv)
    if not 1 <= args.days <= 3650:
        parser.error("--days must be between 1 and 3650")
    try:
        if args.command == "score":
            result = {"newScores": score_pending(data_dir=args.data_dir)}
        else:
            result = forward_report(
                data_dir=args.data_dir, days=args.days, protocol_version=args.protocol
            )
        output = (json.dumps(result, indent=2, allow_nan=False, sort_keys=True) + "\n").encode(
            "utf-8"
        )
        if args.output:
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_bytes(output)
        else:
            print(output.decode("utf-8"), end="")
        return 0
    except Exception as exc:
        logger.error("Forward forecast operation unavailable: %s", type(exc).__name__)
        return 1

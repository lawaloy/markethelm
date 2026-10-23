#!/usr/bin/env python3
"""Backfill legacy market-data files into market_bars durable storage."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

try:
    from scripts import _repo_path
except ImportError:  # run as ``python scripts/<name>.py``: repo root not on sys.path yet
    import _repo_path

from src.core.logger import configure_logging
from src.storage.legacy_market_data import backfill_legacy_market_data

ROOT = _repo_path.ROOT


def main(argv: list[str] | None = None) -> int:
    configure_logging()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", type=Path, default=ROOT / "data")
    parser.add_argument(
        "--replace-existing",
        action="store_true",
        help="overwrite durable rows for dates that already exist",
    )
    args = parser.parse_args(argv)

    try:
        report = backfill_legacy_market_data(
            args.data_dir,
            replace_existing=args.replace_existing,
        )
    except Exception as exc:
        print(json.dumps({"errors": [{"error": str(exc)}]}, indent=2), file=sys.stderr)
        return 1

    print(json.dumps(report, indent=2, sort_keys=True))
    return 1 if report["errors"] else 0


if __name__ == "__main__":
    raise SystemExit(main())

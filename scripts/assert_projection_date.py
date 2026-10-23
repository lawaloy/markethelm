#!/usr/bin/env python3
"""Fail unless a run date is present in projections under ``--data-dir``."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

try:
    from scripts import _repo_path
except ImportError:  # run as ``python scripts/<name>.py``: repo root not on sys.path yet
    import _repo_path

from src.core.logger import configure_logging
from src.storage.projections_store import list_projection_dates

ROOT = _repo_path.ROOT


def main(argv: list[str] | None = None) -> int:
    """Check whether the requested run date is present in projections."""
    configure_logging()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-date", required=True, help="YYYY-MM-DD expected in projections")
    parser.add_argument("--data-dir", type=Path, default=ROOT / "data")
    parser.add_argument("--limit", type=int, default=5)
    args = parser.parse_args(argv)

    dates = list_projection_dates(data_dir=args.data_dir, limit=args.limit)
    if args.run_date not in dates:
        print(
            f"Expected run date {args.run_date!r} in projections; found {dates!r}",
            file=sys.stderr,
        )
        return 1
    print(f"OK: {args.run_date} present in projections ({len(dates)} recent dates)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

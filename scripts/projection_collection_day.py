#!/usr/bin/env python3
"""Report whether the current XNYS session has completed."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
from typing import Optional, Sequence

import pandas as pd

try:
    from scripts import _repo_path
except ImportError:  # run as ``python scripts/<name>.py``: repo root not on sys.path yet
    import _repo_path

from src.analysis.market_calendar import DEFAULT_CALENDAR, get_market_calendar
from src.core.logger import configure_logging

ROOT = _repo_path.ROOT


def session_has_completed(
    timestamp: datetime,
    calendar_name: str = DEFAULT_CALENDAR,
) -> bool:
    """Return true only during/after the close of a session on its local date."""
    if timestamp.tzinfo is None or timestamp.utcoffset() is None:
        raise ValueError("timestamp must include a timezone")
    calendar = get_market_calendar(calendar_name)
    current = pd.Timestamp(timestamp).tz_convert("UTC")
    local_date = current.tz_convert(calendar.tz).date().isoformat()
    try:
        session = calendar.date_to_session(local_date, direction="none")
    except ValueError:
        return False
    return bool(calendar.session_close(session) <= current)


def main(argv: Optional[Sequence[str]] = None) -> int:
    configure_logging()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--at",
        help="Optional timezone-aware ISO timestamp; defaults to the current UTC time.",
    )
    parser.add_argument("--calendar", default=DEFAULT_CALENDAR)
    args = parser.parse_args(argv)
    timestamp = (
        datetime.fromisoformat(args.at.replace("Z", "+00:00"))
        if args.at
        else datetime.now(timezone.utc)
    )
    print("true" if session_has_completed(timestamp, args.calendar) else "false")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

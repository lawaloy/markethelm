#!/usr/bin/env python3
"""
Set a unique PEP 440 dev version in setup.cfg for manual TestPyPI publishes.

  python scripts/dev_version.py --run-number 42   # 0.3.7 -> 0.3.8.dev42

The base version is ``setup.cfg`` [metadata] version (``X.Y.Z``). The dev version uses the
*next patch* (``X.Y.Z+1``) because ``X.Y.(Z+1).devN`` sorts before the final ``X.Y.(Z+1)``
release, and after the already-published ``X.Y.Z``. Each run number is unique, so every
manual publish adds a new TestPyPI version.

Used by ``.github/workflows/publish.yml`` (manual runs only). Run it *after*
``version_sync.py sync``: dashboard files keep the plain ``X.Y.Z`` (npm rejects PEP 440 dev
versions) and only the built package carries the dev version. Nothing is committed.
"""

from __future__ import annotations

import argparse
import configparser
import re
import sys
from pathlib import Path

_BASE_RE = re.compile(r"(\d+)\.(\d+)\.(\d+)")


def compute_dev_version(base: str, run_number: int) -> str:
    """Return ``<next-patch>.dev<run_number>`` for a base ``X.Y.Z`` version."""
    match = _BASE_RE.fullmatch(base.strip())
    if not match:
        raise ValueError(f"Invalid base version {base!r}; expected X.Y.Z")
    if run_number < 1:
        raise ValueError(f"run number must be >= 1, got {run_number}")
    major, minor, patch = (int(part) for part in match.groups())
    return f"{major}.{minor}.{patch + 1}.dev{run_number}"


def apply_dev_version(setup_cfg: Path, run_number: int) -> str:
    """Rewrite setup.cfg [metadata] version to the dev version and return it."""
    cfg = configparser.ConfigParser()
    cfg.read(setup_cfg, encoding="utf-8")
    version = compute_dev_version(cfg["metadata"]["version"], run_number)
    cfg["metadata"]["version"] = version
    with open(setup_cfg, "w", encoding="utf-8", newline="\n") as handle:
        cfg.write(handle)
    return version


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--run-number", type=int, required=True)
    parser.add_argument("--setup-cfg", type=Path, default=Path("setup.cfg"))
    args = parser.parse_args()
    try:
        version = apply_dev_version(args.setup_cfg, args.run_number)
    except (KeyError, ValueError, OSError) as exc:
        print(f"dev_version: {exc}", file=sys.stderr)
        return 2
    print("Version set to", version)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

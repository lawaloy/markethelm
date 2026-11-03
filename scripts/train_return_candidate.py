#!/usr/bin/env python3
"""Freeze a return candidate from a private archive snapshot; never train during collection."""

from __future__ import annotations

import argparse
import hashlib
import json
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Sequence

try:
    from scripts import _repo_path
except ImportError:
    import _repo_path

from scripts.projection_baseline import _input_manifest, _snapshot_inputs
from src.analysis.projection_validation import validation_samples_data_dir
from src.analysis.return_training import train_candidate
from src.core.logger import configure_logging, get_logger
from src.storage.database import database_enabled

logger = get_logger(__name__)
ROOT = _repo_path.ROOT


def main(argv: Sequence[str] | None = None) -> int:
    configure_logging()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", required=True, type=Path)
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument(
        "--frozen-at", help="Aware timestamp; provide the original value to reproduce"
    )
    args = parser.parse_args(argv)
    try:
        if database_enabled():
            raise ValueError("Training requires an offline SQLite archive")
        if not (args.data_dir / "market_bars.sqlite").is_file():
            raise ValueError("The archive database is missing")
        if args.output_dir.exists():
            raise ValueError("Refusing to replace an existing candidate bundle")
        frozen = (
            datetime.fromisoformat(args.frozen_at.replace("Z", "+00:00"))
            if args.frozen_at
            else datetime.now(timezone.utc)
        )
        if frozen.utcoffset() is None or frozen > datetime.now(timezone.utc):
            raise ValueError("Freeze timestamp must be aware and not in the future")
        with tempfile.TemporaryDirectory(prefix="markethelm-candidate-") as temporary:
            snapshot = Path(temporary)
            _snapshot_inputs(args.data_dir, snapshot)
            samples, summary = validation_samples_data_dir(snapshot)
            artifact, report = train_candidate(
                samples, frozen_at=frozen, source={"inputs": _input_manifest(snapshot)}
            )
        artifact_bytes = (
            json.dumps(artifact, indent=2, sort_keys=True, allow_nan=False) + "\n"
        ).encode("utf-8")
        report["artifactFileSha256"] = hashlib.sha256(artifact_bytes).hexdigest()
        report["sourceSummary"] = summary
        args.output_dir.mkdir(parents=True, exist_ok=False)
        (args.output_dir / "ridge_return_v1.json").write_bytes(artifact_bytes)
        (args.output_dir / "diagnostic.json").write_bytes(
            (json.dumps(report, indent=2, sort_keys=True, allow_nan=False) + "\n").encode("utf-8")
        )
        print(f"Frozen shadow candidate in {args.output_dir}")
        return 0
    except Exception as exc:
        logger.error("Candidate training unavailable: %s", type(exc).__name__)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

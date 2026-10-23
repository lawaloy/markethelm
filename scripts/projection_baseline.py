#!/usr/bin/env python3
"""Verify or intentionally update the committed projection evaluator baseline."""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import sys
import tempfile
from datetime import datetime, timezone
from difflib import unified_diff
from pathlib import Path
from typing import Optional, Sequence

try:
    from scripts import _repo_path
except ImportError:  # run as ``python scripts/<name>.py``: repo root not on sys.path yet
    import _repo_path

from src.analysis.backtesting import backtest_data_dir
from src.core.logger import configure_logging

ROOT = _repo_path.ROOT

BASELINE_DIR = ROOT / "baselines" / "projection-v1"
DATA_DIR = BASELINE_DIR / "data"
REPORT_PATH = BASELINE_DIR / "report.json"
DEFAULT_MIN_SAMPLES = 200
DEFAULT_MIN_RUN_DATES = 20
DEFAULT_MIN_SYMBOLS = 25
DEFAULT_MIN_COVERAGE_PCT = 90.0
DEFAULT_MIN_COHORT_SAMPLES = 30
DEFAULT_MIN_CONFIDENCE_COHORTS = 2


def render_report() -> str:
    """Generate canonical JSON for the committed scenario dataset."""
    report = backtest_data_dir(DATA_DIR, days=3650, max_samples=None)
    return json.dumps(report, indent=2, allow_nan=False, sort_keys=True) + "\n"


def check() -> int:
    """Return nonzero and print a compact diff when the baseline has drifted."""
    actual = render_report()
    try:
        expected = REPORT_PATH.read_text(encoding="utf-8")
    except OSError as exc:
        print(f"Unable to read projection baseline: {exc}", file=sys.stderr)
        return 2
    if actual == expected:
        print("OK: projection evaluator matches baseline v1")
        return 0
    print("Projection evaluator baseline drifted:", file=sys.stderr)
    print(
        "".join(
            unified_diff(
                expected.splitlines(keepends=True),
                actual.splitlines(keepends=True),
                fromfile=str(REPORT_PATH),
                tofile="generated projection baseline",
            )
        ),
        file=sys.stderr,
        end="",
    )
    return 1


def update() -> int:
    """Rewrite the baseline only through an explicit update command."""
    REPORT_PATH.write_text(render_report(), encoding="utf-8")
    print(f"Updated {REPORT_PATH.relative_to(ROOT).as_posix()}")
    return 0


def qualify_report(
    report: dict,
    *,
    min_samples: int = DEFAULT_MIN_SAMPLES,
    min_run_dates: int = DEFAULT_MIN_RUN_DATES,
    min_symbols: int = DEFAULT_MIN_SYMBOLS,
    min_coverage_pct: float = DEFAULT_MIN_COVERAGE_PCT,
    min_cohort_samples: int = DEFAULT_MIN_COHORT_SAMPLES,
    min_confidence_cohorts: int = DEFAULT_MIN_CONFIDENCE_COHORTS,
) -> dict:
    """Assess whether observed outcomes are safe to preserve for calibration."""
    summary = report["summary"]
    samples = report["samples"]
    sample_count = int(summary.get("sampleCount", 0))
    run_dates = {sample["runDate"] for sample in samples}
    symbols = {sample["symbol"] for sample in samples}
    eligible_confidence_cohorts = sorted(
        name
        for name, cohort in summary.get("byConfidenceBand", {}).items()
        if name != "UNKNOWN" and int(cohort.get("count", 0)) >= min_cohort_samples
    )
    failures = []
    checks = {
        "sampleCount": {"actual": sample_count, "minimum": min_samples},
        "distinctRunDates": {"actual": len(run_dates), "minimum": min_run_dates},
        "distinctSymbols": {"actual": len(symbols), "minimum": min_symbols},
        "evaluationCoveragePct": {
            "actual": summary.get("evaluationCoveragePct"),
            "minimum": min_coverage_pct,
        },
        "eligibleConfidenceCohorts": {
            "actual": len(eligible_confidence_cohorts),
            "minimum": min_confidence_cohorts,
            "cohorts": eligible_confidence_cohorts,
            "samplesPerCohort": min_cohort_samples,
        },
    }
    for name, check in checks.items():
        actual = check["actual"]
        if actual is None or actual < check["minimum"]:
            failures.append(f"{name} is {actual!r}; requires at least {check['minimum']}")
    if report.get("samplesTruncated"):
        failures.append("report samples are truncated")
    if int(summary.get("verifiedOutcomeCount", 0)) != sample_count:
        failures.append("every scored outcome must have verified previous-close provenance")
    if int(summary.get("timestampedProjectionCount", 0)) != sample_count:
        failures.append("every scored projection must have timezone-aware generation provenance")
    return {
        "schemaVersion": 1,
        "qualified": not failures,
        "checks": checks,
        "failures": failures,
    }


def observed_report(data_dir: Path, days: int) -> dict:
    return backtest_data_dir(data_dir, days=days, max_samples=None, verified_outcomes_only=True)


def assess(data_dir: Path, days: int, **thresholds: object) -> int:
    assessment = qualify_report(observed_report(data_dir, days), **thresholds)
    print(json.dumps(assessment, indent=2, allow_nan=False, sort_keys=True))
    return 0 if assessment["qualified"] else 1


def _input_manifest(data_dir: Path) -> list[dict]:
    inputs = []
    bars = data_dir / "market_bars.sqlite"
    if bars.is_file():
        content = bars.read_bytes()
        inputs.append(
            {
                "path": bars.name,
                "bytes": len(content),
                "sha256": hashlib.sha256(content).hexdigest(),
            }
        )
    return inputs


def _snapshot_inputs(data_dir: Path, snapshot_dir: Path) -> None:
    """Copy durable market_bars sidecar (bars + projections + summaries)."""
    bars = data_dir / "market_bars.sqlite"
    if bars.is_file():
        shutil.copy2(bars, snapshot_dir / bars.name)


def capture(data_dir: Path, output_dir: Path, days: int, **thresholds: object) -> int:
    """Write an immutable observed report only after qualification succeeds."""
    with tempfile.TemporaryDirectory(prefix="markethelm-baseline-") as temporary:
        snapshot_dir = Path(temporary)
        _snapshot_inputs(data_dir, snapshot_dir)
        report = observed_report(snapshot_dir, days)
        assessment = qualify_report(report, **thresholds)
        inputs = _input_manifest(snapshot_dir)
    if not assessment["qualified"]:
        print(json.dumps(assessment, indent=2, allow_nan=False, sort_keys=True))
        print("Refusing to capture an unqualified observed baseline.", file=sys.stderr)
        return 1
    output_dir.mkdir(parents=True, exist_ok=False)
    report_text = json.dumps(report, indent=2, allow_nan=False, sort_keys=True) + "\n"
    (output_dir / "report.json").write_text(report_text, encoding="utf-8")
    manifest = {
        "schemaVersion": 1,
        "capturedAt": datetime.now(timezone.utc).isoformat(),
        "sourceDirectory": data_dir.name,
        "days": days,
        "reportSha256": hashlib.sha256(report_text.encode()).hexdigest(),
        "assessment": assessment,
        "inputs": inputs,
    }
    (output_dir / "manifest.json").write_text(
        json.dumps(manifest, indent=2, allow_nan=False, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(f"Captured qualified observed baseline in {output_dir}")
    return 0


def print_qualified(path: Path) -> int:
    """Print ``true``/``false`` for assessment JSON; exit 2 when the field is not a bool."""
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        print(f"Unable to read assessment JSON: {exc}", file=sys.stderr)
        return 2
    value = payload.get("qualified") if isinstance(payload, dict) else None
    if not isinstance(value, bool):
        print(
            f"assessment.qualified must be a bool; got {type(value).__name__}: {value!r}",
            file=sys.stderr,
        )
        return 2
    print(str(value).lower())
    return 0


def main(argv: Optional[Sequence[str]] = None) -> int:
    configure_logging()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "command",
        choices=("check", "update", "assess", "capture", "print-qualified"),
    )
    parser.add_argument("--data-dir", type=Path, default=ROOT / "data")
    parser.add_argument("--days", type=int, default=365)
    parser.add_argument("--output-dir", type=Path)
    parser.add_argument(
        "--assessment",
        type=Path,
        default=ROOT / "data" / "projection-assessment.json",
        help="Assessment JSON path for print-qualified",
    )
    parser.add_argument("--min-samples", type=int, default=DEFAULT_MIN_SAMPLES)
    parser.add_argument("--min-run-dates", type=int, default=DEFAULT_MIN_RUN_DATES)
    parser.add_argument("--min-symbols", type=int, default=DEFAULT_MIN_SYMBOLS)
    parser.add_argument("--min-coverage-pct", type=float, default=DEFAULT_MIN_COVERAGE_PCT)
    parser.add_argument("--min-cohort-samples", type=int, default=DEFAULT_MIN_COHORT_SAMPLES)
    parser.add_argument(
        "--min-confidence-cohorts", type=int, default=DEFAULT_MIN_CONFIDENCE_COHORTS
    )
    args = parser.parse_args(argv)
    if args.command == "check":
        return check()
    if args.command == "update":
        return update()
    if args.command == "print-qualified":
        return print_qualified(args.assessment)
    thresholds = {
        "min_samples": args.min_samples,
        "min_run_dates": args.min_run_dates,
        "min_symbols": args.min_symbols,
        "min_coverage_pct": args.min_coverage_pct,
        "min_cohort_samples": args.min_cohort_samples,
        "min_confidence_cohorts": args.min_confidence_cohorts,
    }
    if args.command == "assess":
        return assess(args.data_dir, args.days, **thresholds)
    if args.output_dir is None:
        parser.error("capture requires --output-dir")
    return capture(args.data_dir, args.output_dir, args.days, **thresholds)


if __name__ == "__main__":
    raise SystemExit(main())

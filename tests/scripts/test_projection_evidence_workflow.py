"""Protect the scheduled forward-projection evidence collection contract."""

from __future__ import annotations

import importlib.util
from pathlib import Path

import yaml

from src.storage.market_bars import upsert_market_bars
from src.storage.projections_store import upsert_projections

REPO_ROOT = Path(__file__).resolve().parents[2]
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "schedule.yml"


def _workflow() -> str:
    return WORKFLOW.read_text(encoding="utf-8")


def test_projection_evidence_workflow_is_valid_yaml() -> None:
    """Broken multiline run blocks must fail CI before merge, not only on push."""
    parsed = yaml.safe_load(_workflow())
    assert isinstance(parsed, dict)
    assert parsed.get("name") == "Projection evidence collection"
    # PyYAML 1.1 treats bare key ``on`` as boolean True.
    triggers = parsed.get("on", parsed.get(True))
    assert isinstance(triggers, dict)
    assert "schedule" in triggers
    assert "workflow_dispatch" in triggers


def test_projection_evidence_runs_weekdays_after_xnys_close() -> None:
    workflow = _workflow()

    assert workflow.startswith("name: Projection evidence collection\n")
    assert "cron: '30 22 * * 1-5'" in workflow
    assert "group: projection-evidence-collection" in workflow
    assert "cancel-in-progress: false" in workflow
    assert "if: github.ref_name == github.event.repository.default_branch" in workflow
    assert "TZ: America/New_York" in workflow
    assert "scripts/projection_collection_day.py" in workflow
    assert workflow.count("steps.collection-day.outputs.run == 'true'") == 7


def test_projection_evidence_restores_latest_unexpired_successful_archive() -> None:
    workflow = _workflow()

    assert "actions: read" in workflow
    assert "workflow_id: 'schedule.yml'" in workflow
    assert "branch: context.payload.repository.default_branch" in workflow
    assert "status: 'success'" in workflow
    assert "artifact.name === 'projection-forward-archive'" in workflow
    assert "!artifact.expired" in workflow
    assert "uses: actions/download-artifact@v4" in workflow
    assert "path: data" in workflow
    assert "run-id: ${{ steps.previous-archive.outputs.run-id }}" in workflow
    assert "github-token: ${{ github.token }}" in workflow


def test_projection_evidence_preserves_progress_and_qualified_capture() -> None:
    workflow = _workflow()

    assert "scripts/projection_baseline.py assess" in workflow
    assert "scripts/projection_baseline.py print-qualified" in workflow
    assert "python -c" not in workflow
    assert 'echo "qualified=$qualified" >> "$GITHUB_OUTPUT"' in workflow
    assert "scripts/projection_baseline.py capture" in workflow
    assert "name: projection-forward-archive" in workflow
    assert "data/market_bars.sqlite" in workflow
    assert "data/projections_*.csv" not in workflow
    assert "data/projection-assessment.json" in workflow
    assert "name: projection-observed-baseline-${{ github.run_id }}" in workflow
    assert workflow.count("retention-days: 90") == 2


def test_projection_evidence_requires_fresh_bars_and_projection_rows() -> None:
    workflow = _workflow()

    assert 'rm -f "data/projections_${snapshot_date}.csv"' not in workflow
    assert "scripts/assert_market_bar_date.py" in workflow
    assert "--trade-date" in workflow
    assert "scripts/assert_projection_date.py" in workflow
    assert "--run-date" in workflow
    assert 'test -s "data/projections_${snapshot_date}.csv"' not in workflow


def _load_script(name: str):
    module_path = REPO_ROOT / "scripts" / name
    spec = importlib.util.spec_from_file_location(name.removesuffix(".py"), module_path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_assert_market_bar_date_script_reports_missing(tmp_path: Path) -> None:
    module = _load_script("assert_market_bar_date.py")
    assert module.main(["--trade-date", "2099-01-01", "--data-dir", str(tmp_path)]) == 1


def test_assert_projection_date_script_reports_missing(tmp_path: Path) -> None:
    module = _load_script("assert_projection_date.py")
    assert module.main(["--run-date", "2099-01-01", "--data-dir", str(tmp_path)]) == 1


def test_assert_market_bar_date_script_succeeds_when_present(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.delenv("MARKETHELM_DATABASE_URL", raising=False)

    upsert_market_bars(
        [{"symbol": "AAPL", "close": 150.0}],
        "2026-09-18",
        data_dir=tmp_path,
    )
    module = _load_script("assert_market_bar_date.py")
    assert module.main(["--trade-date", "2026-09-18", "--data-dir", str(tmp_path)]) == 0


def test_assert_projection_date_script_succeeds_when_present(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.delenv("MARKETHELM_DATABASE_URL", raising=False)

    upsert_projections(
        [{"symbol": "AAPL", "current_price": 150.0}],
        "2026-09-18",
        data_dir=tmp_path,
    )
    module = _load_script("assert_projection_date.py")
    assert module.main(["--run-date", "2026-09-18", "--data-dir", str(tmp_path)]) == 0


def test_assert_market_bar_date_fails_when_present_outside_limit(
    tmp_path: Path, monkeypatch
) -> None:
    """CI --limit is a newest-first window, not a full-store membership check."""
    monkeypatch.delenv("MARKETHELM_DATABASE_URL", raising=False)

    upsert_market_bars(
        [{"symbol": "AAPL", "close": 149.0}],
        "2026-09-17",
        data_dir=tmp_path,
    )
    upsert_market_bars(
        [{"symbol": "AAPL", "close": 150.0}],
        "2026-09-18",
        data_dir=tmp_path,
    )
    module = _load_script("assert_market_bar_date.py")
    assert (
        module.main(
            [
                "--trade-date",
                "2026-09-18",
                "--data-dir",
                str(tmp_path),
                "--limit",
                "1",
            ]
        )
        == 0
    )
    assert (
        module.main(
            [
                "--trade-date",
                "2026-09-17",
                "--data-dir",
                str(tmp_path),
                "--limit",
                "1",
            ]
        )
        == 1
    )


def test_assert_projection_date_fails_when_present_outside_limit(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.delenv("MARKETHELM_DATABASE_URL", raising=False)

    upsert_projections(
        [{"symbol": "AAPL", "current_price": 149.0}],
        "2026-09-17",
        data_dir=tmp_path,
    )
    upsert_projections(
        [{"symbol": "AAPL", "current_price": 150.0}],
        "2026-09-18",
        data_dir=tmp_path,
    )
    module = _load_script("assert_projection_date.py")
    assert (
        module.main(["--run-date", "2026-09-18", "--data-dir", str(tmp_path), "--limit", "1"]) == 0
    )
    assert (
        module.main(["--run-date", "2026-09-17", "--data-dir", str(tmp_path), "--limit", "1"]) == 1
    )


def test_print_qualified_reads_assessment_bool(tmp_path: Path) -> None:
    module_path = REPO_ROOT / "scripts" / "projection_baseline.py"
    spec = importlib.util.spec_from_file_location("projection_baseline", module_path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    path = tmp_path / "assessment.json"
    path.write_text('{"qualified": false}\n', encoding="utf-8")
    assert module.print_qualified(path) == 0
    path.write_text('{"qualified": "yes"}\n', encoding="utf-8")
    assert module.print_qualified(path) == 2

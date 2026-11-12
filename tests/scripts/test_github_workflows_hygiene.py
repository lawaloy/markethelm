"""Repo gates for GitHub Actions workflow hygiene."""

from __future__ import annotations

from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
WORKFLOWS = REPO_ROOT / ".github" / "workflows"


def _workflow_files() -> list[Path]:
    return sorted(WORKFLOWS.glob("*.yml")) + sorted(WORKFLOWS.glob("*.yaml"))


def test_workflows_contain_no_inline_python_c() -> None:
    """Logic belongs in scripts/; python -c is YAML-fragile and untested."""
    offenders: list[str] = []
    for path in _workflow_files():
        text = path.read_text(encoding="utf-8")
        if "python -c" in text or "python3 -c" in text:
            offenders.append(path.relative_to(REPO_ROOT).as_posix())
    assert offenders == [], f"inline python -c found in: {offenders}"


def test_workflows_are_valid_yaml() -> None:
    """Catch multiline/run-block YAML breakage before merge."""
    for path in _workflow_files():
        parsed = yaml.safe_load(path.read_text(encoding="utf-8"))
        assert isinstance(parsed, dict), f"{path.name} did not parse to a mapping"
        assert "jobs" in parsed, f"{path.name} missing jobs"


def test_ci_runs_actionlint_on_workflows() -> None:
    """PR CI must lint Actions YAML so schedule.yml breakage fails before merge."""
    ci = (REPO_ROOT / ".github" / "workflows" / "python-app.yml").read_text(encoding="utf-8")
    assert "download-actionlint.bash" in ci
    assert "./actionlint -color" in ci
    assert "-shellcheck=" not in ci


def test_uploaded_artifacts_have_explicit_retention() -> None:
    """New uploads must not silently inherit the repository's long default."""
    offenders: list[str] = []
    for path in _workflow_files():
        workflow = yaml.safe_load(path.read_text(encoding="utf-8"))
        for job_name, job in workflow.get("jobs", {}).items():
            if not isinstance(job, dict):
                continue
            for step in job.get("steps", []):
                if not isinstance(step, dict) or not str(step.get("uses", "")).startswith(
                    "actions/upload-artifact@"
                ):
                    continue
                retention = step.get("with", {}).get("retention-days")
                if (
                    isinstance(retention, bool)
                    or not isinstance(retention, int)
                    or not 1 <= retention <= 90
                ):
                    offenders.append(f"{path.name}:{job_name}:{step.get('name', 'upload')}")
    assert offenders == [], f"missing or invalid artifact retention: {offenders}"

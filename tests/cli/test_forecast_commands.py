"""Exercise the public prospective-report CLI without provider credentials."""

import json
import os
import subprocess
import sys

import pytest

from src.cli.forecast_commands import main


def test_packaged_cli_reports_empty_ledger_without_fetching_or_backfilling(tmp_path):
    env = {**os.environ, "MARKETHELM_DATABASE_URL": "", "FINNHUB_API_KEY": "", "OPENAI_API_KEY": ""}
    command = [
        sys.executable,
        "-m",
        "src.cli.commands",
        "forecasts",
        "report",
        "--data-dir",
        str(tmp_path),
    ]
    result = subprocess.run(command, env=env, capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr
    report = json.loads(result.stdout)
    assert report["status"] == "not_started"
    assert report["models"] == {}
    output = tmp_path / "report.json"
    result = subprocess.run(
        [*command, "--output", str(output)], env=env, capture_output=True, text=True, timeout=30
    )
    assert result.returncode == 0, result.stderr
    assert json.loads(output.read_text(encoding="utf-8"))["status"] == "not_started"


def test_packaged_cli_scores_empty_ledger_without_fetching(tmp_path):
    env = {**os.environ, "MARKETHELM_DATABASE_URL": "", "FINNHUB_API_KEY": "", "OPENAI_API_KEY": ""}
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "src.cli.commands",
            "forecasts",
            "score",
            "--data-dir",
            str(tmp_path),
        ],
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == {"newScores": 0}


def test_cli_rejects_out_of_range_lookback(tmp_path):
    with pytest.raises(SystemExit):
        main(["report", "--data-dir", str(tmp_path), "--days", "0"])


def test_cli_redacts_database_failures(tmp_path, monkeypatch, caplog):
    def fail(**kwargs):
        raise RuntimeError("postgresql://secret:password@database")

    monkeypatch.setattr("src.cli.forecast_commands.forward_report", fail)
    assert main(["report", "--data-dir", str(tmp_path)]) == 1
    assert "secret" not in caplog.text and "password" not in caplog.text

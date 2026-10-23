"""Exercise logging through real CLI and HTTP processes, without logging mocks."""

from __future__ import annotations

import json
import os
import re
import socket
import subprocess
import sys
import time
from pathlib import Path
from urllib.error import URLError
from urllib.request import Request, urlopen

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
LOG_LINE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z \[(INFO|WARNING|ERROR)\] ")


@pytest.fixture
def process_env(tmp_path):
    (tmp_path / "data").mkdir()
    config = tmp_path / "alerts.json"
    config.write_text(json.dumps({"defaults": {}, "alerts": []}), encoding="utf-8")
    return {
        **os.environ,
        "PYTHONUTF8": "1",
        "PYTHON_DOTENV_DISABLED": "1",
        "MARKETHELM_DATABASE_URL": "",
        "FINNHUB_API_KEY": "",
        "OPENAI_API_KEY": "",
        "MARKETHELM_ALERTS_CONFIG": str(config),
        "DATA_DIR": str(tmp_path / "data"),
        "MARKETHELM_LOG_DIR": str(tmp_path / "logs"),
        "MARKETHELM_LOG_LEVEL": "INFO",
    }


def run_cli(process_env, *arguments):
    result = subprocess.run(
        [sys.executable, "-X", "utf8", "-m", "src.cli.commands", *arguments],
        cwd=REPO_ROOT,
        env=process_env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=60,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    return result


def general_log(process_env):
    paths = list(Path(process_env["MARKETHELM_LOG_DIR"]).glob("markethelm_2*.log"))
    assert len(paths) == 1
    return paths[0].read_text(encoding="utf-8")


def test_real_worker_cli_logs_once_to_stderr_and_file(process_env):
    result = run_cli(process_env, "alerts", "run")
    message = "Alert check: No active watches configured."
    assert result.stdout == ""
    assert result.stderr.count(message) == 1
    line = next(line for line in result.stderr.splitlines() if message in line)
    assert LOG_LINE.match(line)
    assert "[INFO] src.alerts.alert_worker:" in line
    assert general_log(process_env).count(message) == 1
    errors = list(Path(process_env["MARKETHELM_LOG_DIR"]).glob("markethelm_errors_*.log"))
    assert len(errors) == 1
    assert errors[0].read_text(encoding="utf-8") == ""


def test_real_backtest_cli_stdout_remains_json(process_env):
    result = run_cli(process_env, "backtest", "--data-dir", process_env["DATA_DIR"])
    report = json.loads(result.stdout)
    assert report["summary"]["sampleCount"] == 0
    assert report["summary"]["horizonSessions"] == 5
    assert "[INFO]" not in result.stdout


def test_real_worker_console_threshold_keeps_file_events(process_env):
    process_env["MARKETHELM_LOG_LEVEL"] = "ERROR"
    result = run_cli(process_env, "alerts", "run")
    assert result.stdout == result.stderr == ""
    assert "Alert check: No active watches configured." in general_log(process_env)


def test_real_worker_continues_when_log_directory_is_a_file(process_env, tmp_path):
    blocked = tmp_path / "not-a-directory"
    blocked.write_text("occupied", encoding="utf-8")
    process_env["MARKETHELM_LOG_DIR"] = str(blocked)
    result = run_cli(process_env, "alerts", "run")
    assert "File logging unavailable" in result.stderr
    assert result.stderr.count("Alert check: No active watches configured.") == 1
    assert blocked.read_text(encoding="utf-8") == "occupied"


@pytest.mark.parametrize("access_log", [True, False])
def test_real_uvicorn_request_and_server_logs_share_configuration(
    process_env, tmp_path, access_log
):
    with socket.socket() as reservation:
        reservation.bind(("127.0.0.1", 0))
        port = reservation.getsockname()[1]
    command = [
        sys.executable,
        "-X",
        "utf8",
        "-m",
        "uvicorn",
        "dashboard.backend.main:app",
        "--host",
        "127.0.0.1",
        "--port",
        str(port),
    ]
    if not access_log:
        command.append("--no-access-log")
    capture_path = tmp_path / "server-output.txt"
    with capture_path.open("w", encoding="utf-8") as capture:
        process = subprocess.Popen(
            command,
            cwd=REPO_ROOT,
            env=process_env,
            stdout=capture,
            stderr=subprocess.STDOUT,
        )
        try:
            deadline = time.monotonic() + 45
            while True:
                assert process.poll() is None, capture_path.read_text(encoding="utf-8")
                try:
                    with urlopen(f"http://127.0.0.1:{port}/health", timeout=1) as response:
                        assert json.load(response)["status"] == "healthy"
                    break
                except (URLError, TimeoutError):
                    if time.monotonic() >= deadline:
                        pytest.fail(capture_path.read_text(encoding="utf-8"))
                    time.sleep(0.1)
            request_id = "logging-process-regression"
            request = Request(
                f"http://127.0.0.1:{port}/health?probe=logging-process",
                headers={"X-Request-ID": request_id},
            )
            with urlopen(request, timeout=5) as response:
                assert response.status == 200
                assert response.headers["X-Request-ID"] == request_id
                assert json.load(response)["status"] == "healthy"
            # Response sending precedes Uvicorn's access log; allow it to flush.
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                console = capture_path.read_text(encoding="utf-8")
                if not access_log or 'GET /health?probe=logging-process HTTP/1.1" 200' in console:
                    break
                time.sleep(0.05)
        finally:
            process.terminate()
            process.wait(timeout=10)
    console = capture_path.read_text(encoding="utf-8")
    assert "--- Logging error ---" not in console
    assert console.count(f"request_id={request_id}") == 1
    assert general_log(process_env).count(f"request_id={request_id}") == 1
    for line in console.splitlines():
        if "uvicorn.error:" in line or "dashboard.backend.observability:" in line:
            assert LOG_LINE.match(line), line
    assert "[INFO] uvicorn.error: Started server process" in console
    access_message = 'GET /health?probe=logging-process HTTP/1.1" 200'
    assert console.count(access_message) == int(access_log)
    assert general_log(process_env).count(access_message) == int(access_log)

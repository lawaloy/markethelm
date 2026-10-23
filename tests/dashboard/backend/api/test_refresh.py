"""Tests for dashboard refresh process orchestration."""

import asyncio
import subprocess
from pathlib import Path
from typing import Optional
from unittest.mock import MagicMock

import pytest
from fastapi import BackgroundTasks, HTTPException
from fastapi.testclient import TestClient

from dashboard.backend.api import refresh
from dashboard.backend.main import app
from src.storage.database import init_database


class FakeProcess:
    """Minimal subprocess stand-in for refresh lifecycle tests."""

    def __init__(
        self,
        returncode: int = 0,
        *,
        running: bool = False,
        hang_on_wait: bool = False,
    ) -> None:
        self.returncode = returncode
        self.wait_timeouts: list[int] = []
        self.terminated = False
        self.killed = False
        self._running = running
        self._hang_on_wait = hang_on_wait

    def poll(self) -> Optional[int]:
        if self._running:
            return None
        return self.returncode

    def wait(self, timeout: int) -> int:
        self.wait_timeouts.append(timeout)
        if self._hang_on_wait:
            raise subprocess.TimeoutExpired(cmd="fake-refresh", timeout=timeout)
        self._running = False
        return self.returncode

    def terminate(self) -> None:
        self.terminated = True
        if not self._hang_on_wait:
            self._running = False

    def kill(self) -> None:
        self.killed = True
        self._running = False
        # After kill, a subsequent wait() should succeed.
        self._hang_on_wait = False


def reset_refresh_state() -> None:
    refresh.refresh_status.update(
        {
            "is_running": False,
            "last_refresh": None,
            "last_status": "idle",
            "progress": "Idle.",
        }
    )
    refresh._refresh_process = None
    refresh._refresh_cancel_event.clear()


def test_run_daily_tracker_discards_child_output_to_prevent_deadlock(monkeypatch) -> None:
    reset_refresh_state()
    fake_process = FakeProcess(returncode=0)
    popen = MagicMock(return_value=fake_process)

    monkeypatch.setattr(refresh.subprocess, "Popen", popen)
    monkeypatch.setenv("REFRESH_TOP_N", "0")
    monkeypatch.setattr("src.alerts.alert_worker.run_check_once", lambda: {"triggered": 0})

    refresh.run_daily_tracker()

    _, kwargs = popen.call_args
    assert kwargs["stdout"] == subprocess.DEVNULL
    assert kwargs["stderr"] == subprocess.DEVNULL
    assert kwargs["stdout"] != subprocess.PIPE
    assert kwargs["stderr"] != subprocess.PIPE
    assert fake_process.wait_timeouts == [30]
    assert refresh.refresh_status["last_status"] == "success"
    assert refresh.refresh_status["is_running"] is False


def test_run_daily_tracker_keeps_success_when_post_refresh_alerts_fail(
    monkeypatch,
) -> None:
    """A failed post-refresh alert check must not flip a successful fetch to error."""
    reset_refresh_state()
    fake_process = FakeProcess(returncode=0)
    popen = MagicMock(return_value=fake_process)

    def boom() -> dict:
        raise RuntimeError("alert worker down")

    monkeypatch.setattr(refresh.subprocess, "Popen", popen)
    monkeypatch.setenv("REFRESH_TOP_N", "0")
    monkeypatch.setattr("src.alerts.alert_worker.run_check_once", boom)

    refresh.run_daily_tracker()

    assert refresh.refresh_status["last_status"] == "success"
    assert refresh.refresh_status["is_running"] is False
    assert "successfully" in refresh.refresh_status["progress"].lower()
    assert refresh._refresh_process is None


def test_run_daily_tracker_honors_cancel_event_without_alert_check(monkeypatch) -> None:
    """Cancel must stop the child and skip the post-refresh alert worker."""
    reset_refresh_state()
    fake_process = FakeProcess(returncode=0, running=True)
    alert_check = MagicMock(return_value={"triggered": 0})

    def popen_and_cancel(*_args, **_kwargs):
        # Simulate /refresh/cancel after the child starts (tracker clears the event first).
        refresh._refresh_cancel_event.set()
        return fake_process

    monkeypatch.setattr(refresh.subprocess, "Popen", MagicMock(side_effect=popen_and_cancel))
    monkeypatch.setenv("REFRESH_TOP_N", "0")
    monkeypatch.setattr("src.alerts.alert_worker.run_check_once", alert_check)
    monkeypatch.setattr(refresh.time, "sleep", lambda _seconds: None)

    refresh.run_daily_tracker()

    assert fake_process.terminated is True
    assert fake_process.wait_timeouts == [30]
    assert refresh.refresh_status["last_status"] == "cancelled"
    assert refresh.refresh_status["progress"] == "Refresh cancelled."
    assert refresh.refresh_status["is_running"] is False
    assert refresh._refresh_process is None
    alert_check.assert_not_called()


def test_trigger_refresh_marks_running_before_worker_thread_starts(monkeypatch) -> None:
    reset_refresh_state()
    created_threads = []

    class FakeThread:
        def __init__(self, *, target, daemon: bool) -> None:
            self.target = target
            self.daemon = daemon
            self.started = False
            created_threads.append(self)

        def start(self) -> None:
            self.started = True

    monkeypatch.setenv("FINNHUB_API_KEY", "test-key")
    monkeypatch.setattr(refresh.threading, "Thread", FakeThread)

    response = asyncio.run(refresh.trigger_refresh(BackgroundTasks()))

    assert response.status == "started"
    assert response.is_running is True
    assert refresh.refresh_status["last_status"] == "running"
    assert refresh.refresh_status["progress"] == "Starting markethelm..."
    assert refresh.refresh_status["is_running"] is True
    assert len(created_threads) == 1
    assert created_threads[0].daemon is True
    assert created_threads[0].started is True


def test_trigger_refresh_reports_already_running() -> None:
    reset_refresh_state()
    refresh.refresh_status["is_running"] = True

    response = asyncio.run(refresh.trigger_refresh(BackgroundTasks()))

    assert response.status == "already_running"
    assert response.is_running is True
    assert "already in progress" in response.message


def test_trigger_refresh_rejects_without_api_key_or_env_file(monkeypatch) -> None:
    """Hosted refresh must fail closed when Finnhub credentials are missing."""
    reset_refresh_state()
    # Stub the credential gate so a developer's real .env cannot make this flaky.
    monkeypatch.setattr(refresh, "_has_refresh_credentials", lambda _root: False)

    response = asyncio.run(refresh.trigger_refresh(BackgroundTasks()))

    assert response.status == "error"
    assert response.is_running is False
    assert "API key" in response.message
    assert refresh.refresh_status["last_status"] == "error"
    assert refresh.refresh_status["is_running"] is False


def test_has_refresh_credentials_checks_env_var_and_dotenv(tmp_path, monkeypatch) -> None:
    """Credential detection must ignore ambient process/.env state outside tmp_path."""
    monkeypatch.delenv("FINNHUB_API_KEY", raising=False)
    assert refresh._has_refresh_credentials(tmp_path) is False

    monkeypatch.setenv("FINNHUB_API_KEY", "test-key")
    assert refresh._has_refresh_credentials(tmp_path) is True

    monkeypatch.delenv("FINNHUB_API_KEY", raising=False)
    (tmp_path / ".env").write_text("FINNHUB_API_KEY=from-file\n", encoding="utf-8")
    assert refresh._has_refresh_credentials(tmp_path) is True


def test_has_refresh_credentials_soft_fails_unreadable_dotenv(tmp_path, monkeypatch) -> None:
    """An unreadable project .env must not 500 the Fetch New credential gate."""

    monkeypatch.delenv("FINNHUB_API_KEY", raising=False)
    env_path = tmp_path / ".env"
    env_path.write_text("FINNHUB_API_KEY=from-file\n", encoding="utf-8")

    original = Path.read_text

    def maybe_boom(self, *args, **kwargs):
        if self == env_path:
            raise OSError("permission denied")
        return original(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", maybe_boom)
    assert refresh._has_refresh_credentials(tmp_path) is False


def test_has_refresh_credentials_rejects_empty_or_unrelated_dotenv(tmp_path, monkeypatch) -> None:
    """A present .env without a usable Finnhub key must not false-start refresh."""
    monkeypatch.delenv("FINNHUB_API_KEY", raising=False)

    (tmp_path / ".env").write_text("", encoding="utf-8")
    assert refresh._has_refresh_credentials(tmp_path) is False

    (tmp_path / ".env").write_text("OPENAI_API_KEY=sk-demo\n", encoding="utf-8")
    assert refresh._has_refresh_credentials(tmp_path) is False

    (tmp_path / ".env").write_text("FINNHUB_API_KEY=\n", encoding="utf-8")
    assert refresh._has_refresh_credentials(tmp_path) is False

    (tmp_path / ".env").write_text('FINNHUB_API_KEY=""\n', encoding="utf-8")
    assert refresh._has_refresh_credentials(tmp_path) is False

    (tmp_path / ".env").write_text("export FINNHUB_API_KEY='real-key'\n", encoding="utf-8")
    assert refresh._has_refresh_credentials(tmp_path) is True

    monkeypatch.setenv("FINNHUB_API_KEY", "   ")
    (tmp_path / ".env").write_text("OPENAI_API_KEY=sk-demo\n", encoding="utf-8")
    assert refresh._has_refresh_credentials(tmp_path) is False


def test_run_daily_tracker_timeout_terminates_without_alert_check(monkeypatch) -> None:
    """Wall-clock timeout must stop a hung child and skip post-refresh alerts."""
    reset_refresh_state()
    fake_process = FakeProcess(returncode=0, running=True)
    alert_check = MagicMock(return_value={"triggered": 0})
    clock = {"t": 1_000.0}

    monkeypatch.setattr(refresh.subprocess, "Popen", MagicMock(return_value=fake_process))
    monkeypatch.setenv("REFRESH_TOP_N", "0")
    monkeypatch.setenv("REFRESH_TIMEOUT_SECONDS", "5")
    monkeypatch.setattr("src.alerts.alert_worker.run_check_once", alert_check)
    monkeypatch.setattr(refresh.time, "time", lambda: clock["t"])
    monkeypatch.setattr(
        refresh.time, "sleep", lambda _seconds: clock.__setitem__("t", clock["t"] + 10)
    )

    refresh.run_daily_tracker()

    assert fake_process.terminated is True
    assert fake_process.wait_timeouts == [30]
    assert refresh.refresh_status["last_status"] == "timeout"
    assert "timed out" in refresh.refresh_status["progress"].lower()
    assert refresh.refresh_status["is_running"] is False
    alert_check.assert_not_called()


def test_run_daily_tracker_kills_child_when_wait_hangs_after_timeout(monkeypatch) -> None:
    reset_refresh_state()
    fake_process = FakeProcess(returncode=0, running=True, hang_on_wait=True)
    clock = {"t": 1_000.0}

    monkeypatch.setattr(refresh.subprocess, "Popen", MagicMock(return_value=fake_process))
    monkeypatch.setenv("REFRESH_TOP_N", "0")
    monkeypatch.setenv("REFRESH_TIMEOUT_SECONDS", "1")
    monkeypatch.setattr("src.alerts.alert_worker.run_check_once", lambda: {"triggered": 0})
    monkeypatch.setattr(refresh.time, "time", lambda: clock["t"])
    monkeypatch.setattr(
        refresh.time, "sleep", lambda _seconds: clock.__setitem__("t", clock["t"] + 5)
    )

    refresh.run_daily_tracker()

    assert fake_process.terminated is True
    assert fake_process.killed is True
    assert fake_process.wait_timeouts == [30, 10]
    assert refresh.refresh_status["last_status"] == "timeout"
    assert refresh.refresh_status["is_running"] is False


def test_run_daily_tracker_marks_error_on_nonzero_returncode(monkeypatch) -> None:
    reset_refresh_state()
    fake_process = FakeProcess(returncode=2)
    alert_check = MagicMock(return_value={"triggered": 0})

    monkeypatch.setattr(refresh.subprocess, "Popen", MagicMock(return_value=fake_process))
    monkeypatch.setenv("REFRESH_TOP_N", "0")
    monkeypatch.setattr("src.alerts.alert_worker.run_check_once", alert_check)

    refresh.run_daily_tracker()

    assert refresh.refresh_status["last_status"] == "error"
    assert refresh.refresh_status["is_running"] is False
    alert_check.assert_not_called()


def test_run_daily_tracker_clamps_small_and_invalid_top_n(monkeypatch) -> None:
    reset_refresh_state()
    fake_process = FakeProcess(returncode=0)
    popen = MagicMock(return_value=fake_process)

    monkeypatch.setattr(refresh.subprocess, "Popen", popen)
    monkeypatch.setenv("REFRESH_TOP_N", "3")
    monkeypatch.setattr("src.alerts.alert_worker.run_check_once", lambda: {"triggered": 0})

    refresh.run_daily_tracker()
    command = popen.call_args.args[0]
    assert "--top-n" in command
    assert command[command.index("--top-n") + 1] == "10"

    reset_refresh_state()
    popen.reset_mock()
    monkeypatch.setenv("REFRESH_TOP_N", "not-a-number")
    refresh.run_daily_tracker()
    command = popen.call_args.args[0]
    assert command[command.index("--top-n") + 1] == "10"


def test_run_daily_tracker_falls_back_on_invalid_timeout_env(monkeypatch) -> None:
    """Invalid REFRESH_TIMEOUT_SECONDS must not crash after spawn or orphan the child."""
    reset_refresh_state()
    fake_process = FakeProcess(returncode=0)
    popen = MagicMock(return_value=fake_process)
    sleep_calls: list[float] = []

    def fake_sleep(seconds: float) -> None:
        sleep_calls.append(seconds)
        # Advance wall clock past the default 600s timeout on first poll loop.
        if len(sleep_calls) == 1:
            fake_process._running = False

    monkeypatch.setattr(refresh.subprocess, "Popen", popen)
    monkeypatch.setenv("REFRESH_TOP_N", "0")
    monkeypatch.setenv("REFRESH_TIMEOUT_SECONDS", "not-an-int")
    monkeypatch.setattr(refresh.time, "sleep", fake_sleep)
    monkeypatch.setattr("src.alerts.alert_worker.run_check_once", lambda: {"triggered": 0})

    # Make poll loop see a finished process immediately.
    fake_process._running = False
    refresh.run_daily_tracker()

    assert popen.called
    assert refresh.refresh_status["last_status"] == "success"
    assert refresh.refresh_status["is_running"] is False
    assert refresh._refresh_process is None


def test_run_daily_tracker_invalid_timeout_still_manages_running_child(monkeypatch) -> None:
    """Even with a bad timeout env, the spawned process must remain tracked until exit."""
    reset_refresh_state()
    fake_process = FakeProcess(returncode=0, running=True)
    popen = MagicMock(return_value=fake_process)
    polls = {"n": 0}

    original_poll = fake_process.poll

    def poll_then_finish():
        polls["n"] += 1
        if polls["n"] >= 2:
            fake_process._running = False
        return original_poll()

    fake_process.poll = poll_then_finish

    monkeypatch.setattr(refresh.subprocess, "Popen", popen)
    monkeypatch.setenv("REFRESH_TOP_N", "0")
    monkeypatch.setenv("REFRESH_TIMEOUT_SECONDS", "abc")
    monkeypatch.setattr(refresh.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr("src.alerts.alert_worker.run_check_once", lambda: {"triggered": 0})

    refresh.run_daily_tracker()

    assert popen.called
    assert polls["n"] >= 2
    assert refresh.refresh_status["last_status"] == "success"
    assert refresh._refresh_process is None


def test_cancel_refresh_terminates_running_process() -> None:
    reset_refresh_state()
    fake_process = FakeProcess(returncode=0, running=True)
    refresh.refresh_status["is_running"] = True
    refresh.refresh_status["last_status"] = "running"
    refresh._refresh_process = fake_process

    response = asyncio.run(refresh.cancel_refresh())

    assert refresh._refresh_cancel_event.is_set()
    assert fake_process.terminated is True
    assert fake_process.killed is False
    assert fake_process.wait_timeouts == [10]
    # Single-flight stays held until the worker finally clears it.
    assert response.is_running is True
    assert response.last_status == "cancelled"
    assert refresh.refresh_status["is_running"] is True
    assert refresh.refresh_status["progress"] == "Cancelling refresh..."


def test_cancel_refresh_kills_process_when_terminate_hangs() -> None:
    reset_refresh_state()
    fake_process = FakeProcess(returncode=0, running=True, hang_on_wait=True)
    refresh.refresh_status["is_running"] = True
    refresh._refresh_process = fake_process

    response = asyncio.run(refresh.cancel_refresh())

    assert fake_process.terminated is True
    assert fake_process.killed is True
    assert fake_process.wait_timeouts == [10]
    assert response.is_running is True
    assert response.last_status == "cancelled"
    assert refresh.refresh_status["is_running"] is True


def test_cancel_refresh_rejects_when_idle() -> None:
    reset_refresh_state()

    with pytest.raises(HTTPException) as exc_info:
        asyncio.run(refresh.cancel_refresh())

    assert exc_info.value.status_code == 400
    assert exc_info.value.detail == "No refresh in progress."


def test_refresh_mutations_require_auth_in_database_mode(tmp_path, monkeypatch) -> None:
    reset_refresh_state()
    db_path = tmp_path / "refresh-auth.db"
    monkeypatch.setenv("MARKETHELM_DATABASE_URL", f"sqlite:///{db_path.as_posix()}")
    monkeypatch.setenv("MARKETHELM_AUTH_SECRET", "test-secret-min-16-chars")

    init_database()
    client = TestClient(app)

    assert client.post("/api/refresh").status_code == 401
    assert client.post("/api/refresh/cancel").status_code == 401
    assert client.get("/api/refresh/status").status_code == 401


def test_run_daily_tracker_omits_no_screener_when_disabled(monkeypatch) -> None:
    """REFRESH_NO_SCREENER=false must leave the screener enabled in the child CLI."""
    reset_refresh_state()
    fake_process = FakeProcess(returncode=0)
    popen = MagicMock(return_value=fake_process)

    monkeypatch.setattr(refresh.subprocess, "Popen", popen)
    monkeypatch.setenv("REFRESH_TOP_N", "0")
    monkeypatch.setenv("REFRESH_NO_SCREENER", "false")
    monkeypatch.setenv("REFRESH_MAX_WORKERS", "8")
    monkeypatch.setattr("src.alerts.alert_worker.run_check_once", lambda: {"triggered": 0})

    refresh.run_daily_tracker()

    args, kwargs = popen.call_args
    command = args[0]
    assert "--no-screener" not in command
    # REFRESH_MAX_WORKERS=8 must clamp to data_fetcher's 1..4 band before spawn.
    assert kwargs["env"]["STOCK_FETCH_MAX_WORKERS"] == "4"
    assert refresh.refresh_status["last_status"] == "success"


def test_run_daily_tracker_defaults_to_no_screener_and_four_workers(monkeypatch) -> None:
    reset_refresh_state()
    fake_process = FakeProcess(returncode=0)
    popen = MagicMock(return_value=fake_process)

    monkeypatch.setattr(refresh.subprocess, "Popen", popen)
    monkeypatch.setenv("REFRESH_TOP_N", "0")
    monkeypatch.delenv("REFRESH_NO_SCREENER", raising=False)
    monkeypatch.delenv("REFRESH_MAX_WORKERS", raising=False)
    monkeypatch.setattr("src.alerts.alert_worker.run_check_once", lambda: {"triggered": 0})

    refresh.run_daily_tracker()

    args, kwargs = popen.call_args
    assert "--no-screener" in args[0]
    assert kwargs["env"]["STOCK_FETCH_MAX_WORKERS"] == "4"

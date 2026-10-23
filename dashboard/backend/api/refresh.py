"""
Refresh API endpoints — trigger the daily MarketHelm run to fetch new data.
"""

import os
import subprocess
import sys
import threading
import time
from datetime import datetime
from pathlib import Path

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException
from pydantic import BaseModel

from dashboard.backend.auth import require_user_id
from src.core.logger import get_logger

logger = get_logger(__name__)

router = APIRouter()

# Hard ceilings so a poisoned REFRESH_* env cannot monopolize Finnhub/CPU
# or hold the global refresh lock indefinitely.
MAX_REFRESH_TOP_N = 500
MAX_REFRESH_TIMEOUT_SECONDS = 3600
MAX_REFRESH_WORKERS = 4
DEFAULT_REFRESH_TOP_N = 10
DEFAULT_REFRESH_TIMEOUT_SECONDS = 600
DEFAULT_REFRESH_WORKERS = 4

# Track refresh status
refresh_status = {
    "is_running": False,
    "last_refresh": None,
    "last_status": "idle",
    "progress": "Idle.",
}

_refresh_process: subprocess.Popen | None = None
_refresh_cancel_event = threading.Event()
# Serializes the already-running check + flag set + thread spawn so two
# overlapping POSTs cannot each start a Finnhub-burning child.
_refresh_start_lock = threading.Lock()


def _resolve_refresh_top_n() -> int:
    """Parse REFRESH_TOP_N with lower floor (when >0) and hard upper ceiling.

    ``0`` remains the explicit unlimited opt-in. Negative / unparseable values
    must not collapse into unlimited via ``max(0, n)`` — that silently drops
    ``--top-n`` and fans out across the full index.
    """
    top_n_value = os.getenv("REFRESH_TOP_N", str(DEFAULT_REFRESH_TOP_N))
    try:
        top_n = int(top_n_value)
    except (TypeError, ValueError):
        top_n = DEFAULT_REFRESH_TOP_N
    if top_n < 0:
        top_n = DEFAULT_REFRESH_TOP_N
    elif top_n > 0:
        top_n = max(DEFAULT_REFRESH_TOP_N, top_n)  # At least 10 stocks when using limit
    return min(MAX_REFRESH_TOP_N, top_n)


def _resolve_refresh_timeout_seconds() -> int:
    """Parse REFRESH_TIMEOUT_SECONDS with min 1s and hard upper ceiling."""
    timeout_raw = os.getenv("REFRESH_TIMEOUT_SECONDS", str(DEFAULT_REFRESH_TIMEOUT_SECONDS))
    try:
        max_seconds = max(1, int(timeout_raw))
    except (TypeError, ValueError):
        max_seconds = DEFAULT_REFRESH_TIMEOUT_SECONDS
    return min(MAX_REFRESH_TIMEOUT_SECONDS, max_seconds)


def _resolve_refresh_max_workers() -> int:
    """Parse REFRESH_MAX_WORKERS clamped to data_fetcher's 1..4 worker band."""
    raw = os.getenv("REFRESH_MAX_WORKERS", str(DEFAULT_REFRESH_WORKERS))
    try:
        workers = int(raw)
    except (TypeError, ValueError):
        workers = DEFAULT_REFRESH_WORKERS
    return max(1, min(MAX_REFRESH_WORKERS, workers))


def _finnhub_key_from_dotenv(project_root: Path) -> str | None:
    """Return a non-empty FINNHUB_API_KEY from project .env, if present.

    Presence of an unrelated/empty ``.env`` must not count as credentials —
    that false-starts refresh, holds the global lock, then fails in the child.
    """
    env_path = project_root / ".env"
    if not env_path.is_file():
        return None
    try:
        text = env_path.read_text(encoding="utf-8")
    except OSError:
        return None
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].strip()
        if "=" not in line:
            continue
        key, _, value = line.partition("=")
        if key.strip() != "FINNHUB_API_KEY":
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        value = value.strip()
        return value or None
    return None


def _has_refresh_credentials(project_root: Path) -> bool:
    """True when a non-empty Finnhub key is available via env or project .env."""
    env_key = (os.getenv("FINNHUB_API_KEY") or "").strip()
    if env_key:
        return True
    return bool(_finnhub_key_from_dotenv(project_root))


class RefreshResponse(BaseModel):
    status: str
    message: str
    last_refresh: str | None
    is_running: bool


class RefreshStatusResponse(BaseModel):
    is_running: bool
    last_refresh: str | None
    last_status: str | None
    progress: str | None


def run_daily_tracker():
    """Run the MarketHelm CLI (`markethelm`) in a separate process."""
    global _refresh_process
    try:
        refresh_status["is_running"] = True
        refresh_status["progress"] = "Starting markethelm..."
        refresh_status["last_status"] = "running"
        _refresh_cancel_event.clear()

        # Repo checkout: run top-level main.py. Pip install: run same CLI as console_scripts.
        project_root = Path(__file__).parent.parent.parent.parent
        main_script = project_root / "main.py"
        if main_script.is_file():
            command = [sys.executable, str(main_script)]
        else:
            command = [sys.executable, "-m", "src.cli.commands"]

        # Run markethelm (default to top 10 for faster refresh; hard-capped).
        top_n = _resolve_refresh_top_n()
        if top_n:
            command.extend(["--top-n", str(top_n)])

        no_screener = os.getenv("REFRESH_NO_SCREENER", "1").lower() in {"1", "true", "yes"}
        if no_screener:
            command.append("--no-screener")

        refresh_status["progress"] = "Refreshing..."

        env = os.environ.copy()
        env["STOCK_FETCH_MAX_WORKERS"] = str(_resolve_refresh_max_workers())

        # Parse timeout before spawn so a bad env var cannot orphan a running child
        # (ValueError used to trip the outer except after Popen and clear _refresh_process).
        max_seconds = _resolve_refresh_timeout_seconds()

        # Do not use PIPE for stdout/stderr: the tracker logs heavily to the console.
        # Unread PIPE buffers deadlock the child once full (parent only drains in communicate()).
        _refresh_process = subprocess.Popen(
            command,
            cwd=str(project_root),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            env=env,
        )

        start_time = time.time()

        while True:
            if _refresh_cancel_event.is_set():
                refresh_status["last_status"] = "cancelled"
                refresh_status["progress"] = "Refresh cancelled."
                if _refresh_process.poll() is None:
                    _refresh_process.terminate()
                break

            if _refresh_process.poll() is not None:
                break

            elapsed = int(time.time() - start_time)

            refresh_status["progress"] = "Refreshing..."

            if elapsed >= max_seconds:
                refresh_status["last_status"] = "timeout"
                refresh_status["progress"] = "Refresh timed out. Please try again."
                _refresh_process.terminate()
                break

            time.sleep(2)

        try:
            _refresh_process.wait(timeout=30)
        except subprocess.TimeoutExpired:
            _refresh_process.kill()
            _refresh_process.wait(timeout=10)

        if _refresh_cancel_event.is_set():
            refresh_status["last_status"] = "cancelled"
            refresh_status["progress"] = "Refresh cancelled."
            return

        if refresh_status["last_status"] == "timeout":
            return

        if _refresh_process.returncode == 0:
            refresh_status["last_status"] = "success"
            refresh_status["last_refresh"] = datetime.now().isoformat()
            refresh_status["progress"] = "Data refresh completed successfully!"
            try:
                from src.alerts.alert_worker import run_check_once

                result = run_check_once()
                if result.get("triggered", 0) > 0:
                    logger.info("Alerts triggered after refresh: %s", result["triggered"])
            except Exception as exc:
                logger.warning("Post-refresh alert check failed: %s", exc)
        else:
            refresh_status["last_status"] = "error"
            refresh_status["progress"] = "Refresh failed. Please try again."
    except Exception:
        refresh_status["last_status"] = "error"
        refresh_status["progress"] = "Refresh failed. Please try again."
    finally:
        _refresh_process = None
        refresh_status["is_running"] = False


@router.post(
    "/refresh",
    response_model=RefreshResponse,
    dependencies=[Depends(require_user_id)],
)
async def trigger_refresh(background_tasks: BackgroundTasks):
    """
    Trigger a data refresh (daily run) to fetch fresh data.

    This will:
    1. Run the tracker (`main.py` or `python -m src.cli.commands`)
    2. Fetch latest data from Finnhub API
    3. Generate new projections
    4. Save updated CSV/JSON files
    5. Dashboard will automatically show new data
    """
    project_root = Path(__file__).parent.parent.parent.parent
    with _refresh_start_lock:
        if refresh_status["is_running"]:
            return RefreshResponse(
                status="already_running",
                message="Data refresh is already in progress. Please wait.",
                last_refresh=refresh_status.get("last_refresh"),
                is_running=True,
            )

        if not _has_refresh_credentials(project_root):
            refresh_status["last_status"] = "error"
            refresh_status["progress"] = "Refresh failed. Please check your API key configuration."
            return RefreshResponse(
                status="error",
                message="Refresh failed. Please check your API key configuration.",
                last_refresh=refresh_status.get("last_refresh"),
                is_running=False,
            )

        refresh_status["last_status"] = "running"
        refresh_status["progress"] = "Starting markethelm..."
        refresh_status["is_running"] = True

        # Start refresh in background
        thread = threading.Thread(target=run_daily_tracker, daemon=True)
        thread.start()

    return RefreshResponse(
        status="started",
        message="Latest data will load when ready.",
        last_refresh=refresh_status.get("last_refresh"),
        is_running=True,
    )


@router.get(
    "/refresh/status",
    response_model=RefreshStatusResponse,
    dependencies=[Depends(require_user_id)],
)
async def get_refresh_status():
    """Get the current status of data refresh.

    Hosted mode requires auth so anonymous clients cannot observe global refresh
    progress. File mode (``require_user_id`` → ``None``) stays open.
    """
    if not refresh_status.get("is_running") and not refresh_status.get("last_status"):
        refresh_status["last_status"] = "idle"
        refresh_status["progress"] = "Idle."
    return RefreshStatusResponse(
        is_running=refresh_status["is_running"],
        last_refresh=refresh_status.get("last_refresh"),
        last_status=refresh_status.get("last_status"),
        progress=refresh_status.get("progress"),
    )


@router.post(
    "/refresh/cancel",
    response_model=RefreshStatusResponse,
    dependencies=[Depends(require_user_id)],
)
async def cancel_refresh():
    """Cancel the current refresh job if running."""
    if not refresh_status["is_running"]:
        raise HTTPException(status_code=400, detail="No refresh in progress.")

    refresh_status["progress"] = "Cancelling refresh..."
    refresh_status["last_status"] = "cancelled"
    _refresh_cancel_event.set()

    if _refresh_process and _refresh_process.poll() is None:
        _refresh_process.terminate()
        try:
            _refresh_process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            _refresh_process.kill()

    # Leave is_running True until run_daily_tracker's finally clears it.
    # Clearing here opens a window where POST /refresh can spawn a second
    # Finnhub-burning child while the cancelled worker is still tearing down.
    return RefreshStatusResponse(
        is_running=refresh_status["is_running"],
        last_refresh=refresh_status.get("last_refresh"),
        last_status=refresh_status.get("last_status"),
        progress=refresh_status.get("progress"),
    )

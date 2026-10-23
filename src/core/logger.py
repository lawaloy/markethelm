"""Shared logging API for the CLI, API, workers, and repository scripts.

Modules use ``get_logger(__name__)``. Process entry points call
``configure_logging()`` once, after loading their environment.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

LOG_FORMAT = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
_LOCK = threading.RLock()
_HANDLER_MARKER = "_markethelm_handler"


def get_logger(name: str = "markethelm") -> logging.Logger:
    """Return a standard logger without adding handlers or touching the filesystem.

    Application debug records can reach the debug file while console handlers
    apply the configured minimum level. Third-party loggers retain their levels.
    """
    logger = logging.getLogger(name)
    if logger.level == logging.NOTSET:
        logger.setLevel(logging.DEBUG)
    return logger


def configure_logging(
    *,
    level: str | int | None = None,
    log_dir: str | Path | None = None,
    force: bool = False,
) -> None:
    """Configure shared handlers once; preserve handlers owned by other tools.

    ``MARKETHELM_LOG_LEVEL`` controls console verbosity (default INFO).
    ``MARKETHELM_LOG_DIR`` selects the file directory (default logs); an empty
    string disables files. File creation is best effort on read-only hosts.
    ``force`` closes and replaces only our handlers, for explicit reconfiguration.
    """
    raw_level = level if level is not None else os.getenv("MARKETHELM_LOG_LEVEL", "INFO")
    if isinstance(raw_level, str):
        resolved_level = logging.getLevelNamesMapping().get(raw_level.strip().upper())
    else:
        resolved_level = raw_level
    if not isinstance(resolved_level, int):
        raise ValueError(f"Invalid logging level: {raw_level!r}")
    directory = log_dir if log_dir is not None else os.getenv("MARKETHELM_LOG_DIR", "logs")

    with _LOCK:
        root = logging.getLogger()
        owned = [handler for handler in root.handlers if getattr(handler, _HANDLER_MARKER, False)]
        if owned and not force:
            return
        for handler in owned:
            root.removeHandler(handler)
            handler.close()

        root.setLevel(resolved_level)
        formatter = logging.Formatter(LOG_FORMAT, datefmt="%Y-%m-%dT%H:%M:%SZ")
        formatter.converter = time.gmtime

        def attach(handler: logging.Handler, minimum: int) -> None:
            handler.setLevel(minimum)
            handler.setFormatter(formatter)
            setattr(handler, _HANDLER_MARKER, True)
            root.addHandler(handler)

        attach(logging.StreamHandler(), resolved_level)
        # Uvicorn's CLI configures its own console handlers before importing us.
        # Route server records through the same handlers as application records.
        for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
            server_logger = logging.getLogger(name)
            if (
                name == "uvicorn.access"
                and not server_logger.handlers
                and not server_logger.propagate
            ):
                # Respect Uvicorn's explicit --no-access-log setting.
                continue
            for handler in list(server_logger.handlers):
                server_logger.removeHandler(handler)
                handler.close()
            server_logger.propagate = True
            server_logger.setLevel(resolved_level)
        if not str(directory).strip():
            return
        path = Path(directory)
        try:
            path.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            get_logger(__name__).warning("File logging unavailable for %s: %s", path, exc)
            return

        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        for filename, minimum in (
            (f"markethelm_{today}.log", logging.DEBUG),
            (f"markethelm_errors_{today}.log", logging.ERROR),
        ):
            try:
                handler = logging.FileHandler(path / filename, encoding="utf-8")
            except OSError as exc:
                get_logger(__name__).warning("File logging unavailable for %s: %s", filename, exc)
                continue
            attach(handler, minimum)


def setup_logger(name: str = "markethelm", log_dir: str = "logs") -> logging.Logger:
    """Compatibility entry point; new modules should use ``get_logger`` instead."""
    configure_logging(log_dir=log_dir)
    return get_logger(name)

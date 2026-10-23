"""Behavioral and repository-wide checks for the shared logging contract."""

import ast
import logging
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

import pytest

from src.core.logger import configure_logging, get_logger, setup_logger


def owned_handlers():
    return [
        handler
        for handler in logging.getLogger().handlers
        if getattr(handler, "_markethelm_handler", False)
    ]


@pytest.fixture(autouse=True)
def isolated_logging():
    root = logging.getLogger()
    original_handlers = list(root.handlers)
    original_level = root.level
    server_state = [
        (logger, list(logger.handlers), logger.level, logger.propagate)
        for logger in (
            logging.getLogger(name) for name in ("uvicorn", "uvicorn.error", "uvicorn.access")
        )
    ]
    for handler in owned_handlers():
        root.removeHandler(handler)
    yield
    for handler in owned_handlers():
        root.removeHandler(handler)
        handler.close()
    root.handlers[:] = original_handlers
    root.setLevel(original_level)
    for logger, handlers, level, propagate in server_state:
        logger.handlers[:] = handlers
        logger.setLevel(level)
        logger.propagate = propagate


def test_get_logger_has_no_configuration_side_effects(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    before = list(logging.getLogger().handlers)
    logger = get_logger("test.shared.no_io")
    assert logger is get_logger("test.shared.no_io")
    assert logger.handlers == []
    assert logging.getLogger().handlers == before
    assert list(tmp_path.iterdir()) == []


def test_get_logger_preserves_explicit_module_level():
    logger = get_logger("test.shared.override")
    logger.setLevel(logging.WARNING)
    assert get_logger("test.shared.override").level == logging.WARNING


def test_console_files_levels_format_and_exception_tracebacks(tmp_path, capsys):
    configure_logging(log_dir=tmp_path / "nested" / "logs")
    logger = get_logger("test.shared.output")
    logger.debug("debug details")
    logger.info("fetched %s symbols", 3)
    try:
        raise ValueError("provider failure")
    except ValueError:
        logger.exception("Fetch failed")

    console = capsys.readouterr().err
    assert "debug details" not in console
    assert "[INFO] test.shared.output: fetched 3 symbols" in console
    assert "Z [INFO]" in console
    assert "Traceback" in console
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    general = (tmp_path / "nested" / "logs" / f"markethelm_{today}.log").read_text(encoding="utf-8")
    errors = (tmp_path / "nested" / "logs" / f"markethelm_errors_{today}.log").read_text(
        encoding="utf-8"
    )
    assert "debug details" in general
    assert "fetched 3 symbols" in general
    assert "fetched 3 symbols" not in errors
    assert "ValueError: provider failure" in errors


def test_configuration_is_idempotent_across_modules_and_threads(tmp_path, capsys):
    with ThreadPoolExecutor(max_workers=8) as executor:
        list(executor.map(lambda _: configure_logging(log_dir=tmp_path), range(16)))
    assert len(owned_handlers()) == 3
    get_logger("src.alerts.test").info("worker event")
    get_logger("dashboard.backend.test").info("API event")
    console = capsys.readouterr().err
    assert console.count("worker event") == 1
    assert console.count("API event") == 1


def test_force_closes_only_owned_handlers_and_preserves_external_capture(tmp_path):
    external = logging.NullHandler()
    root = logging.getLogger()
    root.addHandler(external)
    try:
        configure_logging(log_dir=tmp_path)
        old_files = [
            handler for handler in owned_handlers() if isinstance(handler, logging.FileHandler)
        ]
        configure_logging(log_dir="", level="WARNING", force=True)
        assert len(owned_handlers()) == 1
        assert all(handler.stream is None for handler in old_files)
        assert external in root.handlers
    finally:
        root.removeHandler(external)


@pytest.mark.parametrize("failure", ["mkdir", "file"])
def test_unwritable_files_keep_console_logging(tmp_path, capsys, failure):
    target = (
        "src.core.logger.Path.mkdir"
        if failure == "mkdir"
        else "src.core.logger.logging.FileHandler"
    )
    with patch(target, side_effect=OSError("read-only")):
        configure_logging(log_dir=tmp_path)
    get_logger("test.shared.readonly").error("still available")
    assert len(owned_handlers()) == 1
    assert "still available" in capsys.readouterr().err


def test_environment_controls_levels_and_console_only_mode(monkeypatch, capsys):
    monkeypatch.setenv("MARKETHELM_LOG_LEVEL", "warning")
    monkeypatch.setenv("MARKETHELM_LOG_DIR", "")
    configure_logging()
    get_logger("test.shared.env").info("hidden")
    get_logger("test.shared.env").warning("visible")
    console = capsys.readouterr().err
    assert "hidden" not in console
    assert "visible" in console
    assert len(owned_handlers()) == 1


def test_invalid_level_fails_before_replacing_handlers(tmp_path):
    configure_logging(log_dir=tmp_path)
    before = owned_handlers()
    with pytest.raises(ValueError, match="Invalid logging level"):
        configure_logging(level="not-a-level", force=True)
    assert owned_handlers() == before


def test_legacy_setup_logger_uses_shared_configuration(tmp_path):
    first = setup_logger("legacy.first", str(tmp_path))
    second = setup_logger("legacy.second", str(tmp_path))
    assert isinstance(first, logging.Logger)
    assert first.handlers == second.handlers == []
    assert len(owned_handlers()) == 3


def test_uvicorn_records_use_shared_handlers_without_duplicates(capsys):
    server = logging.getLogger("uvicorn")
    server.addHandler(logging.StreamHandler())
    server.propagate = False
    configure_logging(log_dir="")
    logging.getLogger("uvicorn.error").info("server started")
    assert server.handlers == []
    assert capsys.readouterr().err.count("server started") == 1


def test_configuration_respects_disabled_uvicorn_access_logs(capsys):
    server = logging.getLogger("uvicorn.access")
    server.handlers.clear()
    server.propagate = False
    configure_logging(log_dir="")
    server.info("disabled access event")
    assert "disabled access event" not in capsys.readouterr().err
    assert not server.propagate


def test_project_modules_use_the_shared_logging_api():
    """Keep new modules from silently reintroducing independent logging setups."""
    root = Path(__file__).resolve().parents[2]
    violations = []
    for directory in ("src", "dashboard/backend", "scripts"):
        for path in (root / directory).rglob("*.py"):
            if path == root / "src/core/logger.py":
                continue
            tree = ast.parse(path.read_text(encoding="utf-8-sig"), filename=str(path))
            for node in ast.walk(tree):
                if isinstance(node, (ast.Import, ast.ImportFrom)):
                    modules = (
                        [item.name for item in node.names]
                        if isinstance(node, ast.Import)
                        else [node.module or ""]
                    )
                    if any(name == "logging" or name.startswith("logging.") for name in modules):
                        violations.append(str(path.relative_to(root)))
                if (
                    isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Name)
                    and node.func.id == "setup_logger"
                ):
                    violations.append(str(path.relative_to(root)))
    assert not violations, f"Use src.core.logger instead of independent logging: {violations}"

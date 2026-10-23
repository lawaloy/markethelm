"""Import placement and fresh-process startup regressions."""

from __future__ import annotations

import ast
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]


class ImportPlacementVisitor(ast.NodeVisitor):
    def __init__(self):
        self.depth = 0
        self.violations = []

    def visit_FunctionDef(self, node):
        self.depth += 1
        self.generic_visit(node)
        self.depth -= 1

    visit_AsyncFunctionDef = visit_FunctionDef
    visit_ClassDef = visit_FunctionDef

    def visit_Import(self, node):
        if self.depth:
            self.violations.append(node.lineno)

    visit_ImportFrom = visit_Import


def test_python_imports_are_at_module_scope():
    files = list(ROOT.glob("*.py"))
    for directory in ("src", "dashboard/backend", "scripts", "tests", "e2e/scripts"):
        files.extend((ROOT / directory).rglob("*.py"))
    violations = []
    for path in files:
        visitor = ImportPlacementVisitor()
        visitor.visit(ast.parse(path.read_text(encoding="utf-8-sig")))
        violations.extend(f"{path.relative_to(ROOT)}:{line}" for line in visitor.violations)
    assert not violations, "Move imports to module scope: " + ", ".join(violations)


@pytest.fixture
def import_process_env(tmp_path):
    return {
        **os.environ,
        "PYTHON_DOTENV_DISABLED": "1",
        "FINNHUB_API_KEY": "",
        "OPENAI_API_KEY": "",
        "MARKETHELM_DATABASE_URL": "",
        "MARKETHELM_LOG_DIR": str(tmp_path / "logs"),
        "DATA_DIR": str(tmp_path / "data"),
    }


@pytest.mark.parametrize(
    "module",
    [
        "src.storage.database",
        "src.storage.projections_store",
        "src.alerts.alert_paths",
        "src.alerts.alert_worker",
        "src.cli.commands",
        "dashboard.backend.main",
    ],
)
def test_module_imports_in_a_fresh_process(module, import_process_env):
    result = subprocess.run(
        [sys.executable, "-c", f"import {module}"],
        cwd=ROOT,
        env=import_process_env,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert "partially initialized module" not in result.stderr


def test_backend_imports_when_started_from_its_own_directory(import_process_env):
    result = subprocess.run(
        [sys.executable, "-c", 'import main; assert main.app.title == "MarketHelm API"'],
        cwd=ROOT / "dashboard/backend",
        env=import_process_env,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert result.returncode == 0, result.stderr


def test_optional_packages_do_not_break_sqlite_or_demo_mode(import_process_env, tmp_path):
    code = """
import importlib.abc
import os
import sys
from pathlib import Path

class MissingOptionalPackages(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path, target=None):
        if fullname.split('.')[0] in {'openai', 'psycopg', 'pytickersymbols'}:
            raise ModuleNotFoundError(fullname)

os.environ.pop('OPENAI_API_KEY', None)
sys.meta_path.insert(0, MissingOptionalPackages())
from src.analysis.ai_summarizer import AISummarizer
from src.services.index_fetcher import IndexFetcher
from src.storage.database import _connect_postgresql, get_connection, init_database
from src.utils.company_names import resolve_company_name

os.environ['MARKETHELM_DATABASE_URL'] = 'sqlite:///' + str(Path(sys.argv[1]) / 'app.sqlite')
init_database()
with get_connection() as connection:
    row = connection.execute('SELECT COUNT(*) AS count FROM schema_migrations').fetchone()
    assert row['count'] > 0
assert not AISummarizer().enabled
assert resolve_company_name('AAPL') == 'AAPL'
assert not IndexFetcher(cache_dir=str(Path(sys.argv[1]) / 'cache')).package_available
try:
    _connect_postgresql()
except RuntimeError as error:
    assert 'requires Psycopg' in str(error)
else:
    raise AssertionError('PostgreSQL must reject an unavailable driver')
"""
    result = subprocess.run(
        [sys.executable, "-c", code, str(tmp_path)],
        cwd=ROOT,
        env=import_process_env,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert result.returncode == 0, result.stderr

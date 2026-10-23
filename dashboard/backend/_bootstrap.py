"""Prepare source imports, environment, and logging before application imports."""

from pathlib import Path

try:
    from dashboard.backend._source_path import REPO_ROOT
except ModuleNotFoundError:  # Direct execution from dashboard/backend.
    from _source_path import REPO_ROOT

from dotenv import load_dotenv

from src.core.logger import configure_logging

load_dotenv()
if REPO_ROOT is not None:
    load_dotenv(REPO_ROOT / ".env")
_user_env = Path.home() / ".markethelm" / ".env"
if _user_env.is_file():
    load_dotenv(_user_env, override=True)
configure_logging()

"""Shared environment loading without depending on CLI commands."""

from pathlib import Path

from dotenv import load_dotenv


def load_environment(user_env: Path | None = None) -> None:
    """Load project settings, then the user's overrides when present."""
    load_dotenv()
    path = user_env if user_env is not None else Path.home() / ".markethelm" / ".env"
    if path.is_file():
        load_dotenv(path, override=True)

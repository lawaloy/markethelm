"""
MarketHelm — main entry point (convenience wrapper for the CLI).

This is a convenience wrapper for the CLI interface.
- CLI presentation: src/cli/commands.py
- Core workflow logic: src/workflows/tracker.py

You can also run directly:
  python -m src.cli.commands     # CLI interface
  python -m src.workflows.tracker  # Direct workflow (programmatic)
"""

from src.cli import commands


def run() -> int:
    """Run the CLI convenience entry point."""

    return commands.main()


if __name__ == "__main__":
    raise SystemExit(run())

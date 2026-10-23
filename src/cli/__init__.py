"""CLI interface; defer command imports until an exported function is requested."""

from importlib import import_module

__all__ = ["main", "display_results"]


def __getattr__(name: str):
    """Keep public exports without importing commands before ``python -m`` runs it."""
    if name in __all__:
        return getattr(import_module(".commands", __name__), name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")

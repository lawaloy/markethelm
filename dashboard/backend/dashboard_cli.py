"""Console entry: `markethelm-web` — serves API + bundled SPA."""

import os

import uvicorn


def parse_port(raw: str | None, default: int = 8000) -> int:
    """Parse PORT; invalid or out-of-range values fall back to default."""
    try:
        port = int(raw if raw is not None and raw != "" else default)
    except (TypeError, ValueError):
        return default
    if not 1 <= port <= 65535:
        return default
    return port


def main() -> None:
    host = os.getenv("HOST", "0.0.0.0")
    port = parse_port(os.getenv("PORT"))
    uvicorn.run(
        "dashboard.backend.main:app",
        host=host,
        port=port,
        reload=os.getenv("UVICORN_RELOAD", "").lower() in {"1", "true", "yes"},
    )

"""
FastAPI backend for the MarketHelm dashboard.
"""

# Logging, sys.path and .env must be set up before the imports below (see _bootstrap).
try:
    from dashboard.backend import _bootstrap
except ImportError:  # run as a script from dashboard/backend: repo root not on sys.path yet
    import _bootstrap

import os
import threading
from contextlib import asynccontextmanager
from pathlib import Path
from typing import List, Optional

from fastapi import Depends, FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

from dashboard.backend.api import alerts, auth, history, market, projections, refresh, stocks
from dashboard.backend.api.market import get_market_summary
from dashboard.backend.auth import require_user_id
from dashboard.backend.observability import ObservabilityMiddleware, prometheus_metrics
from dashboard.backend.rate_limit import RateLimitMiddleware
from src.core.logger import get_logger

logger = get_logger(__name__)

# Source-checkout root found by _bootstrap (None for a pip install).
REPO_ROOT = _bootstrap.REPO_ROOT


def _coerce_startup_triggered(raw) -> int:
    """Coerce startup triggered count; Inf/NaN must not log as a real trigger."""
    import math

    if isinstance(raw, bool):
        return int(raw)
    if isinstance(raw, float) and not math.isfinite(raw):
        return 0
    try:
        return int(raw or 0)
    except (TypeError, ValueError, OverflowError):
        return 0


def _startup_alert_check() -> None:
    try:
        from src.alerts.alert_worker import run_check_once

        result = run_check_once()
        triggered = _coerce_startup_triggered(result.get("triggered", 0))
        if triggered:
            logger.info("Startup alert check triggered %s watch(es)", triggered)
    except Exception as exc:
        logger.warning("Startup alert check failed: %s", exc)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    try:
        from src.storage.database import init_database

        init_database()
    except Exception as exc:
        logger.warning("Database init skipped: %s", exc)
    threading.Thread(target=_startup_alert_check, daemon=True).start()
    yield


app = FastAPI(
    title="MarketHelm API",
    description="API for stock market data, projections, and recommendations",
    version="0.3.7",
    lifespan=lifespan,
)

# CORS configuration for local development
DEFAULT_CORS_ORIGINS = [
    "http://localhost:3000",
    "http://127.0.0.1:3000",
    "http://localhost:3001",
    "http://127.0.0.1:3001",
    "http://localhost:3002",
    "http://localhost:3003",
    "http://localhost:3004",
    "http://localhost:5173",
    "http://127.0.0.1:5173",
]


def parse_cors_origins(
    raw: str,
    defaults: Optional[List[str]] = None,
) -> List[str]:
    """Parse CORS_ORIGINS for credentialed middleware.

    Wildcards, non-http(s) schemes, and control/whitespace-tainted values are
    rejected so a bad deploy env cannot broaden browser credential access.
    Falls back to defaults when nothing valid remains.
    """
    fallback = list(defaults) if defaults is not None else list(DEFAULT_CORS_ORIGINS)
    accepted: List[str] = []
    for part in str(raw or "").split(","):
        origin = part.strip()
        if not origin:
            continue
        if origin == "*":
            logger.warning(
                "Ignoring CORS origin %r — wildcard is incompatible with " "allow_credentials=True",
                origin,
            )
            continue
        if any(ch.isspace() or ord(ch) < 32 for ch in origin):
            logger.warning(
                "Ignoring CORS origin with whitespace/control characters: %r",
                origin,
            )
            continue
        lower = origin.lower()
        if not (lower.startswith("http://") or lower.startswith("https://")):
            logger.warning(
                "Ignoring CORS origin %r — only http(s) origins are allowed",
                origin,
            )
            continue
        accepted.append(origin)
    return accepted or fallback


origins = parse_cors_origins(os.getenv("CORS_ORIGINS", ""))

app.add_middleware(RateLimitMiddleware)
app.add_middleware(ObservabilityMiddleware)
app.add_middleware(
    CORSMiddleware,
    allow_origins=origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Include routers
app.include_router(market.router, prefix="/api/market", tags=["Market"])
app.include_router(projections.router, prefix="/api/projections", tags=["Projections"])
app.include_router(stocks.router, prefix="/api/stocks", tags=["Stocks"])
app.include_router(refresh.router, prefix="/api", tags=["Refresh"])
app.include_router(history.router, prefix="/api/history", tags=["History"])
app.include_router(alerts.router, prefix="/api/alerts", tags=["Alerts"])
app.include_router(auth.router, prefix="/api/auth", tags=["Auth"])


@app.get("/health")
async def health():
    """Health check endpoint"""
    return {"status": "healthy"}


@app.get("/health/live")
async def health_live():
    return {"status": "healthy", "service": "api"}


@app.get("/health/ready")
async def health_ready():
    from src.storage.database import database_enabled

    if not database_enabled():
        return {"status": "ready", "database": "disabled"}
    from src.storage.health import database_health, latest_worker_heartbeat

    database = database_health()
    payload = {
        "status": "ready" if database["ok"] else "not_ready",
        "database": database,
        "worker": None,
    }
    if database["ok"]:
        try:
            payload["worker"] = latest_worker_heartbeat()
        except Exception:
            payload["worker"] = None
    if not database["ok"]:
        return JSONResponse(payload, status_code=503)
    return payload


@app.get("/health/worker")
async def health_worker():
    from src.storage.database import database_enabled

    if not database_enabled():
        return {"status": "disabled"}
    from src.alerts.alert_worker import resolve_interval_seconds
    from src.storage.health import worker_health

    health = worker_health(stale_after_seconds=resolve_interval_seconds() * 2 + 30)
    payload = {"status": "healthy" if health["ok"] else "unhealthy", **health}
    if not health["ok"]:
        return JSONResponse(payload, status_code=503)
    return payload


@app.get("/metrics", include_in_schema=False)
async def metrics():
    return PlainTextResponse(prometheus_metrics(), media_type="text/plain; version=0.0.4")


@app.get("/api/data-info")
async def data_info(_user_id: Optional[str] = Depends(require_user_id)):
    """Data status: path, latest date, and whether we need to fetch for the most recent trading day.

    Hosted mode requires auth so anonymous clients cannot read data_dir / fetch
    readiness. File mode (``require_user_id`` → ``None``) stays open.
    """
    from dashboard.backend.services.data_loader import get_data_loader, get_most_recent_trading_day

    try:
        loader = get_data_loader()
        target_trading_day = get_most_recent_trading_day()
        return {
            "data_dir": str(loader.data_dir),
            "latest_date": loader.get_latest_date(),
            "target_trading_day": target_trading_day,
            "needs_fetch": loader.needs_fetch_for_latest_trading_day(),
            "available_dates": loader.get_available_dates()[:5],
        }
    except (ValueError, OSError):
        # Unreadable data dirs / glob failures during status probes must map to
        # 404 so App autofetch does not treat them as hard 500 boot failures.
        raise HTTPException(status_code=404, detail="No data available.")


@app.get("/api/summary")
async def api_summary():
    """Market summary (AI or demo)."""
    return await get_market_summary()


_STATIC_DIR = Path(__file__).resolve().parent / "static"
_INDEX = _STATIC_DIR / "index.html"
_ASSETS_DIR = _STATIC_DIR / "assets"


class SpaFallbackMiddleware(BaseHTTPMiddleware):
    """Return index.html for unknown GET routes so React Router deep links work."""

    async def dispatch(self, request: Request, call_next) -> Response:
        response = await call_next(request)
        if response.status_code != 404 or request.method != "GET" or not _INDEX.is_file():
            return response
        path = request.url.path
        if path.startswith("/api/") or path.startswith("/assets/"):
            return response
        accept = request.headers.get("accept", "*/*")
        wants_html = path == "/" or "text/html" in accept or "*/*" in accept
        if wants_html:
            return FileResponse(_INDEX)
        return response


if _STATIC_DIR.is_dir() and _INDEX.is_file():
    if _ASSETS_DIR.is_dir():
        app.mount("/assets", StaticFiles(directory=_ASSETS_DIR), name="spa-assets")

    @app.get("/", include_in_schema=False)
    async def spa_index():
        return FileResponse(_INDEX)

    app.add_middleware(SpaFallbackMiddleware)
else:

    @app.get("/")
    async def root():
        """Health check when SPA bundle is not present (e.g. dev without frontend build)."""
        return {
            "status": "healthy",
            "service": "MarketHelm API",
            "version": "0.3.7",
            "spa": False,
        }


if __name__ == "__main__":
    import uvicorn

    reload = os.getenv("UVICORN_RELOAD", "").lower() in {"1", "true", "yes"}
    uvicorn.run(
        "dashboard.backend.main:app", host="0.0.0.0", port=8000, reload=reload, log_config=None
    )

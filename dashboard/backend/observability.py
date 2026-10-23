"""Low-dependency HTTP request metrics and correlation IDs."""

from __future__ import annotations

import threading
import time
import uuid
from collections import Counter

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request

from src.core.logger import get_logger

logger = get_logger(__name__)
_lock = threading.Lock()
_requests = Counter()
_duration = Counter()


class ObservabilityMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        request_id = request.headers.get("x-request-id", "")
        if not request_id or len(request_id) > 128 or any(ord(c) < 32 for c in request_id):
            request_id = uuid.uuid4().hex
        started = time.perf_counter()
        response = await call_next(request)
        elapsed = time.perf_counter() - started
        route = request.scope.get("route")
        path = getattr(route, "path", "unmatched")
        key = (request.method, path, response.status_code)
        with _lock:
            _requests[key] += 1
            _duration[(request.method, path)] += elapsed
        response.headers["X-Request-ID"] = request_id
        logger.info(
            "request method=%s path=%s status=%s duration_ms=%.1f request_id=%s",
            request.method,
            path,
            response.status_code,
            elapsed * 1000,
            request_id,
        )
        return response


def prometheus_metrics() -> str:
    lines = [
        "# HELP markethelm_http_requests_total HTTP requests.",
        "# TYPE markethelm_http_requests_total counter",
    ]
    with _lock:
        for (method, path, status), value in sorted(_requests.items()):
            lines.append(
                f'markethelm_http_requests_total{{method="{method}",path="{path}",'
                f'status="{status}"}} {value}'
            )
        lines.extend(
            [
                "# HELP markethelm_http_request_duration_seconds_sum HTTP request duration.",
                "# TYPE markethelm_http_request_duration_seconds_sum counter",
            ]
        )
        for (method, path), value in sorted(_duration.items()):
            lines.append(
                f'markethelm_http_request_duration_seconds_sum{{method="{method}",path="{path}"}} '
                f"{value:.6f}"
            )
    return "\n".join(lines) + "\n"

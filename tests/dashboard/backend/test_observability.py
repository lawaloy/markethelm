import asyncio
import re

from fastapi.testclient import TestClient
from starlette.requests import Request
from starlette.responses import Response

from dashboard.backend.main import app
from dashboard.backend.observability import ObservabilityMiddleware
from src.storage.database import LATEST_SCHEMA_VERSION, init_database


def test_liveness_readiness_and_metrics_file_mode(monkeypatch):
    monkeypatch.delenv("MARKETHELM_DATABASE_URL", raising=False)
    client = TestClient(app)
    live = client.get("/health/live", headers={"X-Request-ID": "probe-123"})
    assert live.status_code == 200
    assert live.headers["X-Request-ID"] == "probe-123"
    assert client.get("/health/ready").json()["status"] == "ready"
    metrics = client.get("/metrics")
    assert metrics.status_code == 200
    assert "markethelm_http_requests_total" in metrics.text


def test_readiness_reports_database_schema(tmp_path, monkeypatch):
    monkeypatch.setenv(
        "MARKETHELM_DATABASE_URL", f"sqlite:///{(tmp_path / 'health.db').as_posix()}"
    )

    init_database()
    response = TestClient(app).get("/health/ready")
    assert response.status_code == 200
    assert response.json()["database"]["schema_version"] == LATEST_SCHEMA_VERSION


_HEX_REQUEST_ID = re.compile(r"^[0-9a-f]{32}$")


def _echo_request_id(raw: str) -> str:
    """Run ObservabilityMiddleware.dispatch with a raw X-Request-ID value."""

    async def dummy_app(scope, receive, send):
        raise AssertionError("ASGI app should not be invoked")

    scope = {
        "type": "http",
        "method": "GET",
        "path": "/health/live",
        "raw_path": b"/health/live",
        "query_string": b"",
        "headers": [(b"x-request-id", raw.encode("latin-1"))],
        "client": ("127.0.0.1", 1234),
        "server": ("testserver", 80),
        "scheme": "http",
        "http_version": "1.1",
    }
    request = Request(scope)
    middleware = ObservabilityMiddleware(dummy_app)

    async def call_next(_request):
        return Response(status_code=200)

    response = asyncio.run(middleware.dispatch(request, call_next))
    return response.headers["x-request-id"]


def test_control_character_request_id_is_replaced() -> None:
    """CR/LF in X-Request-ID must not be reflected into response headers or logs."""
    echoed = _echo_request_id("ok\r\ninjected")
    assert echoed != "ok\r\ninjected"
    assert "\r" not in echoed
    assert "\n" not in echoed
    assert _HEX_REQUEST_ID.match(echoed)


def test_oversized_request_id_is_replaced() -> None:
    oversized = "a" * 129
    echoed = _echo_request_id(oversized)
    assert echoed != oversized
    assert len(echoed) == 32
    assert _HEX_REQUEST_ID.match(echoed)


def test_valid_request_id_is_preserved() -> None:
    assert _echo_request_id("probe-123") == "probe-123"
    assert _echo_request_id("a" * 128) == "a" * 128

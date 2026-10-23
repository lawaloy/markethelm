"""CORS preflight must not spend API rate-limit buckets.

CORSMiddleware is added last, so it is the outermost stack frame. A browser
sign-in issues OPTIONS /api/auth/login before POST. If RateLimitMiddleware
ran first, each dashboard XHR would burn two global slots and a limit-1
window would 429 the real login after a single preflight.
"""

from fastapi.testclient import TestClient
from starlette.requests import Request

import dashboard.backend.rate_limit as rate_limit
from dashboard.backend.main import app
from dashboard.backend.rate_limit import RateLimitRule, check_rate_limits
from src.storage.database import init_database


def _api_request(path: str, method: str = "OPTIONS") -> Request:
    return Request(
        {
            "type": "http",
            "method": method,
            "path": path,
            "raw_path": path.encode("ascii"),
            "query_string": b"",
            "headers": [],
            "client": ("203.0.113.5", 1234),
            "server": ("testserver", 80),
            "scheme": "http",
        }
    )


def test_options_consumes_global_bucket_when_limiter_runs(monkeypatch) -> None:
    """OPTIONS is an API method; skipping it in check_rate_limits would hide
    a middleware-order regression and allow an OPTIONS flood."""
    monkeypatch.delenv("MARKETHELM_DATABASE_URL", raising=False)
    monkeypatch.setenv("MARKETHELM_RATE_LIMIT_ENABLED", "true")
    monkeypatch.setattr(rate_limit, "_memory_counters", rate_limit._MemoryCounters())
    rules = (RateLimitRule("api-global", 1, 60),)
    now = 1_700_000_000
    request = _api_request("/api/auth/login", method="OPTIONS")

    first = check_rate_limits(request, now=now, rules=rules)
    second = check_rate_limits(request, now=now, rules=rules)

    assert first is not None and first.allowed is True
    assert first.remaining == 0
    assert second is not None and second.allowed is False
    assert second.limit == 1


def test_cors_preflight_does_not_consume_global_login_budget(tmp_path, monkeypatch) -> None:
    """Two browser preflights must leave the limit-1 global slot for the POST.

    CORSMiddleware short-circuits OPTIONS + Access-Control-Request-Method
    without calling RateLimitMiddleware. Swapping add_middleware order would
    make the first real login 429 after a single CORS handshake.
    """
    monkeypatch.setenv(
        "MARKETHELM_DATABASE_URL",
        f"sqlite:///{(tmp_path / 'cors-preflight-limits.db').as_posix()}",
    )
    monkeypatch.setenv("MARKETHELM_AUTH_SECRET", "test-secret-min-16-chars")
    monkeypatch.setenv("MARKETHELM_RATE_LIMIT_ENABLED", "true")
    monkeypatch.setenv("MARKETHELM_RATE_LIMIT_GLOBAL", "1")
    monkeypatch.setenv("MARKETHELM_RATE_LIMIT_LOGIN", "1000")
    monkeypatch.setenv("MARKETHELM_RATE_LIMIT_REGISTER", "1000")
    monkeypatch.setenv("MARKETHELM_RATE_LIMIT_AUTH_EMAIL", "1000")
    monkeypatch.setenv("MARKETHELM_RATE_LIMIT_EXPENSIVE", "1000")

    init_database()

    client = TestClient(app)
    preflight_headers = {
        "Origin": "http://localhost:3000",
        "Access-Control-Request-Method": "POST",
        "Access-Control-Request-Headers": "content-type",
    }

    first_preflight = client.options("/api/auth/login", headers=preflight_headers)
    second_preflight = client.options("/api/auth/login", headers=preflight_headers)

    assert first_preflight.status_code == 200
    assert second_preflight.status_code == 200
    assert first_preflight.headers.get("access-control-allow-origin") == ("http://localhost:3000")
    assert "x-ratelimit-limit" not in first_preflight.headers
    assert "x-ratelimit-limit" not in second_preflight.headers

    login = client.post(
        "/api/auth/login",
        json={"email": "cors-preflight@example.com", "password": "password123"},
    )
    assert login.status_code == 401
    assert login.json()["detail"] == "Invalid email or password."

    blocked = client.post(
        "/api/auth/login",
        json={"email": "cors-preflight@example.com", "password": "password123"},
    )
    assert blocked.status_code == 429
    assert blocked.json() == {"detail": "Too many requests."}
    assert int(blocked.headers["retry-after"]) >= 1

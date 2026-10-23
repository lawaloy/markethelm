"""CORS must expose rate-limit errors to the dashboard Origin.

CORSMiddleware is outermost, so credentialed POSTs that RateLimitMiddleware
rejects still pick up Access-Control-Allow-Origin. If RateLimit were moved
outside CORS *and* OPTIONS were skipped to keep preflights free, #572 would
still pass while the SPA could not read 429 JSON (opaque CORS failure).

A bare OPTIONS (no Access-Control-Request-Method) is not a preflight and
must still consume the global bucket — otherwise an OPTIONS flood bypasses
the limiter that check_rate_limits already applies in unit tests.
"""

from fastapi.testclient import TestClient

from dashboard.backend.main import app
from src.storage.database import init_database

DASHBOARD_ORIGIN = "http://localhost:3000"


def _limited_client(tmp_path, monkeypatch, db_name: str) -> TestClient:
    monkeypatch.setenv(
        "MARKETHELM_DATABASE_URL",
        f"sqlite:///{(tmp_path / db_name).as_posix()}",
    )
    monkeypatch.setenv("MARKETHELM_AUTH_SECRET", "test-secret-min-16-chars")
    monkeypatch.setenv("MARKETHELM_RATE_LIMIT_ENABLED", "true")
    monkeypatch.setenv("MARKETHELM_RATE_LIMIT_GLOBAL", "1")
    monkeypatch.setenv("MARKETHELM_RATE_LIMIT_LOGIN", "1000")
    monkeypatch.setenv("MARKETHELM_RATE_LIMIT_REGISTER", "1000")
    monkeypatch.setenv("MARKETHELM_RATE_LIMIT_AUTH_EMAIL", "1000")
    monkeypatch.setenv("MARKETHELM_RATE_LIMIT_EXPENSIVE", "1000")

    init_database()

    return TestClient(app)


def test_rate_limited_login_exposes_cors_headers_to_browser_origin(tmp_path, monkeypatch) -> None:
    """A limit-1 429 must remain readable cross-origin by the Vite dashboard.

    #572 POSTs without Origin, so CORSMiddleware never attaches allow-origin
    on the blocked login. The real SPA always sends Origin; losing that header
    hides Retry-After from the UI even when the global slot is correctly spent.
    """
    client = _limited_client(tmp_path, monkeypatch, "cors-429-headers.db")
    origin_headers = {"Origin": DASHBOARD_ORIGIN}
    body = {"email": "cors-429@example.com", "password": "password123"}

    login = client.post("/api/auth/login", json=body, headers=origin_headers)
    assert login.status_code == 401
    assert login.json()["detail"] == "Invalid email or password."
    assert login.headers.get("access-control-allow-origin") == DASHBOARD_ORIGIN
    assert login.headers.get("x-ratelimit-remaining") == "0"

    blocked = client.post("/api/auth/login", json=body, headers=origin_headers)
    assert blocked.status_code == 429
    assert blocked.json() == {"detail": "Too many requests."}
    assert blocked.headers.get("access-control-allow-origin") == DASHBOARD_ORIGIN
    assert int(blocked.headers["retry-after"]) >= 1
    assert blocked.headers.get("x-ratelimit-limit") == "1"


def test_non_preflight_options_consumes_global_login_budget(tmp_path, monkeypatch) -> None:
    """OPTIONS without Access-Control-Request-Method must still spend the slot.

    CORSMiddleware only short-circuits true preflights. Skipping every OPTIONS
    in RateLimitMiddleware.dispatch would keep #572 green (preflight still
    never reaches the limiter) while an OPTIONS flood never 429s.
    """
    client = _limited_client(tmp_path, monkeypatch, "cors-options-consume.db")
    origin_headers = {"Origin": DASHBOARD_ORIGIN}

    preflight_skip = client.options("/api/auth/login", headers=origin_headers)
    assert preflight_skip.status_code != 429
    assert preflight_skip.headers.get("x-ratelimit-limit") == "1"
    assert preflight_skip.headers.get("x-ratelimit-remaining") == "0"

    blocked = client.post(
        "/api/auth/login",
        json={"email": "cors-options@example.com", "password": "password123"},
        headers=origin_headers,
    )
    assert blocked.status_code == 429
    assert blocked.json() == {"detail": "Too many requests."}
    assert int(blocked.headers["retry-after"]) >= 1
    assert blocked.headers.get("access-control-allow-origin") == DASHBOARD_ORIGIN

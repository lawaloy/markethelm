"""Global API throttles must 429 through the real app, not only matches().

`configured_rules()` unit-tests the catch-all separately. These HTTP tests
prove the middleware still applies api-global to every /api/ path — including
anonymously open probes and auth handlers with unused per-route headroom —
so a refactor cannot drop the last-line DoS brake. Health and metrics stay
reachable so operators can still probe a throttled instance.
"""

import pytest
from fastapi.testclient import TestClient

from dashboard.backend.main import app
from src.storage.database import init_database
from src.storage.users import get_user_by_email


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv(
        "MARKETHELM_DATABASE_URL",
        f"sqlite:///{(tmp_path / 'global-limits.db').as_posix()}",
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


def test_global_rate_limit_blocks_further_api_calls_including_auth(client):
    first = client.get("/api/alerts/health")
    assert first.status_code == 200
    assert first.json()["ok"] is True
    assert first.json()["quotes"] is True
    assert isinstance(first.json()["live_quotes_configured"], bool)

    blocked = client.get("/api/alerts/health")
    assert blocked.status_code == 429
    assert blocked.json() == {"detail": "Too many requests."}
    assert int(blocked.headers["retry-after"]) >= 1

    # Login has its own unused bucket, but the catch-all still runs first.
    login = client.post(
        "/api/auth/login",
        json={"email": "global@example.com", "password": "password123"},
    )
    assert login.status_code == 429
    assert login.json() == {"detail": "Too many requests."}

    register = client.post(
        "/api/auth/register",
        json={"email": "global@example.com", "password": "password123"},
    )
    assert register.status_code == 429
    assert get_user_by_email("global@example.com") is None


def test_health_probes_stay_available_after_api_global_is_spent(client):
    assert client.get("/api/alerts/health").status_code == 200
    assert client.get("/api/alerts/health").status_code == 429

    assert client.get("/health").status_code == 200
    assert client.get("/health/live").status_code == 200
    ready = client.get("/health/ready")
    assert ready.status_code == 200
    assert ready.json()["status"] == "ready"
    metrics = client.get("/metrics")
    assert metrics.status_code == 200
    assert "text/plain" in metrics.headers.get("content-type", "")

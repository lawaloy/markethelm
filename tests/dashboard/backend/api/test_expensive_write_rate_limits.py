"""Expensive-write throttles must 429 through the real app, not only matches().

`configured_rules().matches()` is unit-tested separately. These HTTP tests
prove the middleware still applies the expensive-write bucket to refresh,
refresh cancel, account mutations, POST quotes, alert runs, and live alert
tests so a path or method refactor cannot silently drop those throttles
(or start applying them to GET quotes / refresh status).
"""

from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

from dashboard.backend.api import refresh
from dashboard.backend.main import app
from src.storage.database import init_database
from src.storage.user_alerts import save_user_alerts_config
from tests.dashboard.backend.api.test_refresh import FakeProcess, reset_refresh_state


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv(
        "MARKETHELM_DATABASE_URL",
        f"sqlite:///{(tmp_path / 'expensive-limits.db').as_posix()}",
    )
    monkeypatch.setenv("MARKETHELM_AUTH_SECRET", "test-secret-min-16-chars")
    monkeypatch.setenv("MARKETHELM_RATE_LIMIT_ENABLED", "true")
    monkeypatch.setenv("MARKETHELM_RATE_LIMIT_GLOBAL", "1000")
    monkeypatch.setenv("MARKETHELM_RATE_LIMIT_LOGIN", "1000")
    monkeypatch.setenv("MARKETHELM_RATE_LIMIT_REGISTER", "1000")
    monkeypatch.setenv("MARKETHELM_RATE_LIMIT_AUTH_EMAIL", "1000")
    monkeypatch.setenv("MARKETHELM_RATE_LIMIT_EXPENSIVE", "1")
    # Keep spend + follow-up POSTs in the same 60s window (CI can otherwise
    # straddle `now % 60 == 0` and see a fresh expensive-write bucket).
    monkeypatch.setattr("dashboard.backend.rate_limit.time.time", lambda: 1_700_000_030)

    init_database()

    return TestClient(app)


def _register(client, email="expensive@example.com"):
    response = client.post(
        "/api/auth/register",
        json={"email": email, "password": "password123"},
    )
    assert response.status_code == 200
    return response.json()


def _login(client, email, password):
    response = client.post(
        "/api/auth/login",
        json={"email": email, "password": password},
    )
    assert response.status_code == 200
    return response.json()["access_token"]


def _spend_expensive_bucket(client):
    """Consume the single expensive-write slot with an authenticated mutation."""
    registered = _register(client)
    headers = {"Authorization": f"Bearer {registered['access_token']}"}
    changed = client.post(
        "/api/auth/password/change",
        headers=headers,
        json={
            "current_password": "password123",
            "new_password": "new-password-123",
        },
    )
    assert changed.status_code == 200
    token = _login(client, "expensive@example.com", "new-password-123")
    return {"Authorization": f"Bearer {token}"}


def test_password_change_rate_limit_blocks_a_second_change(client):
    headers = _spend_expensive_bucket(client)

    blocked = client.post(
        "/api/auth/password/change",
        headers=headers,
        json={
            "current_password": "new-password-123",
            "new_password": "third-password-123",
        },
    )
    assert blocked.status_code == 429
    assert blocked.json() == {"detail": "Too many requests."}
    assert int(blocked.headers["retry-after"]) >= 1

    # The second change never ran; the password stays the first new value.
    assert (
        client.post(
            "/api/auth/login",
            json={"email": "expensive@example.com", "password": "third-password-123"},
        ).status_code
        == 401
    )
    assert (
        client.post(
            "/api/auth/login",
            json={"email": "expensive@example.com", "password": "new-password-123"},
        ).status_code
        == 200
    )


def test_expensive_write_bucket_is_shared_and_does_not_gate_get_quotes(client, monkeypatch):
    headers = _spend_expensive_bucket(client)
    popen = MagicMock()
    monkeypatch.setattr("dashboard.backend.api.refresh.subprocess.Popen", popen)
    run_user_check = MagicMock()
    monkeypatch.setattr("src.alerts.alert_worker.run_user_check", run_user_check)
    resolve_prices = MagicMock(return_value={"AAPL": 180.0})
    monkeypatch.setattr(
        "dashboard.backend.api.alerts.resolve_symbol_prices",
        resolve_prices,
    )

    refresh = client.post("/api/refresh", headers=headers)
    assert refresh.status_code == 429
    assert refresh.json() == {"detail": "Too many requests."}
    popen.assert_not_called()

    deleted = client.request(
        "DELETE",
        "/api/auth/account",
        headers=headers,
        json={"current_password": "new-password-123", "confirmation": "DELETE"},
    )
    assert deleted.status_code == 429
    assert client.get("/api/auth/me", headers=headers).status_code == 200

    quotes_get = client.get(
        "/api/alerts/quotes",
        params={"symbols": "AAPL"},
        headers=headers,
    )
    assert quotes_get.status_code == 200
    assert quotes_get.json()["prices"]["AAPL"] == 180.0

    quotes_post = client.post(
        "/api/alerts/quotes",
        json={"symbols": ["AAPL"]},
        headers=headers,
    )
    assert quotes_post.status_code == 429
    assert quotes_post.json() == {"detail": "Too many requests."}

    run = client.post("/api/alerts/run", headers=headers)
    assert run.status_code == 429
    run_user_check.assert_not_called()
    # GET quotes is the only quotes call that reached the handler.
    assert resolve_prices.call_count == 1


def test_expensive_write_gates_alert_test_and_refresh_cancel(client, monkeypatch):
    """Live test-sends and refresh cancel share the expensive-write bucket.

    Sibling POST paths are covered above. These two can still send mail/webhooks
    or terminate an in-flight Finnhub child if a path typo drops them from the
    rule, so the middleware must 429 before those handlers run.
    """

    headers = _spend_expensive_bucket(client)
    user_id = client.get("/api/auth/me", headers=headers).json()["id"]
    save_user_alerts_config(
        user_id,
        {
            "defaults": {},
            "alerts": [
                {
                    "id": "watch_aapl",
                    "name": "AAPL watch",
                    "enabled": True,
                    "condition": {
                        "type": "price_threshold",
                        "symbol": "AAPL",
                        "operator": "below",
                        "value": 100,
                    },
                }
            ],
        },
    )
    run_alert_test = MagicMock(
        return_value={
            "alert_id": "watch_aapl",
            "status": "sent",
            "notifiers": ["email"],
        }
    )
    monkeypatch.setattr(
        "dashboard.backend.api.alerts.run_alert_test",
        run_alert_test,
    )

    tested = client.post(
        "/api/alerts/test",
        headers=headers,
        json={"id": "watch_aapl", "dry_run": False},
    )
    assert tested.status_code == 429
    assert tested.json() == {"detail": "Too many requests."}
    run_alert_test.assert_not_called()

    reset_refresh_state()
    fake_process = FakeProcess(returncode=-15, running=True)
    refresh.refresh_status["is_running"] = True
    refresh.refresh_status["last_status"] = "running"
    refresh._refresh_process = fake_process
    try:
        cancelled = client.post("/api/refresh/cancel", headers=headers)
        assert cancelled.status_code == 429
        assert cancelled.json() == {"detail": "Too many requests."}
        assert fake_process.terminated is False
        assert refresh._refresh_cancel_event.is_set() is False
        # Status stays off the expensive-write rule so operators can still poll.
        status = client.get("/api/refresh/status", headers=headers)
        assert status.status_code == 200
        assert status.json()["is_running"] is True
    finally:
        reset_refresh_state()

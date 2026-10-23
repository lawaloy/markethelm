"""Naive last_seen_at must fail closed (TypeError) instead of 500ing /health/worker."""

import pytest
from fastapi.testclient import TestClient

from dashboard.backend.main import app
from src.storage.database import get_connection, init_database
from src.storage.health import record_worker_heartbeat, worker_health


@pytest.fixture
def db(tmp_path, monkeypatch):
    path = tmp_path / "naive-heartbeat.db"
    monkeypatch.setenv("MARKETHELM_DATABASE_URL", f"sqlite:///{path.as_posix()}")
    init_database()


def test_worker_health_naive_iso_timestamp_is_invalid_heartbeat(db) -> None:
    """fromisoformat succeeds; subtracting naive from aware UTC must not escape."""
    record_worker_heartbeat("w1", "healthy")
    with get_connection() as conn:
        conn.execute(
            "UPDATE worker_heartbeats SET last_seen_at = ? WHERE worker_id = ?",
            ("2026-07-24T12:00:00", "w1"),
        )
    assert worker_health(stale_after_seconds=30) == {
        "ok": False,
        "reason": "invalid_heartbeat",
    }


def test_worker_probe_503s_on_naive_timestamp_without_500(db) -> None:
    record_worker_heartbeat("w1", "healthy")
    with get_connection() as conn:
        conn.execute(
            "UPDATE worker_heartbeats SET last_seen_at = ? WHERE worker_id = ?",
            ("2026-07-24T12:00:00", "w1"),
        )

    response = TestClient(app).get("/health/worker")
    assert response.status_code == 503
    payload = response.json()
    assert payload["status"] == "unhealthy"
    assert payload["ok"] is False
    assert payload["reason"] == "invalid_heartbeat"

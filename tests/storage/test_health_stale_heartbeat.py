"""A leftover healthy heartbeat must not look ready after the worker dies."""

from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from dashboard.backend.main import app
from src.storage.database import get_connection, init_database
from src.storage.health import record_worker_heartbeat


@pytest.fixture
def db(tmp_path, monkeypatch):
    path = tmp_path / "stale-heartbeat.db"
    monkeypatch.setenv("MARKETHELM_DATABASE_URL", f"sqlite:///{path.as_posix()}")
    # Pin the worker interval so the HTTP probe's stale window is deterministic:
    # stale_after = interval * 2 + 30 = 150s.
    monkeypatch.setenv("ALERT_CHECK_INTERVAL_SECONDS", "60")
    init_database()


def test_worker_probe_503s_on_stale_healthy_heartbeat(db) -> None:
    """HTTP 503 must follow worker_health ok=False even when the row still says healthy.

    ``/health/worker`` sets JSON status from ok, then unpacks the heartbeat, so
    the row's ``healthy`` label can overwrite the probe string. Load balancers
    that key off HTTP status (not the overwritten JSON field) must still see
    503 when last_seen_at is older than the stale window. Storage-layer
    ``worker_health`` already covers this; the HTTP wrapper in main.py did not.
    """
    record_worker_heartbeat("w1", "healthy", {"enqueued": 1})
    stale = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
    with get_connection() as conn:
        conn.execute(
            "UPDATE worker_heartbeats SET last_seen_at = ? WHERE worker_id = ?",
            (stale, "w1"),
        )

    response = TestClient(app).get("/health/worker")
    assert response.status_code == 503
    payload = response.json()
    assert payload["ok"] is False
    assert payload["worker_id"] == "w1"
    assert payload["age_seconds"] > 150

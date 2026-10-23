"""A leftover 'running' heartbeat must fail closed — not look healthy to probes."""

import pytest
from fastapi.testclient import TestClient

from dashboard.backend.main import app
from src.storage.database import init_database
from src.storage.health import record_worker_heartbeat, worker_health


@pytest.fixture
def db(tmp_path, monkeypatch):
    path = tmp_path / "running-heartbeat.db"
    monkeypatch.setenv("MARKETHELM_DATABASE_URL", f"sqlite:///{path.as_posix()}")
    init_database()


def test_worker_health_running_status_is_not_ok(db) -> None:
    """run_db_worker_cycle writes running before work; a crash can leave it there."""
    record_worker_heartbeat("w1", "running", {"tick": 1})
    health = worker_health(stale_after_seconds=30)
    assert health["ok"] is False
    assert health["status"] == "running"
    assert health["worker_id"] == "w1"
    assert health["details"] == {"tick": 1}
    assert health["age_seconds"] >= 0


def test_worker_probe_503s_on_fresh_running_heartbeat(db) -> None:
    record_worker_heartbeat("w1", "running")

    response = TestClient(app).get("/health/worker")
    assert response.status_code == 503
    payload = response.json()
    assert payload["ok"] is False
    assert payload["worker_id"] == "w1"
    # Probe sets status=unhealthy then unpacks heartbeat, so worker status wins.
    assert payload["status"] == "running"

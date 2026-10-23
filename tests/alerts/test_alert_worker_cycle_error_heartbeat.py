"""A crashed hosted worker cycle must record an error heartbeat for probes."""

from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from dashboard.backend.main import app
from src.alerts.alert_worker import run_db_worker_cycle
from src.storage.database import init_database
from src.storage.health import latest_worker_heartbeat, record_worker_heartbeat


@pytest.fixture
def db(tmp_path, monkeypatch):
    path = tmp_path / "cycle-error.db"
    monkeypatch.setenv("MARKETHELM_DATABASE_URL", f"sqlite:///{path.as_posix()}")
    init_database()


def test_run_db_worker_cycle_records_error_heartbeat_and_probe_503s(db) -> None:
    record_worker_heartbeat("w1", "healthy", {"enqueued": 1})
    with patch(
        "src.alerts.alert_orchestrator.run_orchestrator_tick",
        side_effect=RuntimeError("snapshot failed"),
    ) as mock_tick:
        with patch(
            "src.alerts.job_processor.process_job_queue",
            return_value={"evaluated": 99, "delivered": 99, "failed": 0},
        ) as mock_queue:
            with pytest.raises(RuntimeError, match="snapshot failed"):
                run_db_worker_cycle("w1")

    mock_tick.assert_called_once()
    mock_queue.assert_not_called()

    heartbeat = latest_worker_heartbeat()
    assert heartbeat is not None
    assert heartbeat["worker_id"] == "w1"
    assert heartbeat["status"] == "error"
    assert heartbeat["details"] == {"error": "RuntimeError"}

    response = TestClient(app).get("/health/worker")
    assert response.status_code == 503
    payload = response.json()
    # Probe sets status=unhealthy then unpacks heartbeat, so worker status wins.
    assert payload["status"] == "error"
    assert payload["ok"] is False
    assert payload["worker_id"] == "w1"
    assert payload["details"] == {"error": "RuntimeError"}

"""Successful ticks must clear leftover running; queue crashes must fail closed."""

from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from dashboard.backend.main import app
from src.alerts.alert_worker import run_db_worker_cycle
from src.storage.database import init_database
from src.storage.health import latest_worker_heartbeat, record_worker_heartbeat


@pytest.fixture
def db(tmp_path, monkeypatch):
    path = tmp_path / "cycle-success-queue.db"
    monkeypatch.setenv("MARKETHELM_DATABASE_URL", f"sqlite:///{path.as_posix()}")
    init_database()


def test_run_db_worker_cycle_success_clears_running_heartbeat_and_probe_200s(db) -> None:
    """A leftover running stamp from a prior crash must not stick after a good tick."""
    record_worker_heartbeat("w1", "running", {"tick": 0})
    with patch(
        "src.alerts.alert_orchestrator.run_orchestrator_tick",
        return_value={"last_data_date": "2026-06-09", "message": None, "enqueued": 2},
    ) as mock_tick:
        with patch(
            "src.alerts.job_processor.process_job_queue",
            return_value={"evaluated": 2, "delivered": 1, "failed": 0},
        ) as mock_queue:
            result = run_db_worker_cycle("w1")

    mock_tick.assert_called_once()
    mock_queue.assert_called_once_with("w1")
    assert result["enqueued"] == 2
    assert result["triggered"] == 1
    assert result["last_data_date"] == "2026-06-09"
    assert result["jobs"] == {"evaluated": 2, "delivered": 1, "failed": 0}

    heartbeat = latest_worker_heartbeat()
    assert heartbeat is not None
    assert heartbeat["worker_id"] == "w1"
    assert heartbeat["status"] == "healthy"
    assert heartbeat["details"] == {
        "enqueued": 2,
        "jobs": {"evaluated": 2, "delivered": 1, "failed": 0},
    }

    response = TestClient(app).get("/health/worker")
    assert response.status_code == 200
    payload = response.json()
    assert payload["ok"] is True
    assert payload["status"] == "healthy"
    assert payload["worker_id"] == "w1"


def test_run_db_worker_cycle_keeps_last_success_healthy_while_next_tick_runs(db) -> None:
    """A routine cycle must not create a transient readiness outage."""
    previous_details = {"enqueued": 1, "jobs": {"delivered": 1}}
    record_worker_heartbeat("w1", "healthy", previous_details)

    observed_during_tick = {}

    def inspect_heartbeat_during_tick():
        heartbeat = latest_worker_heartbeat() or {}
        probe = TestClient(app).get("/health/worker")
        observed_during_tick.update(heartbeat)
        observed_during_tick["probe_status"] = probe.status_code
        observed_during_tick["probe"] = probe.json()
        return {"last_data_date": "2026-06-09", "message": None, "enqueued": 0}

    with patch(
        "src.alerts.alert_orchestrator.run_orchestrator_tick",
        side_effect=inspect_heartbeat_during_tick,
    ):
        with patch(
            "src.alerts.job_processor.process_job_queue",
            return_value={"evaluated": 0, "delivered": 0, "failed": 0},
        ):
            run_db_worker_cycle("w1")

    assert observed_during_tick["status"] == "healthy"
    assert observed_during_tick["details"] == previous_details
    # Staging polls this probe during a live cycle; status=running used to 503 here.
    assert observed_during_tick["probe_status"] == 200
    assert observed_during_tick["probe"]["ok"] is True
    assert observed_during_tick["probe"]["status"] == "healthy"
    assert observed_during_tick["probe"]["details"] == previous_details

    heartbeat = latest_worker_heartbeat()
    assert heartbeat is not None
    assert heartbeat["status"] == "healthy"
    assert heartbeat["details"] == {
        "enqueued": 0,
        "jobs": {"evaluated": 0, "delivered": 0, "failed": 0},
    }


def test_run_db_worker_cycle_queue_crash_records_error_heartbeat_and_probe_503s(
    db,
) -> None:
    """process_job_queue is inside the same try as the orchestrator tick."""
    record_worker_heartbeat("w1", "healthy", {"enqueued": 1})
    with patch(
        "src.alerts.alert_orchestrator.run_orchestrator_tick",
        return_value={"last_data_date": "2026-06-09", "message": None, "enqueued": 3},
    ) as mock_tick:
        with patch(
            "src.alerts.job_processor.process_job_queue",
            side_effect=RuntimeError("queue failed"),
        ) as mock_queue:
            with pytest.raises(RuntimeError, match="queue failed"):
                run_db_worker_cycle("w1")

    mock_tick.assert_called_once()
    mock_queue.assert_called_once_with("w1")

    heartbeat = latest_worker_heartbeat()
    assert heartbeat is not None
    assert heartbeat["worker_id"] == "w1"
    assert heartbeat["status"] == "error"
    assert heartbeat["details"] == {"error": "RuntimeError"}

    response = TestClient(app).get("/health/worker")
    assert response.status_code == 503
    payload = response.json()
    assert payload["ok"] is False
    assert payload["status"] == "error"
    assert payload["worker_id"] == "w1"
    assert payload["details"] == {"error": "RuntimeError"}

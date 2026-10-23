"""File-mode Settings GET/status must not 500 when alerts.json has a non-list key."""

import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

from dashboard.backend.main import app


@pytest.fixture
def alerts_config_dir(tmp_path: Path, monkeypatch):
    config_dir = tmp_path / "markethelm"
    config_dir.mkdir()
    monkeypatch.setenv("MARKETHELM_ALERTS_CONFIG", str(config_dir / "alerts.json"))
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.delenv("MARKETHELM_DATABASE_URL", raising=False)
    return config_dir


@pytest.fixture
def client():
    return TestClient(app)


def _stub_status_deps(monkeypatch) -> None:
    monkeypatch.setattr(
        "dashboard.backend.api.alerts.database_enabled",
        lambda: False,
    )
    monkeypatch.setattr(
        "dashboard.backend.api.alerts.get_data_loader",
        lambda: MagicMock(
            get_latest_date=MagicMock(return_value=None),
            load_projections=MagicMock(return_value=MagicMock(empty=True)),
        ),
    )
    monkeypatch.setattr(
        "src.alerts.alert_storage.AlertStorage",
        lambda data_dir=None: MagicMock(
            latest_event_timestamp=MagicMock(return_value=None),
        ),
    )
    monkeypatch.setattr(
        "src.alerts.delivery_status.latest_deliveries_by_channel",
        lambda storage: [],
    )


@pytest.mark.parametrize("bad_alerts", [1, True, "ab", {"id": "x"}])
def test_file_get_config_soft_fails_non_list_alerts(
    client, alerts_config_dir: Path, bad_alerts, monkeypatch
) -> None:
    """Settings GET polishes the file before normalize; ``alerts: 1`` 500ed."""
    monkeypatch.setattr(
        "dashboard.backend.api.alerts.database_enabled",
        lambda: False,
    )
    (alerts_config_dir / "alerts.json").write_text(
        json.dumps({"defaults": {"email_to": "ops@example.com"}, "alerts": bad_alerts}),
        encoding="utf-8",
    )

    response = client.get("/api/alerts/config")
    assert response.status_code == 200
    body = response.json()
    assert body["exists"] is True
    assert body["config"]["alerts"] == []
    assert body["config"]["defaults"]["email_to"] == "ops@example.com"


@pytest.mark.parametrize("bad_alerts", [1, True, {"id": "x"}])
def test_file_status_soft_fails_non_list_alerts(
    client, alerts_config_dir: Path, bad_alerts, monkeypatch
) -> None:
    (alerts_config_dir / "alerts.json").write_text(
        json.dumps({"defaults": {}, "alerts": bad_alerts}),
        encoding="utf-8",
    )
    _stub_status_deps(monkeypatch)

    response = client.get("/api/alerts/status")
    assert response.status_code == 200
    assert response.json()["active_watches"] == 0

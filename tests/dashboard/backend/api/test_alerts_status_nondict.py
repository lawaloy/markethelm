"""File-mode /api/alerts/status must tolerate non-dict alert rows."""

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
    return config_dir


@pytest.fixture
def client():
    return TestClient(app)


def test_status_skips_nondict_alert_rows(
    client, alerts_config_dir: Path, tmp_path: Path, monkeypatch
) -> None:
    config_path = alerts_config_dir / "alerts.json"
    config_path.write_text(
        json.dumps(
            {
                "defaults": {},
                "alerts": [
                    "junk",
                    None,
                    42,
                    {
                        "id": "keep_on",
                        "enabled": True,
                        "condition": {
                            "type": "price_threshold",
                            "symbol": "AAPL",
                            "operator": ">",
                            "value": 100,
                        },
                    },
                    {
                        "id": "keep_off",
                        "enabled": False,
                        "condition": {
                            "type": "price_threshold",
                            "symbol": "MSFT",
                            "operator": "<",
                            "value": 50,
                        },
                    },
                ],
            }
        ),
        encoding="utf-8",
    )

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

    response = client.get("/api/alerts/status")
    assert response.status_code == 200
    assert response.json()["active_watches"] == 1


def test_status_all_nondict_alerts_counts_zero(
    client, alerts_config_dir: Path, monkeypatch
) -> None:
    config_path = alerts_config_dir / "alerts.json"
    config_path.write_text(
        json.dumps({"defaults": {}, "alerts": ["a", None, 1]}),
        encoding="utf-8",
    )
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

    response = client.get("/api/alerts/status")
    assert response.status_code == 200
    assert response.json()["active_watches"] == 0

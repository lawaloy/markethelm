"""File-mode Check-now and Send-test must survive poison alerts.json.

#552 locks Settings GET/init. Hosted poison-row run/test is locked in
``test_alerts_corrupt_config_recovery``. File-mode Check-now goes through
``AlertEngine.from_config`` (its own json.load) and Send-test through
``run_alert_test``. A later ``json.loads`` without those soft-fails would
500 Check-now or map a poison file to the missing-config 404.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from dashboard.backend.main import app

POISON_BLOBS = (
    b'{"not": "recoverable"',
    b"[]",
    b'"just-a-string"',
    b"null",
    b"\xff\xfe",
)


@pytest.fixture
def file_mode(tmp_path: Path, monkeypatch):
    monkeypatch.delenv("MARKETHELM_DATABASE_URL", raising=False)
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.delenv("ALERT_EMAIL_TO", raising=False)
    monkeypatch.delenv("ALERT_WEBHOOK_URL", raising=False)
    monkeypatch.delenv("DISCORD_WEBHOOK_URL", raising=False)
    monkeypatch.delenv("ALERT_WEBHOOK_FORMAT", raising=False)
    user_dir = tmp_path / "markethelm"
    user_dir.mkdir()
    monkeypatch.setattr("src.alerts.alert_paths.user_config_dir", lambda: user_dir)
    config_path = user_dir / "alerts.json"
    monkeypatch.setenv("MARKETHELM_ALERTS_CONFIG", str(config_path))
    return config_path


@pytest.fixture
def client():
    return TestClient(app)


@pytest.mark.parametrize("blob", POISON_BLOBS)
def test_file_mode_run_idles_on_corrupt_or_non_object_root(
    client, file_mode: Path, blob: bytes, monkeypatch
) -> None:
    """Check-now must 200 idle, not 500 or 404 no-market-data."""
    file_mode.write_bytes(blob)

    def _boom_loader():
        raise AssertionError("corrupt config must not load market data")

    monkeypatch.setattr(
        "dashboard.backend.services.data_loader.get_data_loader",
        _boom_loader,
    )

    response = client.post("/api/alerts/run")
    assert response.status_code == 200
    body = response.json()
    assert body["triggered"] == 0
    assert body["message"] == "No active watches configured."
    assert file_mode.read_bytes() == blob


def test_file_mode_test_404s_on_corrupt_without_building_notifiers(client, file_mode: Path) -> None:
    """Send-test must 404 as corrupt (not missing) and skip notifier setup."""
    file_mode.write_text("{not-json", encoding="utf-8")

    with patch(
        "src.cli.alerts_commands.AlertEngine._build_notifiers",
    ) as mock_build:
        response = client.post(
            "/api/alerts/test",
            json={"id": "any", "dry_run": True},
        )

    assert response.status_code == 404
    assert "Corrupt or invalid alerts config" in response.json()["detail"]
    mock_build.assert_not_called()
    assert file_mode.read_text(encoding="utf-8") == "{not-json"

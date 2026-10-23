"""File-mode Settings GET/init must recover poison alerts.json, not 500.

Hosted corrupt-row GET/init is locked in ``test_alerts_config_corrupt_hosted``
and ``test_alerts_corrupt_config_recovery``. File-mode ``load_alerts_config``
returns None for truncated JSON and non-object roots. Settings GET must still
200 with empty rules; a later ``json.loads`` without that soft-fail would 500
the page. Init without force must 409 because the path exists; force must
rewrite a valid empty object, not copy bundled sample ids.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from dashboard.backend.main import app


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


@pytest.mark.parametrize(
    "blob",
    ['{"not": "recoverable"', "[]", '"just-a-string"', "null"],
)
def test_file_mode_get_config_soft_fails_corrupt_or_non_object_root(
    client, file_mode: Path, blob: str
) -> None:
    """Hand-edited array/string/truncated JSON must not 500 Settings GET."""
    file_mode.write_text(blob, encoding="utf-8")

    response = client.get("/api/alerts/config")
    assert response.status_code == 200
    body = response.json()
    # Unreadable payload is treated as missing; the file path still exists.
    assert body["exists"] is False
    assert body["config"]["alerts"] == []
    assert isinstance(body["config"]["defaults"], dict)


def test_file_mode_init_force_recovers_corrupt_file_to_empty_not_bundled_example(
    client, file_mode: Path
) -> None:
    """Conflict must keep the poison file; force must rewrite empty onboarding."""
    file_mode.write_text("{not-json", encoding="utf-8")

    conflict = client.post("/api/alerts/init")
    assert conflict.status_code == 409
    assert "force=true" in conflict.json()["detail"]
    assert file_mode.read_text(encoding="utf-8") == "{not-json"

    forced = client.post("/api/alerts/init?force=true")
    assert forced.status_code == 200

    reset = client.get("/api/alerts/config")
    assert reset.status_code == 200
    assert reset.json()["exists"] is True
    reset_ids = [row["id"] for row in reset.json()["config"]["alerts"]]
    # Empty onboarding config — not alerts.example.json sample ids.
    assert reset_ids == []
    written = json.loads(file_mode.read_text(encoding="utf-8"))
    assert isinstance(written, dict)
    assert written.get("alerts") == []

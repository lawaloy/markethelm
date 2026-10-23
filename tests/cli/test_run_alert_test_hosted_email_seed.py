"""Hosted /api/alerts/test must not seed process-wide ALERT_EMAIL_TO into defaults."""

from unittest.mock import MagicMock, patch

import pytest

from src.cli import alerts_commands
from src.storage.database import init_database


@pytest.fixture
def hosted_db(tmp_path, monkeypatch):
    db_path = tmp_path / "alert-test-seed.db"
    monkeypatch.setenv("MARKETHELM_DATABASE_URL", f"sqlite:///{db_path.as_posix()}")
    monkeypatch.setenv("ALERT_EMAIL_TO", "global-shared@example.com")

    init_database()


def _email_alert_config(*, email_to: str | None = None):
    alert = {
        "id": "a1",
        "name": "Watch",
        "enabled": True,
        "notifications": ["email"],
        "condition": {
            "type": "price_threshold",
            "symbol": "AAPL",
            "operator": "less_than",
            "value": 100,
        },
    }
    if email_to is not None:
        alert["email_to"] = email_to
    return {"defaults": {}, "alerts": [alert]}


def test_run_alert_test_does_not_seed_env_email_when_hosted(hosted_db) -> None:
    """Without a tenant email_to, polish must not invent one from ALERT_EMAIL_TO."""
    with patch("src.alerts.alert_engine.EmailNotifier.from_alert", return_value=None) as from_alert:
        # Engine falls back to LogNotifier when email cannot be built — that is fine.
        # The regression is seeding global ALERT_EMAIL_TO onto the effective alert.
        result = alerts_commands.run_alert_test("a1", dry_run=True, config=_email_alert_config())

    assert from_alert.call_count >= 1
    effective = from_alert.call_args[0][0]
    assert effective.get("email_to") in (None, "")
    assert "email" not in (result.get("notifiers") or [])
    assert result["status"] == "dry_run"


def test_run_alert_test_uses_tenant_email_in_hosted_mode(hosted_db) -> None:
    notifier = MagicMock()
    notifier.__class__.__name__ = "EmailNotifier"
    notifier.preview.return_value = {"channel": "email", "body": "ok"}

    with patch(
        "src.alerts.alert_engine.EmailNotifier.from_alert", return_value=notifier
    ) as from_alert:
        result = alerts_commands.run_alert_test(
            "a1",
            dry_run=True,
            config=_email_alert_config(email_to="tenant@example.com"),
        )

    assert result["status"] == "dry_run"
    effective = from_alert.call_args[0][0]
    assert effective.get("email_to") == "tenant@example.com"
    assert effective.get("email_to") != "global-shared@example.com"


def test_run_alert_test_still_seeds_env_email_in_file_mode(monkeypatch, tmp_path) -> None:
    monkeypatch.delenv("MARKETHELM_DATABASE_URL", raising=False)
    monkeypatch.setenv("ALERT_EMAIL_TO", "ops@example.com")

    notifier = MagicMock()
    notifier.__class__.__name__ = "EmailNotifier"
    notifier.preview.return_value = {"channel": "email", "body": "ok"}

    with patch(
        "src.alerts.alert_engine.EmailNotifier.from_alert", return_value=notifier
    ) as from_alert:
        alerts_commands.run_alert_test("a1", dry_run=True, config=_email_alert_config())

    effective = from_alert.call_args[0][0]
    assert effective.get("email_to") == "ops@example.com"

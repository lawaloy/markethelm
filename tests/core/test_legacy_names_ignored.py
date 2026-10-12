"""Post-rename contract: pre-markethelm names must not be honoured.

#697 removed ``src.core.compat`` so ``MARKET_HELM_*``, ``STOCK_TRACKER_CONFIG``,
and ``~/.market-helm`` no longer alias the canonical ``MARKETHELM_*`` /
``~/.markethelm`` names. Hosts that still export the old spellings would
silently run in file mode, skip auth, or ignore a deploy config override.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from dashboard.backend.rate_limit import rate_limiting_enabled
from dashboard.backend.user_paths import user_config_dir as dashboard_user_config_dir
from src.alerts.alert_paths import (
    resolve_alerts_config_path,
)
from src.alerts.alert_paths import user_config_dir as alerts_user_config_dir
from src.core.config import get_indices_to_track
from src.storage.database import database_enabled, resolve_database_path
from src.storage.session import AuthError, create_access_token, ensure_auth_secret


def _fake_home(monkeypatch, tmp_path: Path) -> Path:
    monkeypatch.setenv("HOME", str(tmp_path))
    if sys.platform == "win32":
        monkeypatch.setenv("USERPROFILE", str(tmp_path))
    return tmp_path


def test_legacy_database_url_does_not_enable_hosted_mode(monkeypatch, tmp_path):
    monkeypatch.delenv("MARKETHELM_DATABASE_URL", raising=False)
    legacy_url = f"sqlite:///{(tmp_path / 'legacy.db').as_posix()}"
    monkeypatch.setenv("MARKET_HELM_DATABASE_URL", legacy_url)

    assert database_enabled() is False
    with pytest.raises(RuntimeError, match="MARKETHELM_DATABASE_URL"):
        resolve_database_path()


def test_legacy_auth_secret_does_not_sign_tokens(monkeypatch):
    monkeypatch.delenv("MARKETHELM_AUTH_SECRET", raising=False)
    monkeypatch.setenv("MARKET_HELM_AUTH_SECRET", "legacy-secret-min-16")

    with pytest.raises(AuthError, match="MARKETHELM_AUTH_SECRET"):
        ensure_auth_secret()
    with pytest.raises(AuthError, match="MARKETHELM_AUTH_SECRET"):
        create_access_token("user-123")


def test_legacy_config_env_does_not_override_tracked_indices(monkeypatch, tmp_path):
    custom = tmp_path / "legacy_exchanges.json"
    custom.write_text(
        json.dumps({"indices_to_track": ["Dow Jones"]}),
        encoding="utf-8",
    )
    monkeypatch.delenv("MARKETHELM_CONFIG", raising=False)
    monkeypatch.setenv("STOCK_TRACKER_CONFIG", str(custom))
    monkeypatch.setenv("MARKET_HELM_CONFIG", str(custom))

    assert get_indices_to_track() != ["Dow Jones"]
    assert "Dow Jones" not in get_indices_to_track()


def test_legacy_alerts_config_env_does_not_override_path(monkeypatch, tmp_path):
    home = _fake_home(monkeypatch, tmp_path / "home")
    user_dir = home / ".markethelm"
    user_dir.mkdir(parents=True)
    user_path = user_dir / "alerts.json"
    user_path.write_text('{"alerts": [{"id": "user"}]}', encoding="utf-8")

    repo_root = tmp_path / "repo"
    (repo_root / "config").mkdir(parents=True)
    monkeypatch.setattr("src.alerts.alert_paths._REPO_ROOT", repo_root)

    env_path = tmp_path / "legacy-alerts.json"
    env_path.write_text('{"alerts": [{"id": "legacy"}]}', encoding="utf-8")
    monkeypatch.delenv("MARKETHELM_ALERTS_CONFIG", raising=False)
    monkeypatch.setenv("MARKET_HELM_ALERTS_CONFIG", str(env_path))

    assert resolve_alerts_config_path() == user_path


def test_legacy_rate_limit_env_does_not_enable_limits(monkeypatch):
    monkeypatch.delenv("MARKETHELM_DATABASE_URL", raising=False)
    monkeypatch.delenv("MARKETHELM_RATE_LIMIT_ENABLED", raising=False)
    monkeypatch.setenv("MARKET_HELM_RATE_LIMIT_ENABLED", "true")

    assert rate_limiting_enabled() is False


def test_legacy_user_config_dir_is_not_used_or_migrated(monkeypatch, tmp_path):
    home = _fake_home(monkeypatch, tmp_path)
    legacy = home / ".market-helm"
    legacy.mkdir()
    (legacy / "alerts.json").write_text("{}", encoding="utf-8")

    assert dashboard_user_config_dir() == home / ".markethelm"
    assert alerts_user_config_dir() == home / ".markethelm"
    assert legacy.is_dir()
    assert (legacy / "alerts.json").is_file()

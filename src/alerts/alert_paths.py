"""Resolve alert config locations for repo dev vs pip install (~/.markethelm)."""

from __future__ import annotations

import json
import os
import shutil
import tempfile
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlparse

import src.alerts.alert_rules as alert_rules
from src.utils.tickers import normalize_ticker

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent

# Path-keyed locks so concurrent update_user_env_vars callers (distinct
# settings saves) cannot drop keys via unlocked load-mutate-save.
_ENV_PATH_LOCKS: Dict[str, threading.Lock] = {}
_ENV_PATH_LOCKS_GUARD = threading.Lock()


def _env_lock_for(path: Path) -> threading.Lock:
    key = str(path.resolve())
    with _ENV_PATH_LOCKS_GUARD:
        lock = _ENV_PATH_LOCKS.get(key)
        if lock is None:
            lock = threading.Lock()
            _ENV_PATH_LOCKS[key] = lock
        return lock


def user_config_dir() -> Path:
    """Per-user config directory (same location as the dashboard uses)."""
    return Path.home() / ".markethelm"


def bundled_example_path() -> Path:
    return _REPO_ROOT / "config" / "alerts.example.json"


def user_alerts_config_path() -> Path:
    """Writable user alerts path (never the repo dev fallback)."""
    env_path = os.environ.get("MARKETHELM_ALERTS_CONFIG")
    if env_path:
        return Path(env_path)
    return user_config_dir() / "alerts.json"


def resolve_alerts_config_path(explicit: Optional[Path] = None) -> Path:
    """Path to alerts.json: env override, then user file, then repo dev file."""
    if explicit is not None:
        return Path(explicit)
    env_path = os.environ.get("MARKETHELM_ALERTS_CONFIG")
    if env_path:
        return Path(env_path)
    user_path = user_config_dir() / "alerts.json"
    if user_path.exists():
        return user_path
    repo_path = _REPO_ROOT / "config" / "alerts.json"
    if repo_path.exists():
        return repo_path
    return user_path


_PLACEHOLDER_EMAILS = frozenset({"you@example.com", "alerts@example.com", "backup@example.com"})


def _is_placeholder_webhook(url: str) -> bool:
    lowered = url.lower()
    if "your/webhook" in lowered:
        return True
    host = (urlparse(url.strip()).hostname or "").lower()
    return host == "example.com" or host.endswith(".example.com")


def alert_rows(config: Dict[str, Any]) -> list[Any]:
    """Return the alerts array, or [] when a hand-edited key is not a list.

    ``config.get("alerts") or []`` treats missing/empty as empty but still
    iterates a truthy non-list (``1``, ``true``, a dict) and TypeErrors.
    """
    alerts = config.get("alerts") if isinstance(config, dict) else None
    return alerts if isinstance(alerts, list) else []


def polish_alerts_config(
    config: Dict[str, Any],
    *,
    seed_env_email: bool = True,
) -> Dict[str, Any]:
    """Remove bundled placeholders and optionally seed defaults from environment.

    Hosted multi-user saves must pass ``seed_env_email=False`` so a process-wide
    ``ALERT_EMAIL_TO`` is never written into another tenant's config.
    """
    polished = dict(config)
    raw_defaults = polished.get("defaults")
    # Truthy non-dict defaults (list/str/number) crash dict(); soft-fail to {}.
    defaults = dict(raw_defaults) if isinstance(raw_defaults, dict) else {}

    if seed_env_email:
        env_email = (os.environ.get("ALERT_EMAIL_TO") or "").strip()
        if env_email and not defaults.get("email_to"):
            defaults["email_to"] = env_email

    # Match per-alert polish: bundled / example placeholders must not survive
    # in defaults or hosted channel readiness reports a false webhook_url ready.
    default_webhook = str(defaults.get("webhook_url") or "").strip()
    if default_webhook and _is_placeholder_webhook(default_webhook):
        defaults.pop("webhook_url", None)

    polished["defaults"] = defaults
    alerts: list[Dict[str, Any]] = []
    for alert in alert_rows(polished):
        if not isinstance(alert, dict):
            continue
        cleaned = dict(alert)
        email = str(cleaned.get("email_to") or "").strip().lower()
        if email in _PLACEHOLDER_EMAILS:
            cleaned.pop("email_to", None)
        webhook_url = str(cleaned.get("webhook_url") or "").strip()
        if webhook_url and _is_placeholder_webhook(webhook_url):
            cleaned.pop("webhook_url", None)
        alerts.append(cleaned)
    polished["alerts"] = alerts
    return dedupe_alerts_config(polished)


def _price_alert_key(condition: Dict[str, Any]) -> Optional[str]:
    if not isinstance(condition, dict):
        return None
    if condition.get("type") != "price_threshold":
        return None
    symbol = normalize_ticker(condition.get("symbol"))
    operator = condition.get("operator")
    value = condition.get("value")
    if not symbol or not operator or value is None:
        return None
    return f"{symbol}|{operator}|{value}"


def dedupe_alerts_config(config: Dict[str, Any]) -> Dict[str, Any]:
    """Keep one rule per price-threshold condition (first wins)."""
    seen: set[str] = set()
    unique: list[Dict[str, Any]] = []
    for alert in alert_rows(config):
        if not isinstance(alert, dict):
            continue
        raw_condition = alert.get("condition")
        condition = raw_condition if isinstance(raw_condition, dict) else {}
        key = _price_alert_key(condition) or str(alert.get("id"))
        if key in seen:
            continue
        seen.add(key)
        unique.append(alert)
    config["alerts"] = unique
    return config


def init_minimal_user_alerts_config(force: bool = False) -> Path:
    """Create an empty user alerts config with env-based defaults (dashboard onboarding)."""
    dest = user_alerts_config_path()
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.exists() and not force:
        raise FileExistsError(str(dest))
    save_alerts_config(polish_alerts_config({"defaults": {}, "alerts": []}), explicit=dest)
    return dest


def init_user_alerts_config(force: bool = False) -> Path:
    """Copy bundled alerts.example.json to the user alerts config path."""
    dest = user_alerts_config_path()
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.exists() and not force:
        raise FileExistsError(str(dest))
    example = bundled_example_path()
    if not example.exists():
        raise FileNotFoundError(f"Bundled example not found: {example}")
    shutil.copy(example, dest)
    with open(dest, "r", encoding="utf-8") as handle:
        config = json.load(handle)
    save_alerts_config(polish_alerts_config(config), explicit=dest)
    return dest


def load_alerts_config(explicit: Optional[Path] = None) -> Tuple[Path, Optional[Dict[str, Any]]]:
    """Return resolved config path and parsed JSON, or None if the file is missing/unreadable."""
    path = resolve_alerts_config_path(explicit) if explicit is None else Path(explicit)
    if not path.exists():
        return path, None
    try:
        with open(path, "r", encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        # Treat corrupt/unreadable config like a missing file so Settings GET
        # can soft-fail instead of 500ing on hand-edited JSON.
        return path, None
    if not isinstance(data, dict):
        return path, None
    return path, data


def save_alerts_config(config: Dict[str, Any], explicit: Optional[Path] = None) -> Path:
    """Write alerts config atomically to the user config path."""
    path = Path(explicit) if explicit is not None else user_alerts_config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    payload = polish_alerts_config(config)
    payload = strip_webhook_secrets_from_config(payload)
    with open(tmp, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2)
        handle.write("\n")
    tmp.replace(path)
    return path


def get_enabled_watch_symbols() -> List[str]:
    """Symbols referenced by enabled price / RSI / single-symbol compound watches."""

    _, raw = load_alerts_config()
    if not raw:
        return []
    symbols: set[str] = set()
    for alert in alert_rows(raw):
        if not isinstance(alert, dict):
            continue
        if not alert.get("enabled"):
            continue
        condition = alert.get("condition") or {}
        if not isinstance(condition, dict):
            continue
        for symbol in alert_rules.condition_watch_symbols(condition):
            symbols.add(symbol)
    return sorted(symbols)


def apply_alert_defaults(
    alert: Dict[str, Any], defaults: Optional[Dict[str, Any]]
) -> Dict[str, Any]:
    """Merge config-level defaults into a rule for notification delivery."""
    if not defaults:
        return alert
    if not isinstance(defaults, dict):
        return alert
    merged = dict(alert)
    raw_notifications = merged.get("notifications")
    # Require a real list: strings make `"email" in notifications` true via
    # substring search and would mis-seed email_to from defaults.
    notifications = raw_notifications if isinstance(raw_notifications, list) else []
    if "email" in notifications and not merged.get("email_to") and defaults.get("email_to"):
        merged["email_to"] = defaults["email_to"]
    if "webhook" in notifications:
        if not merged.get("webhook_url") and defaults.get("webhook_url"):
            merged["webhook_url"] = defaults["webhook_url"]
        if not merged.get("webhook_format") and defaults.get("webhook_format"):
            merged["webhook_format"] = defaults["webhook_format"]
    return merged


def strip_webhook_secrets_from_config(config: Dict[str, Any]) -> Dict[str, Any]:
    """Remove webhook URLs from persisted config — secrets belong in server env only."""
    cleaned = dict(config)
    raw_defaults = cleaned.get("defaults")
    defaults = dict(raw_defaults) if isinstance(raw_defaults, dict) else {}
    defaults.pop("webhook_url", None)
    cleaned["defaults"] = defaults
    cleaned["alerts"] = [
        {k: v for k, v in alert.items() if k != "webhook_url"}
        for alert in alert_rows(cleaned)
        if isinstance(alert, dict)
    ]
    return cleaned


def _is_safe_env_assignment(key: str, value: str) -> bool:
    """Reject keys/values that would split .env lines or inject new assignments."""
    if not key or any(ch in key for ch in ("\r", "\n", "\0", "=")):
        return False
    if any(ch in value for ch in ("\r", "\n", "\0")):
        return False
    return True


def _replace_env_file_with_retry(source: Path, target: Path) -> None:
    """Atomically replace an env file, tolerating brief Windows file locks."""
    attempts = 6
    for attempt in range(attempts):
        try:
            os.replace(source, target)
            return
        except PermissionError:
            if attempt == attempts - 1:
                raise
            time.sleep(0.01 * (attempt + 1))


def update_user_env_vars(updates: Dict[str, str]) -> None:
    """Update ~/.markethelm/.env without logging or returning secret values.

    Serialized per path and written via temp+replace so concurrent settings
    saves cannot drop keys or leave a truncated .env on crash mid-write.
    """
    env_path = user_config_dir() / ".env"
    user_config_dir().mkdir(parents=True, exist_ok=True)
    lock = _env_lock_for(env_path)
    with lock:
        existing: Dict[str, str] = {}
        order: list[str] = []
        if env_path.exists():
            for line in env_path.read_text(encoding="utf-8").splitlines():
                stripped = line.strip()
                if not stripped or stripped.startswith("#") or "=" not in stripped:
                    continue
                key, _, value = stripped.partition("=")
                key = key.strip()
                existing[key] = value.strip()
                if key not in order:
                    order.append(key)
        for key, value in updates.items():
            if not value:
                if key in existing:
                    del existing[key]
                    if key in order:
                        order.remove(key)
                continue
            text = str(value)
            if not _is_safe_env_assignment(str(key), text):
                raise ValueError("Environment updates cannot contain CR/LF/NUL or '=' in keys.")
            existing[str(key)] = text
            if str(key) not in order:
                order.append(str(key))
        content = "\n".join(f"{key}={existing[key]}" for key in order)
        fd, tmp_name = tempfile.mkstemp(
            dir=env_path.parent,
            prefix=f".{env_path.name}.",
            suffix=".tmp",
        )
        os.close(fd)
        tmp = Path(tmp_name)
        try:
            tmp.write_text(f"{content}\n" if content else "", encoding="utf-8")
            _replace_env_file_with_retry(tmp, env_path)
        finally:
            try:
                tmp.unlink(missing_ok=True)
            except OSError:
                pass

"""CLI: markethelm alerts list|test|init|run"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from ..alerts.alert_engine import AlertEngine
from ..alerts.alert_paths import (
    apply_alert_defaults,
    init_user_alerts_config,
    load_alerts_config,
    polish_alerts_config,
    resolve_alerts_config_path,
)
from ..alerts.alert_storage import AlertStorage
from ..alerts.delivery_status import record_notifier_delivery
from ..core.logger import configure_logging, get_logger
from ..storage.database import database_enabled

logger = get_logger(__name__)


def _notifier_label(class_name: str) -> Optional[str]:
    labels = {
        "LogNotifier": None,
        "EmailNotifier": "email",
        "WebhookNotifier": "webhook",
    }
    return labels.get(class_name, class_name.replace("Notifier", "").lower())


def _format_condition(condition: Any) -> str:
    # Hand-edited alerts.json may store null / strings where objects are expected.
    if not isinstance(condition, dict):
        return "?"
    ctype = condition.get("type", "?")
    if ctype == "price_threshold":
        return f"{condition.get('symbol')} {condition.get('operator')} " f"{condition.get('value')}"
    if ctype == "rsi_threshold":
        period = condition.get("period", 14)
        return (
            f"{condition.get('symbol')} RSI({period}) "
            f"{condition.get('operator')} {condition.get('value')}"
        )
    if ctype == "screening_match":
        raw_filters = condition.get("filters", {})
        filters = raw_filters if isinstance(raw_filters, dict) else {}
        parts = [f"{key}={value}" for key, value in filters.items()]
        return "screening: " + ", ".join(parts)
    if ctype == "compound":
        leaves = condition.get("conditions")
        count = len(leaves) if isinstance(leaves, list) else 0
        return f"compound {condition.get('op', 'and')} ({count} conditions)"
    return ctype


def _find_alert(alerts: List[Dict[str, Any]], alert_id: str) -> Optional[Dict[str, Any]]:
    for alert in alerts:
        if alert.get("id") == alert_id:
            return alert
    return None


def _load_env() -> None:
    try:
        from dotenv import load_dotenv
    except ImportError:
        return
    from ..alerts.alert_paths import user_config_dir

    load_dotenv()
    user_env = user_config_dir() / ".env"
    if user_env.exists():
        load_dotenv(user_env, override=True)


def cmd_list(config_path: Optional[Path] = None) -> int:
    path, raw = load_alerts_config(config_path)
    if raw is None:
        if path.exists():
            logger.error(
                "Corrupt or invalid alerts config at %s. Fix JSON or run: "
                "markethelm alerts init --force",
                path,
            )
        else:
            logger.error(
                "No alerts config at %s. Run: markethelm alerts init",
                path,
            )
        return 1

    config = polish_alerts_config(raw)
    alerts = config.get("alerts", [])
    if not alerts:
        logger.info("No alert rules in %s", path)
        return 0

    logger.info("Alerts config: %s", path)
    logger.info("-" * 60)
    for alert in alerts:
        enabled = alert.get("enabled", False)
        status = "enabled" if enabled else "disabled"
        raw_notifications = alert.get("notifications")
        # Non-list notifications are not channel names (and strings join by char).
        # Mixed-type lists (hand-edited JSON) must not TypeError on join.
        if isinstance(raw_notifications, list):
            channels = [
                ch.strip() for ch in raw_notifications if isinstance(ch, str) and ch.strip()
            ]
        else:
            channels = ["log"]
        notifications = ", ".join(channels) if channels else "log"
        condition = _format_condition(alert.get("condition"))
        logger.info(
            "%s  [%s]  %s",
            alert.get("id", "?"),
            status,
            alert.get("name", alert.get("id", "?")),
        )
        logger.info("  condition: %s", condition)
        logger.info("  notify: %s", notifications)
        if alert.get("webhook_format") or alert.get("payload_format"):
            logger.info(
                "  webhook format: %s",
                alert.get("webhook_format") or alert.get("payload_format"),
            )
        logger.info("")
    return 0


def cmd_init(force: bool = False) -> int:
    try:
        dest = init_user_alerts_config(force=force)
    except FileExistsError:
        from ..alerts.alert_paths import user_config_dir

        logger.error(
            "%s already exists. Use --force to overwrite.",
            user_config_dir() / "alerts.json",
        )
        return 1
    except FileNotFoundError as exc:
        logger.error("%s", exc)
        return 1

    logger.info("Created %s", dest)
    logger.info("Edit the file, set enabled rules to true, and configure SMTP/webhook env vars.")
    logger.info("Secrets (SMTP password, webhook URLs) belong in ~/.markethelm/.env — not in git.")
    return 0


def run_alert_test(
    alert_id: str,
    dry_run: bool = False,
    config_path: Optional[Path] = None,
    config: Optional[Dict[str, Any]] = None,
    storage: Optional[Any] = None,
) -> Dict[str, Any]:
    """Send or preview a test notification; returns a structured result for API/CLI."""
    # Hosted multi-user must not copy process-wide ALERT_EMAIL_TO into in-memory
    # defaults (then apply_alert_defaults → notifier). File-mode CLI still seeds.
    seed_env_email = not database_enabled()
    if config is not None:
        polished = polish_alerts_config(config, seed_env_email=seed_env_email)
    else:
        path, raw = load_alerts_config(config_path)
        if raw is None:
            if path.exists():
                raise FileNotFoundError(f"Corrupt or invalid alerts config at {path}")
            raise FileNotFoundError(f"No alerts config at {path}")
        polished = polish_alerts_config(raw, seed_env_email=seed_env_email)

    alert = _find_alert(polished.get("alerts", []), alert_id)
    if alert is None:
        raise ValueError(f"No alert with id {alert_id!r}")

    raw_condition = alert.get("condition")
    condition = raw_condition if isinstance(raw_condition, dict) else {}
    event = {
        "alert_id": alert["id"],
        "alert_name": alert.get("name", alert["id"]),
        "symbols": ["TEST"],
        "timestamp": datetime.utcnow().isoformat(),
        # Null / non-dict condition previously AttributeError'd Settings "Send test".
        "condition_type": condition.get("type", "test"),
        "test": True,
    }

    defaults = polished.get("defaults") or {}
    effective = apply_alert_defaults(alert, defaults)
    engine = AlertEngine([alert], defaults=defaults)
    notifiers = engine._build_notifiers(effective)
    if not notifiers:
        raise RuntimeError(f"No notifiers configured for alert {alert_id!r}")

    storage = storage or AlertStorage()
    delivered: List[str] = []
    attempted: List[str] = []
    previews: List[Dict[str, Any]] = []
    for notifier in notifiers:
        name = notifier.__class__.__name__
        label = _notifier_label(name)
        if label:
            attempted.append(label)
        if dry_run:
            payload: Any = event
            if hasattr(notifier, "build_payload"):
                payload = notifier.build_payload(event)
            previews.append({"notifier": name, "payload": payload})
        else:
            try:
                ok = notifier.send(event) is not False
            except Exception as exc:
                record_notifier_delivery(
                    storage,
                    alert_id=alert_id,
                    notifier=notifier,
                    success=False,
                    test=True,
                    error=str(exc),
                )
                ok = False
            else:
                record_notifier_delivery(
                    storage,
                    alert_id=alert_id,
                    notifier=notifier,
                    success=ok,
                    test=True,
                )
            if ok and label:
                delivered.append(label)

    if not dry_run and attempted and not delivered:
        raise RuntimeError(f"No test notifications were delivered for alert {alert_id!r}")

    return {
        "alert_id": alert_id,
        "dry_run": dry_run,
        "notifiers": (
            delivered
            if not dry_run
            else [label for item in previews if (label := _notifier_label(item["notifier"]))]
        ),
        "previews": previews if dry_run else None,
        "status": "dry_run" if dry_run else "sent",
    }


def cmd_run(*, loop: bool = False, interval: Optional[int] = None) -> int:
    """Evaluate enabled watches once, or run on a schedule until interrupted."""
    from ..alerts.alert_worker import run_worker_loop, run_worker_once

    if loop:
        run_worker_loop(interval)
        return 0

    run_worker_once()
    return 0


def cmd_test(alert_id: str, dry_run: bool = False, config_path: Optional[Path] = None) -> int:
    path = resolve_alerts_config_path(config_path)
    if not path.exists():
        logger.error("No alerts config at %s. Run: markethelm alerts init", path)
        return 1

    try:
        result = run_alert_test(alert_id, dry_run=dry_run, config_path=config_path)
    except (FileNotFoundError, ValueError, RuntimeError) as exc:
        logger.error("%s", exc)
        return 1

    logger.info("Testing alert %r (%s)", alert_id, "dry-run" if dry_run else "live")
    if dry_run and result.get("previews"):
        for preview in result["previews"]:
            logger.info(
                "[%s] would send: %s",
                preview["notifier"],
                json.dumps(preview["payload"], indent=2),
            )
    else:
        for name in result.get("notifiers") or []:
            logger.info("[%s] sent test notification", name)
    return 0


def main(argv: Optional[List[str]] = None) -> None:
    parser = argparse.ArgumentParser(
        prog="markethelm alerts",
        description="Manage MarketHelm alert rules (config + test notifications)",
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=None,
        help="Path to alerts.json (default: ~/.markethelm/alerts.json or repo config)",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("list", help="List configured alert rules")

    init_parser = sub.add_parser(
        "init", help="Create ~/.markethelm/alerts.json from the bundled example"
    )
    init_parser.add_argument(
        "--force",
        action="store_true",
        help="Overwrite an existing user alerts file",
    )

    test_parser = sub.add_parser(
        "test", help="Send a test notification for one rule (no market check)"
    )
    test_parser.add_argument("--id", required=True, help="Alert id from alerts.json")
    test_parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print payloads instead of delivering notifications",
    )

    run_parser = sub.add_parser(
        "run",
        help="Evaluate enabled watches against latest market data",
    )
    run_parser.add_argument(
        "--loop",
        action="store_true",
        help="Run until interrupted (scheduled worker)",
    )
    run_parser.add_argument(
        "--interval",
        type=int,
        default=None,
        metavar="SECONDS",
        help="Seconds between checks when --loop (min 60; default 300 or "
        "ALERT_CHECK_INTERVAL_SECONDS)",
    )

    args = parser.parse_args(argv)
    _load_env()
    configure_logging()

    if args.command == "list":
        sys.exit(cmd_list(args.config))
    if args.command == "init":
        sys.exit(cmd_init(force=args.force))
    if args.command == "test":
        sys.exit(cmd_test(args.id, dry_run=args.dry_run, config_path=args.config))
    if args.command == "run":
        sys.exit(cmd_run(loop=args.loop, interval=args.interval))

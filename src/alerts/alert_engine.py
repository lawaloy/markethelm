"""
Core alert engine.
"""

import json
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional

from src.utils.company_names import _clean_display_name

from ..core.logger import get_logger
from .alert_paths import apply_alert_defaults, resolve_alerts_config_path
from .alert_rules import (
    collect_rsi_symbols,
    evaluate_compound,
    evaluate_leaf_symbols,
)
from .alert_storage import AlertStorage
from .delivery_status import record_notifier_delivery
from .notifiers.email_notifier import EmailNotifier
from .notifiers.webhook_notifier import WebhookNotifier
from .price_history import closes_by_symbol

logger = get_logger(__name__)


class LogNotifier:
    def send(self, event: Dict) -> bool:
        logger.info(
            f"Alert triggered: {event['alert_name']} ({event['alert_id']}) "
            f"symbols={event.get('symbols', [])}"
        )
        return True


NOTIFIERS = {
    "log": LogNotifier,
}


class AlertEngine:
    def __init__(
        self,
        alerts: List[Dict],
        storage: Optional[AlertStorage] = None,
        defaults: Optional[Dict[str, Any]] = None,
    ):
        self.alerts = alerts
        self.storage = storage or AlertStorage()
        self.defaults = defaults or {}

    @staticmethod
    def from_config_dict(
        config: Dict[str, Any],
        storage: Optional[AlertStorage] = None,
    ) -> Optional["AlertEngine"]:
        # Hand-edited configs may be a list/string/null root or mix non-dict
        # alert rows; never AttributeError the whole check run.
        if not isinstance(config, dict):
            return None
        defaults = config.get("defaults") or {}
        if not isinstance(defaults, dict):
            defaults = {}
        alerts = config.get("alerts", [])
        if not isinstance(alerts, list):
            return None
        enabled = []
        for alert in alerts:
            if not isinstance(alert, dict) or not alert.get("enabled", False):
                continue
            # Hand-edited configs may omit id; drop them at load so evaluate
            # never KeyError mid-run and abort sibling watches.
            alert_id = alert.get("id")
            if not isinstance(alert_id, str) or not alert_id.strip():
                logger.warning("Skipping enabled alert with missing/blank id")
                continue
            enabled.append(alert)
        if not enabled:
            return None
        return AlertEngine(enabled, storage=storage, defaults=defaults)

    @staticmethod
    def from_config(config_path: Optional[Path] = None) -> Optional["AlertEngine"]:
        config_path = resolve_alerts_config_path(config_path)
        if not config_path.exists():
            return None
        try:
            with open(config_path, "r") as f:
                config = json.load(f)
        except Exception as e:
            logger.warning(f"Failed to load alerts config: {e}")
            return None
        if not isinstance(config, dict):
            logger.warning("Alerts config root must be an object; ignoring")
            return None
        return AlertEngine.from_config_dict(config)

    def deliver_event(self, alert: Dict, event: Dict) -> bool:
        """Send notifications for a matched watch (used by the delivery job worker)."""
        # Settings always prepends "log" via buildNotifications. A successful
        # log write must not complete the job when email/webhook were attempted
        # and failed — that skipped retries and lost the user-facing send.
        user_channel_attempted = False
        user_channel_ok = False
        log_ok = False
        is_test = bool(event.get("test"))
        for notifier in self._build_notifiers(alert):
            is_user_channel = isinstance(notifier, (EmailNotifier, WebhookNotifier))
            if is_user_channel:
                user_channel_attempted = True
            try:
                result = notifier.send(event)
            except Exception as exc:
                logger.warning(
                    "Notifier %s failed for alert %s: %s",
                    notifier.__class__.__name__,
                    alert["id"],
                    exc,
                )
                record_notifier_delivery(
                    self.storage,
                    alert_id=alert["id"],
                    notifier=notifier,
                    success=False,
                    test=is_test,
                    error=str(exc),
                )
                continue
            success = result is not False
            record_notifier_delivery(
                self.storage,
                alert_id=alert["id"],
                notifier=notifier,
                success=success,
                test=is_test,
            )
            if success:
                if is_user_channel:
                    user_channel_ok = True
                else:
                    log_ok = True

        delivered = user_channel_ok if user_channel_attempted else log_ok

        if not delivered:
            return False

        # Notifiers already succeeded — a history write failure must not raise
        # and cause job retries / duplicate sends, or abort sibling watches.
        try:
            self.storage.record_event(event)
        except Exception as exc:
            logger.warning(
                "Alert %s delivered but event history write failed: %s",
                alert.get("id"),
                exc,
            )
        return True

    def _within_cooldown(self, alert: Dict) -> bool:
        try:
            cooldown_minutes = int(alert.get("cooldown_minutes", 0) or 0)
        except (TypeError, ValueError, OverflowError):
            # File-mode configs may carry junk / Inf cooldown values; treat as no
            # cooldown so one bad alert cannot abort evaluation of sibling watches.
            logger.warning(
                "Invalid cooldown_minutes on alert %s; treating as 0",
                alert.get("id"),
            )
            return False
        if cooldown_minutes <= 0:
            return False
        last_triggered = self.storage.get_last_triggered(alert["id"])
        if not last_triggered:
            return False
        now = datetime.now(last_triggered.tzinfo) if last_triggered.tzinfo else datetime.utcnow()
        try:
            window = timedelta(minutes=cooldown_minutes)
        except OverflowError:
            # Huge finite cooldown (e.g. 1e15) must not abort sibling watches.
            logger.warning(
                "Overflow cooldown_minutes on alert %s; treating as in cooldown",
                alert.get("id"),
            )
            return True
        return now - last_triggered < window

    def _build_notifiers(self, alert: Dict) -> List[Any]:
        alert = apply_alert_defaults(alert, self.defaults)
        raw_notifications = alert.get("notifications")
        # Non-lists (e.g. int/str) are not iterable channel names; strings also
        # make `"email" in notifications` true via substring membership.
        notifier_names = raw_notifications if isinstance(raw_notifications, list) else ["log"]
        if not notifier_names:
            notifier_names = ["log"]
        instances: List[Any] = []
        for name in notifier_names:
            if name == "log":
                instances.append(LogNotifier())
            elif name == "webhook":
                webhook = WebhookNotifier.from_alert(alert)
                if webhook:
                    instances.append(webhook)
            elif name == "email":
                email = EmailNotifier.from_alert(alert)
                if email:
                    instances.append(email)
            else:
                notifier_cls = NOTIFIERS.get(name)
                if notifier_cls:
                    instances.append(notifier_cls())
                else:
                    logger.warning(f"Unknown notifier '{name}', skipping")
        if not instances:
            instances.append(LogNotifier())
        return instances

    def _closes_for_evaluate(self, stocks: List[Dict]) -> Dict[str, List[float]]:
        """Cached per evaluate() call so sibling RSI watches share one disk pass."""
        cached = getattr(self, "_evaluate_closes_cache", None)
        if cached is not None:
            return cached
        symbols = collect_rsi_symbols(self.alerts)
        history = closes_by_symbol(symbols, stocks) if symbols else {}
        self._evaluate_closes_cache = history
        return history

    def evaluate(self, stocks: List[Dict]) -> List[Dict]:
        events: List[Dict] = []
        # Hand-edited / corrupt runners can pass None/dict; iterating must not
        # TypeError and abort the entire alert check cycle.
        if not isinstance(stocks, list):
            logger.warning("Alert evaluate received non-list stocks; skipping")
            return events
        self._evaluate_closes_cache = None
        for alert in self.alerts:
            alert_id = alert.get("id")
            if not isinstance(alert_id, str) or not alert_id.strip():
                # Direct construction / stale in-memory configs can still lack
                # id; skip so a KeyError cannot abort later watches.
                logger.warning("Alert missing id; skipping")
                continue

            if self._within_cooldown(alert):
                continue

            condition = alert.get("condition", {})
            if not isinstance(condition, dict):
                logger.warning(
                    "Alert %s has non-object condition; skipping",
                    alert_id,
                )
                continue
            condition_type = condition.get("type")
            triggered_symbols: List[str] = []

            if condition_type in {"price_threshold", "rsi_threshold", "screening_match"}:
                # Lazy-load close history once per evaluate() when any RSI leaf needs it.
                history = self._closes_for_evaluate(stocks)
                triggered_symbols = evaluate_leaf_symbols(
                    condition, stocks, closes_by_symbol=history
                )
            elif condition_type == "compound":
                history = self._closes_for_evaluate(stocks)
                triggered_symbols = evaluate_compound(condition, stocks, closes_by_symbol=history)
            else:
                logger.warning(f"Unsupported alert condition: {condition_type}")
                continue

            if not triggered_symbols:
                continue

            # Float NaN names stringify to "nan" and leak into email/webhook copy.
            alert_name = _clean_display_name(alert.get("name")) or alert_id
            event = {
                "alert_id": alert_id,
                "alert_name": alert_name,
                "symbols": triggered_symbols,
                "timestamp": datetime.utcnow().isoformat(),
                "condition_type": condition_type,
            }

            if not self.deliver_event(alert, event):
                logger.warning(
                    "Alert %s matched but no notifications were delivered; "
                    "leaving it eligible for retry.",
                    alert_id,
                )
                continue

            events.append(event)

        return events

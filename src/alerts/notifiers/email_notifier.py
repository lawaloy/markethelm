"""
Send alert events via email (SMTP, SendGrid, or Mailgun).
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from ...core.logger import get_logger
from .email_delivery import (
    EmailDeliveryBackend,
    _platform_from_address,
    _resolve_recipients,
    build_email_backend,
    format_alert_email,
    parse_recipients,
)

logger = get_logger(__name__)

# Re-export for tests and callers that imported helpers from this module.
_parse_recipients = parse_recipients


class EmailNotifier:
    """Deliver alert events as plain-text email."""

    def __init__(
        self,
        backend: EmailDeliveryBackend,
        to_addrs: List[str],
        from_addr: str,
    ) -> None:
        self._backend = backend
        self._to_addrs = to_addrs
        self._from_addr = from_addr

    @classmethod
    def from_alert(cls, alert: Dict[str, Any]) -> Optional["EmailNotifier"]:
        to_addrs = _resolve_recipients(alert)
        if not to_addrs:
            logger.warning(
                "Email notifier requested but no recipients: set 'email_to' on the alert "
                "or ALERT_EMAIL_TO in the environment."
            )
            return None

        from_addr = _platform_from_address(alert)
        if not from_addr:
            logger.warning(
                "Email notifier requested but no From address: set ALERT_EMAIL_FROM "
                "(required for SendGrid/Mailgun) or SMTP_USER for SMTP."
            )
            return None

        backend = build_email_backend(alert)
        if backend is None:
            return None

        return cls(backend=backend, to_addrs=to_addrs, from_addr=from_addr)

    def send(self, event: Dict[str, Any]) -> bool:
        subject, body = format_alert_email(event)
        return self._backend.send(
            subject=subject,
            body=body,
            from_addr=self._from_addr,
            to_addrs=self._to_addrs,
            event=event,
        )

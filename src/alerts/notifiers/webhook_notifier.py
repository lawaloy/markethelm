"""
POST alert events to an HTTPS webhook (Slack-compatible, Discord, or custom JSON).
"""

from __future__ import annotations

import ipaddress
import os
import socket
from typing import Any, Dict, Optional, Union
from urllib.parse import unquote, urlparse

import requests

import src.storage.database as database

from ...core.logger import get_logger
from .delivery_retry import DeliveryAttempt, deliver_with_retry, is_retriable_http_status

logger = get_logger(__name__)

_BLOCKED_WEBHOOK_HOSTS = frozenset(
    {
        "localhost",
        "metadata.google.internal",
        "metadata",
    }
)

# requests.PreparedRequest percent-decodes the host before connecting.
# Bound the unwrap so a crafted ``%25%25...`` chain cannot spin.
_MAX_HOST_UNQUOTE_ROUNDS = 4


def _unquote_hostname(host: str) -> str:
    """Unwrap percent-encoded host labels the way HTTP clients do.

    ``urlparse().hostname`` leaves ``%31%32%37.%30.%30.%31`` encoded, but
    ``requests`` prepares that URL as ``127.0.0.1`` and connects to loopback.
    """
    current = host
    for _ in range(_MAX_HOST_UNQUOTE_ROUNDS):
        decoded = unquote(current)
        if decoded == current:
            break
        current = decoded
    return current.lower().rstrip(".")


def _literal_ip(
    host: str,
) -> Optional[Union[ipaddress.IPv4Address, ipaddress.IPv6Address]]:
    """Parse *host* as an IP, including libc-accepted IPv4 shorthands.

    ``ipaddress.ip_address`` rejects decimal / hex / octal / short forms
    (``2130706433``, ``0x7f000001``, ``0177.0.0.1``, ``127.1``) that
    ``getaddrinfo`` still maps to loopback or link-local. ``inet_aton``
    matches that resolver so we do not treat those hosts as DNS names.
    """
    try:
        return ipaddress.ip_address(host)
    except ValueError:
        pass
    try:
        return ipaddress.IPv4Address(socket.inet_aton(host))
    except (OSError, UnicodeError):
        return None


def _ip_is_public(ip: Union[ipaddress.IPv4Address, ipaddress.IPv6Address]) -> bool:
    """True when *ip* is globally routable, including via IPv4-mapped IPv6.

    CPython reports ``::ffff:100.64.0.1`` as global even though the embedded
    CGNAT address is not (``IPv4Address.is_global`` excludes RFC 6598).
    Connecting to the mapped form is the same as connecting to that IPv4.
    """
    mapped = getattr(ip, "ipv4_mapped", None)
    if mapped is not None:
        ip = mapped
    return bool(ip.is_global)


def is_safe_webhook_url(url: str) -> bool:
    """Return True when *url* is an HTTPS endpoint that is not an obvious SSRF target.

    Checks the literal hostname/IP only (no DNS resolution) so validation stays
    deterministic. Rejects non-HTTPS schemes, credentials in the URL, loopback /
    private / link-local / unspecified / CGNAT IP literals (including IPv4-mapped,
    decimal/hex/octal/short IPv4 forms, percent-encoded host labels, and DNS-style
    trailing dots), and a small set of well-known metadata hostnames.
    """
    cleaned = (url or "").strip()
    if not cleaned:
        return False
    try:
        parsed = urlparse(cleaned)
    except ValueError:
        return False
    if parsed.scheme != "https":
        return False
    if parsed.username is not None or parsed.password is not None:
        return False
    # A terminal dot denotes the same absolute DNS name, but makes inet_aton reject
    # otherwise literal IPv4 (for example 127.0.0.1.). Normalize it before both the
    # blocked-host and literal-IP checks so it cannot turn a private target into a
    # seemingly ordinary hostname. Percent-decode as well: urlparse leaves
    # ``%31%32%37.%30.%30.%31`` encoded while requests connects to 127.0.0.1.
    host = _unquote_hostname((parsed.hostname or "").lower().rstrip("."))
    if not host:
        return False
    if host in _BLOCKED_WEBHOOK_HOSTS or host.endswith(".localhost"):
        return False
    try:
        host.encode("idna")
    except UnicodeError:
        return False
    ip = _literal_ip(host)
    if ip is None:
        return True
    return _ip_is_public(ip)


class WebhookNotifier:
    """Send alert payloads via HTTP POST."""

    def __init__(
        self,
        url: str,
        payload_format: str = "json",
        timeout: float = 10.0,
    ) -> None:
        self._url = url
        self._payload_format = payload_format.lower()
        self._timeout = timeout

    @classmethod
    def from_alert(cls, alert: Dict[str, Any]) -> Optional["WebhookNotifier"]:
        allow_env_webhook = (
            alert.get("_allow_env_webhook", not database.database_enabled()) is not False
        )
        url = (
            alert.get("webhook_url")
            or (os.environ.get("ALERT_WEBHOOK_URL") if allow_env_webhook else None)
            or (os.environ.get("DISCORD_WEBHOOK_URL") if allow_env_webhook else None)
        )
        if not url or not str(url).strip():
            logger.warning(
                "Webhook notifier requested but no URL: set 'webhook_url' on the alert "
                "or ALERT_WEBHOOK_URL in the environment."
            )
            return None
        cleaned_url = str(url).strip()
        if not is_safe_webhook_url(cleaned_url):
            logger.warning(
                "Webhook notifier rejected unsafe URL for alert %s "
                "(require https:// to a public host).",
                alert.get("id") or alert.get("alert_id"),
            )
            return None
        payload_format = (
            alert.get("webhook_format")
            or alert.get("payload_format")
            or (os.environ.get("ALERT_WEBHOOK_FORMAT") if allow_env_webhook else None)
            or "json"
        )
        return cls(url=cleaned_url, payload_format=str(payload_format).strip().lower())

    @staticmethod
    def _format_symbols(raw: Any) -> str:
        """Join symbol lists for display; tolerate None/non-str junk without TypeError."""
        if raw is None:
            return "(none)"
        if isinstance(raw, str):
            cleaned = raw.strip()
            return cleaned or "(none)"
        if not isinstance(raw, (list, tuple)):
            cleaned = str(raw).strip()
            return cleaned or "(none)"
        parts: list[str] = []
        for item in raw:
            if item is None:
                continue
            text = str(item).strip()
            if text:
                parts.append(text)
        return ", ".join(parts) or "(none)"

    @staticmethod
    def _alert_text(event: Dict[str, Any], markdown: str = "slack") -> str:
        symbols = WebhookNotifier._format_symbols(event.get("symbols"))
        alert_name = event.get("alert_name", event.get("alert_id", "alert"))
        test_label = " (test)" if event.get("test") else ""
        condition = event.get("condition_type", "")
        timestamp = event.get("timestamp", "")
        if markdown == "discord":
            return (
                f"**MarketHelm alert{test_label}:** {alert_name}\n"
                f"**Symbols:** {symbols}\n"
                f"**Condition:** {condition}\n"
                f"**Time (UTC):** {timestamp}"
            )
        return (
            f"*MarketHelm alert{test_label}:* {alert_name}\n"
            f"*Symbols:* {symbols}\n"
            f"*Condition:* {condition}\n"
            f"*Time (UTC):* {timestamp}"
        )

    def build_payload(self, event: Dict[str, Any]) -> Dict[str, Any]:
        if self._payload_format == "slack":
            text = self._alert_text(event, markdown="slack")
            return {
                "text": text,
                "blocks": [
                    {
                        "type": "section",
                        "text": {"type": "mrkdwn", "text": text},
                    }
                ],
            }
        if self._payload_format == "discord":
            return {"content": self._alert_text(event, markdown="discord")}
        return dict(event)

    def _post_once(self, event: Dict[str, Any]) -> DeliveryAttempt:
        payload = self.build_payload(event)
        try:
            response = requests.post(
                self._url,
                json=payload,
                headers={"Content-Type": "application/json"},
                timeout=self._timeout,
                # A public HTTPS URL can 30x onto loopback/metadata; do not follow.
                allow_redirects=False,
            )
            if 200 <= response.status_code < 300:
                return DeliveryAttempt(ok=True)
            logger.warning(
                "Webhook returned %s for alert %s: %s",
                response.status_code,
                event.get("alert_id"),
                response.text[:500],
            )
            return DeliveryAttempt(
                ok=False,
                retriable=is_retriable_http_status(response.status_code),
            )
        except requests.RequestException as exc:
            logger.warning(
                "Webhook delivery failed for alert %s: %s",
                event.get("alert_id"),
                exc,
            )
            return DeliveryAttempt(ok=False, retriable=True)

    def send(self, event: Dict[str, Any]) -> bool:
        return deliver_with_retry(
            operation="Webhook",
            alert_id=event.get("alert_id"),
            attempt=lambda: self._post_once(event),
        )

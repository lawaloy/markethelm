"""
Transactional email delivery backends for alert notifications.

Supports SMTP (dev / self-host / SES SMTP relay), SendGrid, and Mailgun.
Provider is selected via ALERT_EMAIL_PROVIDER or inferred from env secrets.
"""

from __future__ import annotations

import os
import smtplib
from abc import ABC, abstractmethod
from email.message import EmailMessage
from typing import Any, Dict, List, Optional, Union
from urllib.parse import urlparse

import requests

import src.storage.database as database

from ...core.logger import get_logger
from .delivery_retry import DeliveryAttempt, deliver_with_retry, is_retriable_http_status

logger = get_logger(__name__)

SUPPORTED_PROVIDERS = frozenset({"smtp", "sendgrid", "mailgun"})
# Official Mailgun API origins only — never send Basic Auth API keys to arbitrary hosts.
ALLOWED_MAILGUN_API_HOSTS = frozenset({"api.mailgun.net", "api.eu.mailgun.net"})


def normalize_mailgun_api_base(raw: str) -> Optional[str]:
    """Return a canonical https Mailgun API origin, or None if unsafe/invalid."""
    text = str(raw).strip()
    if not text:
        return None
    parsed = urlparse(text)
    if parsed.scheme.lower() != "https":
        return None
    if parsed.username is not None or parsed.password is not None:
        return None
    host = (parsed.hostname or "").lower()
    if host not in ALLOWED_MAILGUN_API_HOSTS:
        return None
    if parsed.port not in (None, 443):
        return None
    if parsed.path not in ("", "/") or parsed.query or parsed.fragment:
        return None
    return f"https://{host}"


def normalize_mailgun_domain(raw: str) -> Optional[str]:
    """Return a hostname-like Mailgun sending domain, or None if unsafe/invalid.

    The domain is interpolated into ``{api_base}/v3/{domain}/messages``. Values with
    ``/``, ``@``, whitespace, or control characters reshape the request path and
    must be rejected before authenticated Mailgun calls are made.
    """
    text = str(raw).strip()
    if not text:
        return None
    if any(ch in text for ch in ("/", "\\", "@", " ", "\t", "\r", "\n", "\0")):
        return None
    # Hostname labels: letters, digits, dots, hyphens only (Mailgun sandbox/custom).
    if text.startswith(".") or text.endswith(".") or ".." in text:
        return None
    for label in text.split("."):
        if not label or label.startswith("-") or label.endswith("-"):
            return None
        if not all(ch.isalnum() or ch == "-" for ch in label):
            return None
    return text.lower()


def _sanitize_header_text(value: Any) -> str:
    """Strip CR/LF so alert names cannot split email headers or spoof fields."""
    return str(value or "").replace("\r", " ").replace("\n", " ").strip()


def _is_safe_email_address(addr: str) -> bool:
    """Reject addresses that carry control chars usable for header injection."""
    if not addr or any(ch in addr for ch in ("\r", "\n", "\0")):
        return False
    # Extremely loose shape check: local@domain with no spaces.
    if " " in addr or addr.count("@") != 1:
        return False
    local, _, domain = addr.partition("@")
    return bool(local) and bool(domain)


def _safe_from_address(value: Any) -> Optional[str]:
    """Return a From address only when it cannot inject CR/LF into headers."""
    if value is None:
        return None
    cleaned = str(value).strip()
    if not cleaned or any(ch in cleaned for ch in ("\r", "\n", "\0")):
        return None
    return cleaned


def parse_recipients(value: Union[str, List[str], None]) -> List[str]:
    if not value:
        return []
    if isinstance(value, list):
        raw_parts = [str(item).strip() for item in value if str(item).strip()]
    else:
        raw_parts = [part.strip() for part in str(value).split(",") if part.strip()]
    return [part for part in raw_parts if _is_safe_email_address(part)]


def env_bool(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def resolve_email_provider() -> str:
    explicit = (os.environ.get("ALERT_EMAIL_PROVIDER") or "").strip().lower()
    if explicit:
        if explicit not in SUPPORTED_PROVIDERS:
            logger.warning(
                "Unknown ALERT_EMAIL_PROVIDER=%r; supported: %s. Falling back to auto-detect.",
                explicit,
                ", ".join(sorted(SUPPORTED_PROVIDERS)),
            )
        else:
            return explicit

    if os.environ.get("SENDGRID_API_KEY"):
        return "sendgrid"
    if os.environ.get("MAILGUN_API_KEY") and os.environ.get("MAILGUN_DOMAIN"):
        return "mailgun"
    return "smtp"


def email_delivery_configured() -> bool:
    """True when the active provider has enough env config to send email."""
    provider = resolve_email_provider()
    if provider == "sendgrid":
        return bool(os.environ.get("SENDGRID_API_KEY") and _platform_from_address())
    if provider == "mailgun":
        return bool(
            os.environ.get("MAILGUN_API_KEY")
            and os.environ.get("MAILGUN_DOMAIN")
            and _platform_from_address()
        )
    return bool(
        os.environ.get("SMTP_HOST")
        and os.environ.get("SMTP_USER")
        and os.environ.get("SMTP_PASSWORD")
    )


def _format_symbols(raw: Any) -> str:
    """Join symbol lists for email body; tolerate None/non-str junk without TypeError."""
    if raw is None:
        return "(none)"
    if isinstance(raw, str):
        cleaned = raw.strip()
        return cleaned or "(none)"
    if not isinstance(raw, (list, tuple)):
        cleaned = str(raw).strip()
        return cleaned or "(none)"
    parts: List[str] = []
    for item in raw:
        if item is None:
            continue
        text = str(item).strip()
        if text:
            parts.append(text)
    return ", ".join(parts) or "(none)"


def format_alert_email(event: Dict[str, Any]) -> tuple[str, str]:
    symbols = _format_symbols(event.get("symbols"))
    alert_name = (
        _sanitize_header_text(event.get("alert_name", event.get("alert_id", "alert"))) or "alert"
    )
    subject = f"MarketHelm alert: {alert_name}"
    body = "\n".join(
        [
            f"Alert: {alert_name}",
            f"ID: {_sanitize_header_text(event.get('alert_id', ''))}",
            f"Symbols: {symbols}",
            f"Condition: {_sanitize_header_text(event.get('condition_type', ''))}",
            f"Time (UTC): {_sanitize_header_text(event.get('timestamp', ''))}",
            "",
            "— MarketHelm",
        ]
    )
    return subject, body


def _allow_alert_smtp_overrides(alert: Optional[Dict[str, Any]]) -> bool:
    """Whether per-alert SMTP/from fields may override platform env.

    Hosted multi-user mode must not let tenants redirect SMTP transport or
    spoof From (mirrors webhook/recipient ``_allow_env_*`` isolation). Opt in
    with ``_allow_alert_smtp=True`` for intentional self-host style overrides.
    """

    if not alert:
        return True
    return alert.get("_allow_alert_smtp", not database.database_enabled()) is not False


def _platform_from_address(alert: Optional[Dict[str, Any]] = None) -> Optional[str]:
    if alert and _allow_alert_smtp_overrides(alert):
        from_alert = _safe_from_address(alert.get("email_from"))
        if from_alert:
            return from_alert
    from_env = _safe_from_address(os.environ.get("ALERT_EMAIL_FROM"))
    if from_env:
        return from_env
    if resolve_email_provider() == "smtp":
        if alert and _allow_alert_smtp_overrides(alert):
            smtp_user = _safe_from_address(alert.get("smtp_user"))
            if smtp_user:
                return smtp_user
        username = _safe_from_address(os.environ.get("SMTP_USER"))
        if username:
            return username
    return None


def _resolve_recipients(alert: Dict[str, Any]) -> List[str]:
    """Resolve To: addresses, preferring per-alert recipients.

    Hosted multi-user mode must not fall back to process-wide ``ALERT_EMAIL_TO``
    (mirrors webhook ``_allow_env_webhook`` isolation). Opt in with
    ``_allow_env_email=True`` when intentionally using the shared env mailbox.
    """

    allow_env_email = alert.get("_allow_env_email", not database.database_enabled()) is not False
    env_recipients = parse_recipients(os.environ.get("ALERT_EMAIL_TO")) if allow_env_email else []
    return parse_recipients(alert.get("email_to")) or env_recipients


class EmailDeliveryBackend(ABC):
    @abstractmethod
    def send(
        self,
        *,
        subject: str,
        body: str,
        from_addr: str,
        to_addrs: List[str],
        event: Dict[str, Any],
    ) -> bool: ...


class SmtpEmailBackend(EmailDeliveryBackend):
    def __init__(
        self,
        host: str,
        port: int,
        username: str,
        password: str,
        use_tls: bool = True,
        use_ssl: bool = False,
        timeout: float = 15.0,
    ) -> None:
        self._host = host
        self._port = port
        self._username = username
        self._password = password
        self._use_tls = use_tls
        self._use_ssl = use_ssl
        self._timeout = timeout

    def _send_once(
        self,
        *,
        subject: str,
        body: str,
        from_addr: str,
        to_addrs: List[str],
        event: Dict[str, Any],
    ) -> DeliveryAttempt:
        message = EmailMessage()
        message["Subject"] = subject
        message["From"] = from_addr
        message["To"] = ", ".join(to_addrs)
        message.set_content(body)
        try:
            if self._use_ssl:
                with smtplib.SMTP_SSL(self._host, self._port, timeout=self._timeout) as smtp:
                    smtp.login(self._username, self._password)
                    smtp.send_message(message)
            else:
                with smtplib.SMTP(self._host, self._port, timeout=self._timeout) as smtp:
                    if self._use_tls:
                        smtp.starttls()
                    smtp.login(self._username, self._password)
                    smtp.send_message(message)
            return DeliveryAttempt(ok=True)
        except (smtplib.SMTPException, OSError) as exc:
            logger.warning(
                "SMTP email delivery failed for alert %s: %s",
                event.get("alert_id"),
                exc,
            )
            return DeliveryAttempt(ok=False, retriable=True)

    def send(
        self,
        *,
        subject: str,
        body: str,
        from_addr: str,
        to_addrs: List[str],
        event: Dict[str, Any],
    ) -> bool:
        return deliver_with_retry(
            operation="SMTP email",
            alert_id=event.get("alert_id"),
            attempt=lambda: self._send_once(
                subject=subject,
                body=body,
                from_addr=from_addr,
                to_addrs=to_addrs,
                event=event,
            ),
        )


class SendGridEmailBackend(EmailDeliveryBackend):
    def __init__(self, api_key: str, timeout: float = 15.0) -> None:
        self._api_key = api_key
        self._timeout = timeout

    def _send_once(
        self,
        *,
        subject: str,
        body: str,
        from_addr: str,
        to_addrs: List[str],
        event: Dict[str, Any],
    ) -> DeliveryAttempt:
        payload = {
            "personalizations": [{"to": [{"email": addr} for addr in to_addrs]}],
            "from": {"email": from_addr},
            "subject": subject,
            "content": [{"type": "text/plain", "value": body}],
        }
        try:
            response = requests.post(
                "https://api.sendgrid.com/v3/mail/send",
                headers={
                    "Authorization": f"Bearer {self._api_key}",
                    "Content-Type": "application/json",
                },
                json=payload,
                timeout=self._timeout,
            )
            if response.status_code in (200, 202):
                return DeliveryAttempt(ok=True)
            logger.warning(
                "SendGrid email delivery failed for alert %s: HTTP %s %s",
                event.get("alert_id"),
                response.status_code,
                response.text[:500],
            )
            return DeliveryAttempt(
                ok=False,
                retriable=is_retriable_http_status(response.status_code),
            )
        except requests.RequestException as exc:
            logger.warning(
                "SendGrid email delivery failed for alert %s: %s",
                event.get("alert_id"),
                exc,
            )
            return DeliveryAttempt(ok=False, retriable=True)

    def send(
        self,
        *,
        subject: str,
        body: str,
        from_addr: str,
        to_addrs: List[str],
        event: Dict[str, Any],
    ) -> bool:
        return deliver_with_retry(
            operation="SendGrid email",
            alert_id=event.get("alert_id"),
            attempt=lambda: self._send_once(
                subject=subject,
                body=body,
                from_addr=from_addr,
                to_addrs=to_addrs,
                event=event,
            ),
        )


class MailgunEmailBackend(EmailDeliveryBackend):
    def __init__(
        self,
        api_key: str,
        domain: str,
        api_base: str = "https://api.mailgun.net",
        timeout: float = 15.0,
    ) -> None:
        self._api_key = api_key
        self._domain = domain
        self._api_base = api_base.rstrip("/")
        self._timeout = timeout

    def _send_once(
        self,
        *,
        subject: str,
        body: str,
        from_addr: str,
        to_addrs: List[str],
        event: Dict[str, Any],
    ) -> DeliveryAttempt:
        url = f"{self._api_base}/v3/{self._domain}/messages"
        data = {
            "from": from_addr,
            "to": to_addrs,
            "subject": subject,
            "text": body,
        }
        try:
            response = requests.post(
                url,
                auth=("api", self._api_key),
                data=data,
                timeout=self._timeout,
            )
            if response.status_code in (200, 202):
                return DeliveryAttempt(ok=True)
            logger.warning(
                "Mailgun email delivery failed for alert %s: HTTP %s %s",
                event.get("alert_id"),
                response.status_code,
                response.text[:500],
            )
            return DeliveryAttempt(
                ok=False,
                retriable=is_retriable_http_status(response.status_code),
            )
        except requests.RequestException as exc:
            logger.warning(
                "Mailgun email delivery failed for alert %s: %s",
                event.get("alert_id"),
                exc,
            )
            return DeliveryAttempt(ok=False, retriable=True)

    def send(
        self,
        *,
        subject: str,
        body: str,
        from_addr: str,
        to_addrs: List[str],
        event: Dict[str, Any],
    ) -> bool:
        return deliver_with_retry(
            operation="Mailgun email",
            alert_id=event.get("alert_id"),
            attempt=lambda: self._send_once(
                subject=subject,
                body=body,
                from_addr=from_addr,
                to_addrs=to_addrs,
                event=event,
            ),
        )


def build_smtp_backend(alert: Dict[str, Any]) -> Optional[SmtpEmailBackend]:
    use_overrides = _allow_alert_smtp_overrides(alert)
    host = (alert.get("smtp_host") if use_overrides else None) or os.environ.get("SMTP_HOST")
    # Do not use `or` for port — 0 is falsy but must be rejected as invalid,
    # not silently replaced by the SMTP_PORT default.
    port_raw = alert.get("smtp_port") if use_overrides else None
    if port_raw is None or (isinstance(port_raw, str) and not str(port_raw).strip()):
        port_raw = os.environ.get("SMTP_PORT", "587")
    username = (alert.get("smtp_user") if use_overrides else None) or os.environ.get("SMTP_USER")
    password = (alert.get("smtp_password") if use_overrides else None) or os.environ.get(
        "SMTP_PASSWORD"
    )

    if not host or not str(host).strip():
        logger.warning(
            "Email notifier requested but SMTP_HOST is missing: set 'smtp_host' on the alert "
            "or SMTP_HOST in the environment."
        )
        return None
    if not username or not password:
        logger.warning(
            "Email notifier requested but SMTP credentials are missing: set SMTP_USER and "
            "SMTP_PASSWORD (or per-alert smtp_user / smtp_password)."
        )
        return None

    try:
        port = int(port_raw)
    except (TypeError, ValueError, OverflowError):
        logger.warning("Invalid SMTP port %r; expected an integer.", port_raw)
        return None
    if port < 1 or port > 65535:
        logger.warning("Invalid SMTP port %r; expected 1..65535.", port_raw)
        return None

    use_ssl = env_bool("SMTP_USE_SSL") or port == 465
    use_tls = not use_ssl and env_bool("SMTP_USE_TLS", default=port == 587)

    return SmtpEmailBackend(
        host=str(host).strip(),
        port=port,
        username=str(username).strip(),
        password=str(password),
        use_tls=use_tls,
        use_ssl=use_ssl,
    )


def build_sendgrid_backend() -> Optional[SendGridEmailBackend]:
    api_key = os.environ.get("SENDGRID_API_KEY")
    if not api_key or not str(api_key).strip():
        logger.warning("SendGrid email requested but SENDGRID_API_KEY is missing.")
        return None
    return SendGridEmailBackend(api_key=str(api_key).strip())


def build_mailgun_backend() -> Optional[MailgunEmailBackend]:
    api_key = os.environ.get("MAILGUN_API_KEY")
    domain_raw = os.environ.get("MAILGUN_DOMAIN")
    if not api_key or not str(api_key).strip():
        logger.warning("Mailgun email requested but MAILGUN_API_KEY is missing.")
        return None
    if not domain_raw or not str(domain_raw).strip():
        logger.warning("Mailgun email requested but MAILGUN_DOMAIN is missing.")
        return None
    domain = normalize_mailgun_domain(str(domain_raw))
    if domain is None:
        logger.warning(
            "MAILGUN_DOMAIN %r is not a valid hostname-like sending domain.",
            domain_raw,
        )
        return None
    api_base_raw = os.environ.get("MAILGUN_API_BASE", "https://api.mailgun.net")
    api_base = normalize_mailgun_api_base(str(api_base_raw))
    if api_base is None:
        logger.warning(
            "MAILGUN_API_BASE %r is not an allowed Mailgun HTTPS API origin "
            "(expected https://api.mailgun.net or https://api.eu.mailgun.net).",
            api_base_raw,
        )
        return None
    return MailgunEmailBackend(
        api_key=str(api_key).strip(),
        domain=domain,
        api_base=api_base,
    )


def build_email_backend(alert: Dict[str, Any]) -> Optional[EmailDeliveryBackend]:
    provider = resolve_email_provider()
    if provider == "sendgrid":
        return build_sendgrid_backend()
    if provider == "mailgun":
        return build_mailgun_backend()
    return build_smtp_backend(alert)

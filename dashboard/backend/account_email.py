"""Transactional account email built on the platform email transport."""

from __future__ import annotations

import os
from urllib.parse import urljoin, urlparse

from src.alerts.notifiers.email_delivery import _platform_from_address, build_email_backend
from src.core.logger import get_logger

logger = get_logger(__name__)


def send_account_email(*, recipient: str, purpose: str, token: str) -> bool:
    raw_url = (os.environ.get("MARKETHELM_PUBLIC_URL") or "").strip()
    parsed = urlparse(raw_url)
    local_http = parsed.scheme == "http" and parsed.hostname in {"localhost", "127.0.0.1"}
    if (
        (parsed.scheme != "https" and not local_http)
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
    ):
        logger.error("Account email requires a safe MARKETHELM_PUBLIC_URL")
        return False
    base_url = raw_url.rstrip("/") + "/"
    route = "verify-email" if purpose == "verify_email" else "reset-password"
    link = urljoin(base_url, route) + f"?token={token}"
    subject = (
        "Verify your MarketHelm email"
        if purpose == "verify_email"
        else "Reset your MarketHelm password"
    )
    body = (
        f"Open this link to {route.replace('-', ' ')}:\n\n{link}\n\nThis link expires in one hour."
    )
    backend = build_email_backend({"_allow_alert_smtp": False})
    from_addr = _platform_from_address()
    if not backend or not from_addr:
        logger.error("Account email delivery is not configured")
        return False
    return backend.send(
        subject=subject,
        body=body,
        from_addr=from_addr,
        to_addrs=[recipient],
        event={"alert_id": f"account-{purpose}"},
    )

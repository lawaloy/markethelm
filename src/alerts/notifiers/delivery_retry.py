"""Exponential backoff retries for transient alert delivery failures."""

from __future__ import annotations

import math
import os
import time
from dataclasses import dataclass
from typing import Callable, Optional

from ...core.logger import get_logger

logger = get_logger(__name__)

DEFAULT_MAX_ATTEMPTS = 3
DEFAULT_BASE_SECONDS = 1.0
DEFAULT_MAX_SECONDS = 8.0

# Hard ceilings so a poisoned env cannot monopolize an alert worker.
MAX_ATTEMPTS_CEILING = 10
MAX_BASE_SECONDS_CEILING = 60.0
MAX_SECONDS_CEILING = 300.0


@dataclass(frozen=True)
class DeliveryAttempt:
    ok: bool
    retriable: bool = False


@dataclass(frozen=True)
class DeliveryRetrySettings:
    max_attempts: int
    base_seconds: float
    max_seconds: float


def _int_env(name: str, default: int) -> int:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        logger.warning("Invalid %s=%r; using default %s", name, raw, default)
        return default


def _float_env(name: str, default: float) -> float:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        value = float(raw)
    except ValueError:
        logger.warning("Invalid %s=%r; using default %s", name, raw, default)
        return default
    # float("inf") / float("nan") parse, but sleep(inf) raises OverflowError and
    # NaN delays poison retry math — treat non-finite like invalid input.
    if not math.isfinite(value):
        logger.warning("Non-finite %s=%r; using default %s", name, raw, default)
        return default
    return value


def resolve_delivery_retry_settings() -> DeliveryRetrySettings:
    max_attempts = _int_env("ALERT_DELIVERY_MAX_ATTEMPTS", DEFAULT_MAX_ATTEMPTS)
    base_seconds = _float_env("ALERT_DELIVERY_RETRY_BASE_SECONDS", DEFAULT_BASE_SECONDS)
    max_seconds = _float_env("ALERT_DELIVERY_RETRY_MAX_SECONDS", DEFAULT_MAX_SECONDS)
    return DeliveryRetrySettings(
        max_attempts=min(MAX_ATTEMPTS_CEILING, max(1, max_attempts)),
        base_seconds=min(MAX_BASE_SECONDS_CEILING, max(0.0, base_seconds)),
        max_seconds=min(MAX_SECONDS_CEILING, max(0.0, max_seconds)),
    )


def is_retriable_http_status(status_code: int) -> bool:
    if status_code == 429:
        return True
    return status_code >= 500


def deliver_with_retry(
    *,
    operation: str,
    alert_id: Optional[str],
    attempt: Callable[[], DeliveryAttempt],
    settings: Optional[DeliveryRetrySettings] = None,
) -> bool:
    """Run a delivery attempt up to max_attempts times with exponential backoff."""
    settings = settings or resolve_delivery_retry_settings()
    alert_label = alert_id or "?"

    for attempt_num in range(1, settings.max_attempts + 1):
        result = attempt()
        if result.ok:
            if attempt_num > 1:
                logger.info(
                    "%s delivery succeeded for alert %s on attempt %s/%s",
                    operation,
                    alert_label,
                    attempt_num,
                    settings.max_attempts,
                )
            return True

        if not result.retriable or attempt_num >= settings.max_attempts:
            return False

        delay = min(
            settings.max_seconds,
            settings.base_seconds * (2 ** (attempt_num - 1)),
        )
        logger.warning(
            "%s delivery failed for alert %s (attempt %s/%s); retrying in %.1fs",
            operation,
            alert_label,
            attempt_num,
            settings.max_attempts,
            delay,
        )
        time.sleep(delay)

    return False

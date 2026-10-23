"""Process evaluate_symbol and deliver jobs from the alert_jobs queue."""

from __future__ import annotations

import math
from datetime import datetime, timedelta, timezone
from typing import Any, Dict

from src.alerts.alert_engine import AlertEngine
from src.alerts.alert_rules import evaluate_compound, evaluate_leaf_symbols
from src.alerts.price_history import closes_by_symbol
from src.alerts.user_alert_storage import UserAlertStorage
from src.core.logger import get_logger
from src.storage.alert_jobs import (
    JOB_DELIVER,
    JOB_EVALUATE_SYMBOL,
    claim_jobs,
    complete_job,
    enqueue_job,
    fail_job,
    new_worker_id,
)
from src.storage.alert_watches import get_last_triggered as get_raw_triggered
from src.storage.alert_watches import (
    get_watch,
    list_watches_for_symbol,
    restore_trigger_claim,
    try_claim_trigger,
)
from src.storage.database import init_database
from src.utils.tickers import normalize_ticker

logger = get_logger(__name__)


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _event_timestamp(event: Dict[str, Any]) -> datetime | None:
    raw = event.get("timestamp")
    if not raw:
        return None
    try:
        return _as_utc(datetime.fromisoformat(str(raw).replace("Z", "+00:00")))
    except ValueError:
        return None


def _within_cooldown(user_id: str, alert_id: str, cooldown_minutes: int) -> bool:
    if cooldown_minutes <= 0:
        return False
    storage = UserAlertStorage(user_id)
    last = storage.get_last_triggered(alert_id)
    if not last:
        return False
    last = _as_utc(last)
    try:
        window = timedelta(minutes=cooldown_minutes)
    except OverflowError:
        # Poisoned huge cooldown must not abort the per-symbol watch loop.
        # Treat as still cooling down so sibling tenants keep evaluating.
        return True
    return datetime.now(timezone.utc) - last < window


def _process_evaluate_symbol(job: Dict[str, Any]) -> None:
    payload = job["payload"]
    symbol = normalize_ticker(payload.get("symbol"))
    if not symbol:
        logger.warning(
            "Skipping evaluate_symbol job with invalid symbol %r",
            payload.get("symbol"),
        )
        return
    try:
        price = float(payload["price"])
    except (KeyError, TypeError, ValueError):
        logger.warning(
            "Skipping evaluate_symbol job with invalid price %r for %s",
            payload.get("price"),
            symbol,
        )
        return
    if not math.isfinite(price):
        logger.warning(
            "Skipping evaluate_symbol job with non-finite price %r for %s",
            payload.get("price"),
            symbol,
        )
        return
    stock = {"symbol": symbol, "close": price}
    triggered = 0

    for watch in list_watches_for_symbol(symbol):
        user_id = watch["user_id"]
        alert_id = watch["alert_id"]
        alert = watch["alert"]
        if _within_cooldown(user_id, alert_id, watch["cooldown_minutes"]):
            continue

        if not isinstance(alert, dict):
            logger.warning(
                "Skipping non-object alert payload for %s user %s on %s",
                alert_id,
                user_id,
                symbol,
            )
            continue
        condition = alert.get("condition")
        if not isinstance(condition, dict):
            logger.warning(
                "Skipping non-object condition for alert %s user %s on %s",
                alert_id,
                user_id,
                symbol,
            )
            continue
        if watch["condition_type"] not in {
            "price_threshold",
            "rsi_threshold",
            "compound",
        }:
            continue
        try:
            if watch["condition_type"] == "price_threshold":
                matched_symbols = evaluate_leaf_symbols(condition, [stock])
            elif watch["condition_type"] == "rsi_threshold":
                history = closes_by_symbol([symbol], [stock])
                matched_symbols = evaluate_leaf_symbols(
                    condition, [stock], closes_by_symbol=history
                )
            else:
                # Single-symbol compounds are indexed on the shared ticker.
                needed = []
                leaves = condition.get("conditions")
                if isinstance(leaves, list):
                    for leaf in leaves:
                        if isinstance(leaf, dict) and leaf.get("type") == "rsi_threshold":
                            needed.append(symbol)
                history = closes_by_symbol(needed or [], [stock])
                matched_symbols = evaluate_compound(condition, [stock], closes_by_symbol=history)
        except (TypeError, ValueError, AttributeError) as exc:
            logger.warning(
                "Skipping invalid alert %s for user %s on %s: %s",
                alert_id,
                user_id,
                symbol,
                exc,
            )
            continue
        if not matched_symbols:
            continue

        event = {
            "alert_id": alert_id,
            "alert_name": alert.get("name", alert_id),
            "symbols": matched_symbols,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "condition_type": watch["condition_type"],
            "user_id": user_id,
        }
        enqueue_job(
            JOB_DELIVER,
            {
                "user_id": user_id,
                "alert_id": alert_id,
                "event": event,
                "tick_id": payload.get("tick_id"),
            },
        )
        triggered += 1

    logger.info("evaluate_symbol %s: enqueued %s delivery job(s)", symbol, triggered)


def _process_deliver(job: Dict[str, Any]) -> bool:
    payload = job["payload"]
    if not isinstance(payload, dict):
        logger.warning(
            "Skipping deliver job %s with non-object payload",
            job.get("id"),
        )
        return False
    user_id = payload.get("user_id")
    alert_id = payload.get("alert_id")
    event_raw = payload.get("event")
    # Malformed internal/poison rows must drain (complete) like evaluate soft-skips —
    # KeyError here previously retried up to max_attempts and churned the queue.
    if not user_id or not alert_id or not isinstance(event_raw, dict):
        logger.warning(
            "Skipping deliver job %s with invalid payload keys/event",
            job.get("id"),
        )
        return False
    event = dict(event_raw)
    storage = UserAlertStorage(user_id)

    # A worker can die after the notification and trigger marker commit but before
    # completing the queue job. Stale-job recovery must not resend that event.
    # Missing/invalid event timestamps — or unparseable stored markers — must still
    # skip when a prior successful attempt left a trigger row.
    #
    # Check on every attempt, not only attempts > 1: a late original worker may
    # still hold an in-memory attempts==1 claim after another worker recovered
    # and already delivered the same event.
    event_at = _event_timestamp(event)
    raw_triggered = get_raw_triggered(user_id, alert_id)
    if raw_triggered:
        last_triggered = storage.get_last_triggered(alert_id)
        skip = event_at is None or last_triggered is None
        if not skip and last_triggered is not None:
            skip = _as_utc(last_triggered) >= event_at
        if skip:
            logger.info(
                "Skipping already-delivered event for alert %s (job %s, attempt %s)",
                alert_id,
                job["id"],
                job["attempts"],
            )
            return False

    watch = get_watch(user_id, alert_id)
    if not watch:
        raise RuntimeError(f"Watch {alert_id!r} not found for user {user_id}")

    # Evaluate jobs claimed in the same batch both pass the pre-enqueue cooldown
    # check before either deliver records a trigger. Re-check here so overlapping
    # ticks cannot double-notify under a positive cooldown.
    if _within_cooldown(user_id, alert_id, watch["cooldown_minutes"]):
        logger.info(
            "Skipping deliver within cooldown for alert %s (job %s)",
            alert_id,
            job["id"],
        )
        return False

    # Claim the trigger row before send so concurrent workers that both passed
    # the reads above cannot double-notify. Failed sends roll the claim back
    # only when this event's timestamp is still the stored marker.
    claim_ts = event.get("timestamp")
    claimed, previous_trigger = try_claim_trigger(
        user_id,
        alert_id,
        claim_ts,
        cooldown_minutes=watch["cooldown_minutes"],
    )
    if not claimed:
        logger.info(
            "Skipping deliver; lost trigger claim for alert %s (job %s)",
            alert_id,
            job["id"],
        )
        return False

    alert = watch["alert"]
    defaults = watch["defaults"]
    engine = AlertEngine([alert], storage=storage, defaults=defaults)

    try:
        if not engine.deliver_event(alert, event):
            raise RuntimeError(f"Delivery failed for alert {alert_id!r}")
        return True
    except Exception:
        restore_trigger_claim(user_id, alert_id, previous_trigger, claimed_at=claim_ts)
        raise


def process_job_queue(
    worker_id: str | None = None,
    *,
    limit: int = 50,
    max_batches: int = 100,
) -> Dict[str, int]:
    init_database()
    wid = worker_id or new_worker_id()
    stats = {"evaluated": 0, "delivered": 0, "failed": 0}

    for batch in range(max_batches):
        eval_jobs = claim_jobs([JOB_EVALUATE_SYMBOL], wid, limit=limit)
        for job in eval_jobs:
            try:
                _process_evaluate_symbol(job)
                if complete_job(job["id"], worker_id=wid):
                    stats["evaluated"] += 1
            except Exception as exc:
                logger.exception("evaluate_symbol job %s failed", job["id"])
                if fail_job(job["id"], str(exc), worker_id=wid):
                    stats["failed"] += 1

        deliver_jobs = claim_jobs([JOB_DELIVER], wid, limit=limit)
        for job in deliver_jobs:
            try:
                delivered = _process_deliver(job)
                if complete_job(job["id"], worker_id=wid) and delivered:
                    stats["delivered"] += 1
            except Exception as exc:
                logger.exception("deliver job %s failed", job["id"])
                if fail_job(job["id"], str(exc), worker_id=wid):
                    stats["failed"] += 1

        if not eval_jobs and not deliver_jobs:
            break
    else:
        logger.warning("Stopped processing alert jobs after %s batches", max_batches)

    return stats

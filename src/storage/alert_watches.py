"""Normalized alert watches synced from Helmtower config (multi-user mode)."""

from __future__ import annotations

import json
import math
import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

import src.alerts.alert_rules as alert_rules
import src.storage.database as database
from src.utils.tickers import normalize_ticker

MAX_DELIVERY_LOG = 100
# Cap alerts per config so one tenant cannot fan out unbounded watch rows /
# SQLite sync payloads on every Settings save or evaluate tick.
MAX_ALERTS_PER_CONFIG = 100
# Cap cooldown so timedelta(minutes=…) cannot OverflowError and abort a
# per-symbol watch loop (one tenant's poison value skipping sibling tenants).
MAX_COOLDOWN_MINUTES = 60 * 24 * 365  # one year


class InvalidAlertWatchConfig(ValueError):
    """Raised when a user alert config cannot be normalized into watch rows."""


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _parseable_trigger_timestamp(raw: Optional[str]) -> Optional[str]:
    """Return a storeable ISO timestamp, or None when missing/unparseable.

    Corrupt event timestamps must not be persisted: unparseable markers make
    ``get_last_triggered`` fail open (no cooldown) and break stale-delivery
    comparisons that rely on parseable trigger state.
    """
    if raw is None:
        return None
    text = str(raw).strip()
    if not text:
        return None
    try:
        datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    return text


def _parseable_iso_timestamp(raw: Optional[str]) -> Optional[str]:
    """Return a storeable ISO timestamp, or None when missing/unparseable.

    Delivery log rows are ordered by timestamp text. Corrupt values like
    ``"zzzz"`` can sort as newest forever and distort prune / status UI.
    """
    if raw is None:
        return None
    text = str(raw).strip()
    if not text:
        return None
    try:
        datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    return text


def _coerce_threshold(raw_value: Any, alert_id: str, *, label: str = "price threshold") -> float:
    # Missing/null thresholds previously persisted as SQL NULL watches that can
    # never evaluate usefully — reject at save so Infinity→JSON-null clients
    # and incomplete Settings payloads fail closed.
    if raw_value is None:
        raise InvalidAlertWatchConfig(f"Alert '{alert_id}' has an invalid {label} value.")
    try:
        threshold = float(raw_value)
    except (TypeError, ValueError) as exc:
        raise InvalidAlertWatchConfig(f"Alert '{alert_id}' has an invalid {label} value.") from exc
    if not math.isfinite(threshold):
        raise InvalidAlertWatchConfig(f"Alert '{alert_id}' has an invalid {label} value.")
    return threshold


def _coerce_rsi_period(raw_value: Any, alert_id: str) -> int:
    if raw_value is None:
        return alert_rules.DEFAULT_RSI_PERIOD
    try:
        period = int(raw_value)
    except (TypeError, ValueError) as exc:
        raise InvalidAlertWatchConfig(f"Alert '{alert_id}' has an invalid RSI period.") from exc
    if period < alert_rules.MIN_RSI_PERIOD or period > alert_rules.MAX_RSI_PERIOD:
        raise InvalidAlertWatchConfig(f"Alert '{alert_id}' has an invalid RSI period.")
    return period


def _validate_compound_condition(condition: Dict[str, Any], alert_id: str) -> None:
    op = str(condition.get("op") or "").strip().lower()
    if op not in alert_rules.COMPOUND_OPS:
        raise InvalidAlertWatchConfig(
            f"Alert '{alert_id}' compound rule must use op 'and' or 'or'."
        )
    leaves = condition.get("conditions")
    if not isinstance(leaves, list) or not leaves:
        raise InvalidAlertWatchConfig(f"Alert '{alert_id}' compound rule must include conditions.")
    if len(leaves) > alert_rules.MAX_COMPOUND_LEAVES:
        raise InvalidAlertWatchConfig(
            f"Alert '{alert_id}' compound rule exceeds "
            f"{alert_rules.MAX_COMPOUND_LEAVES} conditions."
        )
    for leaf in leaves:
        if not isinstance(leaf, dict):
            raise InvalidAlertWatchConfig(
                f"Alert '{alert_id}' compound rule has an invalid condition."
            )
        leaf_type = leaf.get("type")
        if leaf_type == "compound":
            raise InvalidAlertWatchConfig(
                f"Alert '{alert_id}' does not support nested compound rules."
            )
        if leaf_type not in alert_rules.LEAF_CONDITION_TYPES:
            raise InvalidAlertWatchConfig(
                f"Alert '{alert_id}' compound rule has an unsupported condition type."
            )
        if leaf_type == "price_threshold":
            if not normalize_ticker(leaf.get("symbol")):
                raise InvalidAlertWatchConfig(f"Alert '{alert_id}' must have a valid symbol.")
            raw_operator = leaf.get("operator")
            if raw_operator is None or not str(raw_operator).strip():
                raise InvalidAlertWatchConfig(f"Alert '{alert_id}' must have an operator.")
            _coerce_threshold(leaf.get("value"), alert_id)
        elif leaf_type == "rsi_threshold":
            if not normalize_ticker(leaf.get("symbol")):
                raise InvalidAlertWatchConfig(f"Alert '{alert_id}' must have a valid symbol.")
            raw_operator = leaf.get("operator")
            if raw_operator is None or not str(raw_operator).strip():
                raise InvalidAlertWatchConfig(f"Alert '{alert_id}' must have an operator.")
            _coerce_threshold(leaf.get("value"), alert_id, label="RSI threshold")
            _coerce_rsi_period(leaf.get("period"), alert_id)


def _coerce_cooldown(raw_value: Any, alert_id: str) -> int:
    # Inf/NaN raise OverflowError via int(float("inf")) or fail isfinite —
    # must become InvalidAlertWatchConfig so backfill/save don't 500.
    try:
        as_float = float(raw_value or 0)
    except (TypeError, ValueError) as exc:
        raise InvalidAlertWatchConfig(
            f"Alert '{alert_id}' has an invalid cooldown_minutes value."
        ) from exc
    if not math.isfinite(as_float):
        raise InvalidAlertWatchConfig(f"Alert '{alert_id}' has an invalid cooldown_minutes value.")
    if as_float < 0:
        # Negative cooldown is treated as "no cooldown" by evaluators; reject
        # at save so Settings cannot silently disable rate limiting.
        raise InvalidAlertWatchConfig(f"Alert '{alert_id}' has an invalid cooldown_minutes value.")
    if as_float > MAX_COOLDOWN_MINUTES:
        # Huge finite values (e.g. 1e15) pass isfinite but OverflowError
        # timedelta at evaluate time — reject at save.
        raise InvalidAlertWatchConfig(f"Alert '{alert_id}' has an invalid cooldown_minutes value.")
    try:
        return int(as_float)
    except (TypeError, ValueError, OverflowError) as exc:
        raise InvalidAlertWatchConfig(
            f"Alert '{alert_id}' has an invalid cooldown_minutes value."
        ) from exc


def _rows_from_config(user_id: str, config: Dict[str, Any], updated_at: str) -> List[tuple]:
    if not isinstance(config, dict):
        raise InvalidAlertWatchConfig("Alerts config must be an object.")
    raw_defaults = config.get("defaults")
    defaults = raw_defaults if isinstance(raw_defaults, dict) else {}
    ensure_alerts_within_limit(config)
    alerts = config.get("alerts") or []
    rows: List[tuple] = []
    seen_ids: set[str] = set()

    for alert in alerts:
        if not isinstance(alert, dict):
            continue
        alert_id = str(alert.get("id") or "").strip()
        if not alert_id:
            continue
        # Primary key is (user_id, alert_id); duplicates would IntegrityError → 500.
        if alert_id in seen_ids:
            raise InvalidAlertWatchConfig(f"Duplicate alert id '{alert_id}'.")
        seen_ids.add(alert_id)
        condition = alert.get("condition") or {}
        if not isinstance(condition, dict):
            condition = {}
        condition_type = str(condition.get("type") or "unknown")
        symbol = None
        operator = None
        threshold = None
        if condition_type == "price_threshold":
            # Strip whitespace / reject None-NaN sentinels so watch index keys match quotes.
            symbol = normalize_ticker(condition.get("symbol"))
            if not symbol:
                raise InvalidAlertWatchConfig(f"Alert '{alert_id}' must have a valid symbol.")
            raw_operator = condition.get("operator")
            if raw_operator is None or not str(raw_operator).strip():
                # Missing/blank operators never match at eval; reject at save so
                # Settings cannot persist zombie enabled rules.
                raise InvalidAlertWatchConfig(f"Alert '{alert_id}' must have an operator.")
            operator = str(raw_operator).strip()
            threshold = _coerce_threshold(condition.get("value"), alert_id)
        elif condition_type == "rsi_threshold":
            symbol = normalize_ticker(condition.get("symbol"))
            if not symbol:
                raise InvalidAlertWatchConfig(f"Alert '{alert_id}' must have a valid symbol.")
            raw_operator = condition.get("operator")
            if raw_operator is None or not str(raw_operator).strip():
                raise InvalidAlertWatchConfig(f"Alert '{alert_id}' must have an operator.")
            operator = str(raw_operator).strip()
            threshold = _coerce_threshold(condition.get("value"), alert_id, label="RSI threshold")
            _coerce_rsi_period(condition.get("period"), alert_id)
        elif condition_type == "compound":
            _validate_compound_condition(condition, alert_id)
            # Index single-symbol compounds on the shared ticker so the hosted
            # evaluate_symbol queue can fire them; multi-symbol compounds stay
            # off the queue (same as screening_match).

            symbol = alert_rules.primary_watch_symbol(condition)
        cooldown_minutes = _coerce_cooldown(alert.get("cooldown_minutes"), alert_id)
        rows.append(
            (
                user_id,
                alert_id,
                1 if alert.get("enabled", False) else 0,
                condition_type,
                symbol,
                operator,
                threshold,
                json.dumps(alert),
                json.dumps(defaults),
                cooldown_minutes,
                updated_at,
            )
        )
    return rows


def ensure_alerts_within_limit(config: Dict[str, Any]) -> None:
    """Reject oversized raw ``alerts`` arrays before polish/dedupe can shrink them.

    Hosted saves polish (and dedupe same price-threshold keys) before watch
    validation — without this gate a 10k duplicate payload would silently
    collapse to one rule and overwrite a prior config.
    """
    if not isinstance(config, dict):
        raise InvalidAlertWatchConfig("Alerts config must be an object.")
    alerts = config.get("alerts", [])
    if alerts is None:
        return
    if not isinstance(alerts, list):
        raise InvalidAlertWatchConfig("Alerts config must include an 'alerts' array.")
    if len(alerts) > MAX_ALERTS_PER_CONFIG:
        raise InvalidAlertWatchConfig(f"Config exceeds maximum of {MAX_ALERTS_PER_CONFIG} alerts.")


def validate_watches_config(user_id: str, config: Dict[str, Any]) -> None:
    """Validate that a config can be normalized without mutating watch rows."""
    ensure_alerts_within_limit(config)
    _rows_from_config(user_id, config, _utc_now())


def _replace_watches(
    conn: sqlite3.Connection,
    user_id: str,
    rows: List[tuple],
) -> None:
    conn.execute("DELETE FROM alert_watches WHERE user_id = ?", (user_id,))
    if rows:
        conn.executemany(
            """
            INSERT INTO alert_watches (
                user_id, alert_id, enabled, condition_type, symbol, operator,
                threshold, alert_json, defaults_json, cooldown_minutes, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            rows,
        )


def sync_watches_from_config(
    user_id: str,
    config: Dict[str, Any],
    *,
    connection: Optional[sqlite3.Connection] = None,
) -> None:
    """Replace user's watch rows from a Helmtower alerts config payload."""
    rows = _rows_from_config(user_id, config, _utc_now())

    if connection is not None:
        _replace_watches(connection, user_id, rows)
        return

    with database.get_connection() as conn:
        _replace_watches(conn, user_id, rows)


def list_enabled_symbols() -> List[str]:
    with database.get_connection() as conn:
        rows = conn.execute("""
            SELECT DISTINCT symbol FROM alert_watches
            WHERE enabled = 1 AND symbol IS NOT NULL AND symbol != ''
            ORDER BY symbol
            """).fetchall()
    return [key for key in (normalize_ticker(row["symbol"]) for row in rows) if key]


def list_watches_for_symbol(symbol: str) -> List[Dict[str, Any]]:
    normalized = normalize_ticker(symbol)
    if not normalized:
        return []
    with database.get_connection() as conn:
        rows = conn.execute(
            """
            SELECT user_id, alert_id, alert_json, defaults_json, cooldown_minutes,
                   condition_type, operator, threshold
            FROM alert_watches
            WHERE enabled = 1 AND symbol = ?
            """,
            (normalized,),
        ).fetchall()
    watches: List[Dict[str, Any]] = []
    for row in rows:
        parsed = _parse_watch_payload(row["alert_json"], row["defaults_json"])
        if parsed is None:
            continue
        alert, defaults = parsed
        watches.append(
            {
                "user_id": row["user_id"],
                "alert_id": row["alert_id"],
                "alert": alert,
                "defaults": defaults,
                "cooldown_minutes": _safe_cooldown_minutes(row["cooldown_minutes"]),
                "condition_type": row["condition_type"],
                "operator": row["operator"],
                "threshold": row["threshold"],
            }
        )
    return watches


def _parse_watch_payload(
    alert_json: Any, defaults_json: Any
) -> Optional[tuple[Dict[str, Any], Dict[str, Any]]]:
    """Parse stored watch JSON; skip poison non-object alert payloads."""
    try:
        alert = json.loads(alert_json)
        defaults = json.loads(defaults_json)
    except (TypeError, json.JSONDecodeError):
        return None
    if not isinstance(alert, dict):
        return None
    if not isinstance(defaults, dict):
        defaults = {}
    return alert, defaults


def _safe_cooldown_minutes(raw_value: Any) -> int:
    try:
        as_float = float(raw_value or 0)
    except (TypeError, ValueError):
        return 0
    if not math.isfinite(as_float) or as_float < 0:
        return 0
    try:
        minutes = int(as_float)
    except (TypeError, ValueError, OverflowError):
        return 0
    if minutes > MAX_COOLDOWN_MINUTES:
        return MAX_COOLDOWN_MINUTES
    return minutes


def get_watch(user_id: str, alert_id: str) -> Optional[Dict[str, Any]]:
    with database.get_connection() as conn:
        row = conn.execute(
            """
            SELECT alert_json, defaults_json, cooldown_minutes
            FROM alert_watches WHERE user_id = ? AND alert_id = ?
            """,
            (user_id, alert_id),
        ).fetchone()
    if not row:
        return None
    parsed = _parse_watch_payload(row["alert_json"], row["defaults_json"])
    if parsed is None:
        return None
    alert, defaults = parsed
    return {
        "alert": alert,
        "defaults": defaults,
        "cooldown_minutes": _safe_cooldown_minutes(row["cooldown_minutes"]),
    }


def get_last_triggered(user_id: str, alert_id: str) -> Optional[str]:
    with database.get_connection() as conn:
        row = conn.execute(
            "SELECT last_triggered_at FROM alert_trigger_state WHERE user_id = ? AND alert_id = ?",
            (user_id, alert_id),
        ).fetchone()
    if not row:
        return None
    return row["last_triggered_at"]


def record_trigger(user_id: str, alert_id: str, timestamp: Optional[str] = None) -> None:
    ts = _parseable_trigger_timestamp(timestamp) or _utc_now()
    with database.get_connection() as conn:
        conn.execute(
            """
            INSERT INTO alert_trigger_state (user_id, alert_id, last_triggered_at)
            VALUES (?, ?, ?)
            ON CONFLICT(user_id, alert_id) DO UPDATE SET
                last_triggered_at = excluded.last_triggered_at
            """,
            (user_id, alert_id, ts),
        )


def _as_utc_datetime(raw: Any) -> Optional[datetime]:
    if raw is None:
        return None
    text = str(raw).strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def try_claim_trigger(
    user_id: str,
    alert_id: str,
    timestamp: Optional[str] = None,
    *,
    cooldown_minutes: int = 0,
) -> tuple[bool, Optional[str]]:
    """Atomically claim a delivery slot before sending notifications.

    Returns ``(claimed, previous_timestamp)``. Callers that fail after a
    successful claim must ``restore_trigger_claim`` with the same
    ``claimed_at`` timestamp so retries stay eligible without clobbering a
    newer successful claim.

    Under a positive cooldown, concurrent workers that both passed a stale
    read of trigger state cannot both send — only the IMMEDIATE winner
    proceeds. Same/newer event timestamps still lose the claim.
    """

    claim_ts = timestamp or _utc_now()
    event_at = _as_utc_datetime(timestamp) if timestamp else None

    with database.get_connection() as conn:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT last_triggered_at FROM alert_trigger_state WHERE user_id = ? AND alert_id = ?",
            (user_id, alert_id),
        ).fetchone()
        previous = str(row["last_triggered_at"]) if row else None

        if previous is not None:
            # Mirror job_processor: missing/invalid event stamps still skip when
            # a prior successful delivery left a trigger row.
            if event_at is None:
                return False, previous
            last_at = _as_utc_datetime(previous)
            if last_at is None or last_at >= event_at:
                return False, previous
            if cooldown_minutes > 0:
                now = datetime.now(timezone.utc)
                if now - last_at < timedelta(minutes=int(cooldown_minutes)):
                    return False, previous

        conn.execute(
            """
            INSERT INTO alert_trigger_state (user_id, alert_id, last_triggered_at)
            VALUES (?, ?, ?)
            ON CONFLICT(user_id, alert_id) DO UPDATE SET
                last_triggered_at = excluded.last_triggered_at
            """,
            (user_id, alert_id, claim_ts),
        )
        return True, previous


def restore_trigger_claim(
    user_id: str,
    alert_id: str,
    previous: Optional[str],
    *,
    claimed_at: Optional[str] = None,
) -> None:
    """Roll back a ``try_claim_trigger`` after a failed notification send.

    Only revert when ``last_triggered_at`` is still this claim. A newer
    overlapping deliver can commit after we claimed and before send fails;
    blindly deleting/updating the row would erase that success and reopen
    duplicate webhook/email delivery. Missing/unparseable ``claimed_at``
    must no-op rather than clobber.
    """
    claim_ts = _parseable_trigger_timestamp(claimed_at)
    if claim_ts is None:
        return
    with database.get_connection() as conn:
        conn.execute("BEGIN IMMEDIATE")
        if previous is None:
            conn.execute(
                """
                DELETE FROM alert_trigger_state
                WHERE user_id = ? AND alert_id = ? AND last_triggered_at = ?
                """,
                (user_id, alert_id, claim_ts),
            )
        else:
            conn.execute(
                """
                UPDATE alert_trigger_state
                SET last_triggered_at = ?
                WHERE user_id = ? AND alert_id = ? AND last_triggered_at = ?
                """,
                (previous, user_id, alert_id, claim_ts),
            )


def record_delivery(
    user_id: str,
    alert_id: str,
    channel: str,
    *,
    success: bool,
    test: bool = False,
    error: Optional[str] = None,
    timestamp: Optional[str] = None,
) -> None:
    ts = _parseable_iso_timestamp(timestamp) or _utc_now()
    with database.get_connection() as conn:
        conn.execute(
            """
            INSERT INTO alert_delivery_log
                (user_id, alert_id, channel, success, test, error, timestamp)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (user_id, alert_id, channel, 1 if success else 0, 1 if test else 0, error, ts),
        )
        count_row = conn.execute(
            "SELECT COUNT(*) AS n FROM alert_delivery_log WHERE user_id = ?",
            (user_id,),
        ).fetchone()
        excess = int(count_row["n"]) - MAX_DELIVERY_LOG
        if excess > 0:
            conn.execute(
                """
                DELETE FROM alert_delivery_log
                WHERE id IN (
                    SELECT id FROM alert_delivery_log
                    WHERE user_id = ?
                    ORDER BY timestamp ASC, id ASC
                    LIMIT ?
                )
                """,
                (user_id, excess),
            )


def latest_trigger_timestamp_for_user(user_id: str) -> Optional[str]:
    with database.get_connection() as conn:
        row = conn.execute(
            """
            SELECT MAX(last_triggered_at) AS ts
            FROM alert_trigger_state
            WHERE user_id = ?
            """,
            (user_id,),
        ).fetchone()
    if not row or not row["ts"]:
        return None
    return str(row["ts"])


def latest_deliveries_for_user(user_id: str) -> List[Dict[str, Any]]:
    with database.get_connection() as conn:
        rows = conn.execute(
            """
            SELECT alert_id, channel, success, test, error, timestamp
            FROM alert_delivery_log
            WHERE user_id = ?
            ORDER BY timestamp DESC, id DESC
            LIMIT ?
            """,
            (user_id, MAX_DELIVERY_LOG),
        ).fetchall()
    latest: Dict[str, Dict[str, Any]] = {}
    for row in rows:
        channel = row["channel"]
        if channel in latest:
            continue
        latest[channel] = {
            "alert_id": row["alert_id"],
            "channel": channel,
            "success": bool(row["success"]),
            "test": bool(row["test"]),
            "timestamp": row["timestamp"],
            "error": row["error"],
        }
    return [latest[key] for key in sorted(latest.keys())]

"""Durable, database-wide coordination and evidence for scheduled collection."""

from __future__ import annotations

import math
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

from src.analysis.market_calendar import get_market_calendar, last_completed_session
from src.core.market_refresh import MarketRefreshSettings
from src.storage.database import get_connection


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def expected_session(now: datetime, settings: MarketRefreshSettings) -> str:
    """Allow the provider a post-close grace period, including on early-close days."""
    return last_completed_session(now - timedelta(seconds=settings.close_delay_seconds)).isoformat()


def load_state() -> dict[str, Any] | None:
    with get_connection() as conn:
        row = conn.execute("SELECT * FROM market_refresh_state WHERE id = 1").fetchone()
    return dict(row) if row else None


def claim_run(
    session: str, now: datetime, settings: MarketRefreshSettings, *, retry_failed: bool = False
) -> tuple[str | None, str]:
    """Serialize scheduled collectors without holding a write lock during network I/O."""
    if now.utcoffset() is None:
        raise ValueError("timestamp must include a timezone")
    now = now.astimezone(timezone.utc)
    stamp = now.isoformat()
    with get_connection() as conn:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(
            "INSERT INTO market_refresh_state (id, status, attempts) VALUES (1, 'idle', 0) "
            "ON CONFLICT(id) DO NOTHING"
        )
        row = conn.execute("SELECT * FROM market_refresh_state WHERE id = 1").fetchone()
        if row["lease_until"] and row["lease_until"] > stamp:
            return None, "running"
        if row["status"] == "running":
            conn.execute(
                "UPDATE market_refresh_state SET status = 'error', lease_until = NULL, "
                "finished_at = ?, error_code = 'lease_expired' WHERE id = 1",
                (stamp,),
            )
        if row["last_success_session"] and row["last_success_session"] >= session:
            return None, "already_collected"
        same_session = row["target_session"] == session
        attempts = int(row["attempts"]) if same_session else 0
        if same_session and not retry_failed:
            if attempts >= settings.max_attempts:
                return None, "attempts_exhausted"
            if row["retry_after"] and row["retry_after"] > stamp:
                return None, "retry_pending"
        if retry_failed:
            attempts = 0
        owner = uuid.uuid4().hex
        # Both parent and child enforce the runtime limit. The extra grace covers
        # process startup/termination before an abandoned lease can be reclaimed.
        lease = (now + timedelta(seconds=settings.timeout_seconds + 120)).isoformat()
        conn.execute(
            """UPDATE market_refresh_state SET owner = ?, status = 'running',
               target_session = ?, started_at = ?, finished_at = NULL,
               lease_until = ?, retry_after = NULL, attempts = ?, error_code = NULL
               WHERE id = 1""",
            (owner, session, stamp, lease, attempts + 1),
        )
    return owner, "claimed"


def finish_run(
    owner: str,
    now: datetime,
    settings: MarketRefreshSettings,
    *,
    error_code: str | None = None,
    quote_count: int = 0,
) -> bool:
    """Fence completion so an expired owner cannot overwrite a newer run."""
    if now.utcoffset() is None:
        raise ValueError("timestamp must include a timezone")
    now = now.astimezone(timezone.utc)
    stamp = now.isoformat()
    with get_connection() as conn:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT * FROM market_refresh_state WHERE id = 1 AND owner = ? "
            "AND status = 'running' AND lease_until > ?",
            (owner, stamp),
        ).fetchone()
        if row is None:
            return False
        if error_code is None:
            conn.execute(
                """UPDATE market_refresh_state SET status = 'success', finished_at = ?,
                   lease_until = NULL, retry_after = NULL, error_code = NULL,
                   last_success_session = target_session, last_success_at = ?,
                   success_started_at = started_at, quote_count = ? WHERE id = 1""",
                (stamp, stamp, quote_count),
            )
        else:
            retry = (now + timedelta(seconds=settings.retry_seconds)).isoformat()
            conn.execute(
                """UPDATE market_refresh_state SET status = 'error', finished_at = ?,
                   lease_until = NULL, retry_after = ?, error_code = ? WHERE id = 1""",
                (stamp, retry, error_code),
            )
    return True


def verified_quote_count(session: str, since: str, now: datetime) -> int:
    """Require fresh provider timestamps AND newly persisted projections/summary.

    A successful process exit or a file bearing today's date is insufficient.
    Quote timestamps identify the market session; bars must also be stored under
    that session date so dashboard weekday lookups can find them.
    """
    with get_connection() as conn:
        rows = conn.execute(
            """SELECT b.symbol, b.close, b.quote_timestamp FROM market_bars b
               JOIN projections p ON p.run_date = b.trade_date AND p.symbol = b.symbol
               JOIN daily_summaries s ON s.summary_date = b.trade_date
               WHERE b.trade_date = ? AND b.written_at >= ?
                 AND p.written_at >= ? AND s.written_at >= ?""",
            (session, since, since, since),
        ).fetchall()
    calendar = get_market_calendar()
    zone = calendar.tz
    earliest_quote = calendar.session_close(session).to_pydatetime() - timedelta(minutes=15)
    symbols = set()
    for row in rows:
        try:
            quote = datetime.fromisoformat(str(row["quote_timestamp"]).replace("Z", "+00:00"))
            price = float(row["close"])
            if quote.utcoffset() is None or not math.isfinite(price) or price <= 0:
                continue
            if quote < earliest_quote or quote > now + timedelta(minutes=5):
                continue
            if quote.astimezone(zone).date().isoformat() == session:
                symbols.add(row["symbol"])
        except (ValueError, TypeError, OverflowError):
            continue
    return len(symbols)


def market_data_health(now: datetime, settings: MarketRefreshSettings) -> dict[str, Any]:
    session = expected_session(now, settings)
    state = load_state()
    if not state:
        return {"ok": False, "reason": "never_collected", "expected_session": session}
    # Give an in-flight collection its bounded runtime before paging for staleness.
    required = last_completed_session(
        now
        - timedelta(
            seconds=settings.close_delay_seconds
            + settings.poll_seconds
            + settings.timeout_seconds
            + 120
        )
    ).isoformat()
    count = 0
    if state["last_success_session"] and state["success_started_at"]:
        count = verified_quote_count(
            state["last_success_session"], state["success_started_at"], now
        )
    ok = bool(
        state["last_success_session"]
        and state["last_success_session"] >= required
        and count >= settings.min_symbols
    )
    return {
        "ok": ok,
        "reason": (
            ("fresh" if state["last_success_session"] == session else "update_pending")
            if ok
            else "missing_session_data"
        ),
        "expected_session": session,
        "required_session": required,
        "data_session": state["last_success_session"],
        "last_success_at": state["last_success_at"],
        "quote_count": count,
        "required_symbols": settings.min_symbols,
        "last_status": state["status"],
        "attempts": state["attempts"],
        "error_code": state["error_code"],
        "retry_after": state["retry_after"],
    }

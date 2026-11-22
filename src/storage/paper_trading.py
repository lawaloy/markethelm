"""Account-scoped paper-trading risk decisions and their audit records.

This module has no order submission path. Only trusted backend code should pass
the authenticated user ID, market-open state, and quote provenance to it.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

from src.trading.risk import (
    PortfolioSnapshot,
    RiskDecision,
    RiskLimits,
    TradeProposal,
    evaluate_risk,
)

from .database import get_connection


class PaperAccountNotFound(ValueError):
    """The authenticated account no longer exists."""


class DuplicateProposal(ValueError):
    """A proposal ID was already evaluated for this user."""


def _timestamp(raw: Any) -> datetime | None:
    if raw is None:
        return None
    try:
        value = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except ValueError:
        return None
    return value if value.tzinfo is not None and value.utcoffset() is not None else None


def _account_row(conn: Any, user_id: str) -> Any:
    if not isinstance(user_id, str) or not user_id.strip():
        raise PaperAccountNotFound("A valid account is required")
    user = conn.execute("SELECT id FROM users WHERE id = ?", (user_id,)).fetchone()
    if user is None:
        raise PaperAccountNotFound("Account not found")
    conn.execute(
        "INSERT INTO paper_accounts (user_id) VALUES (?) ON CONFLICT (user_id) DO NOTHING",
        (user_id,),
    )
    return conn.execute("SELECT * FROM paper_accounts WHERE user_id = ?", (user_id,)).fetchone()


def _snapshot(conn: Any, user_id: str, account: Any, market_open: bool) -> PortfolioSnapshot:
    rows = conn.execute(
        "SELECT symbol, quantity FROM paper_positions WHERE user_id = ?",
        (user_id,),
    ).fetchall()
    return PortfolioSnapshot(
        as_of=_timestamp(account["portfolio_verified_at"]),
        market_open=market_open,
        cash=account["cash"],
        reserved_cash=account["reserved_cash"],
        gross_exposure=account["gross_exposure"],
        positions={row["symbol"]: row["quantity"] for row in rows},
        realized_loss_today=account["realized_loss_today"],
    )


def _limits(account: Any) -> RiskLimits:
    return RiskLimits(
        max_order_notional=account["max_order_notional"],
        max_position_notional=account["max_position_notional"],
        max_total_exposure=account["max_total_exposure"],
        max_daily_loss=account["max_daily_loss"],
        max_data_age_seconds=account["max_data_age_seconds"],
        trading_enabled=account["trading_enabled"] == 1,
        kill_switch=account["kill_switch"] != 0,
    )


def _audit_input(
    proposal: TradeProposal,
    snapshot: PortfolioSnapshot,
    limits: RiskLimits,
) -> str:
    return json.dumps(
        {
            "proposal": {
                "proposal_id": proposal.proposal_id,
                "symbol": proposal.symbol,
                "side": proposal.side,
                "quantity": str(proposal.quantity),
                "reference_price": str(proposal.reference_price),
                "quote_at": str(proposal.quote_at),
                "strategy_version": proposal.strategy_version,
            },
            "portfolio": {
                "as_of": str(snapshot.as_of),
                "market_open": snapshot.market_open,
                "cash": str(snapshot.cash),
                "reserved_cash": str(snapshot.reserved_cash),
                "gross_exposure": str(snapshot.gross_exposure),
                "realized_loss_today": str(snapshot.realized_loss_today),
                "positions": {key: str(value) for key, value in snapshot.positions.items()},
                "pending_symbols": sorted(snapshot.pending_symbols),
                "pending_proposal_ids": sorted(snapshot.pending_proposal_ids),
            },
            "limits": {
                "max_order_notional": str(limits.max_order_notional),
                "max_position_notional": str(limits.max_position_notional),
                "max_total_exposure": str(limits.max_total_exposure),
                "max_daily_loss": str(limits.max_daily_loss),
                "max_data_age_seconds": limits.max_data_age_seconds,
                "trading_enabled": limits.trading_enabled,
                "kill_switch": limits.kill_switch,
            },
        },
        sort_keys=True,
        separators=(",", ":"),
    )


def record_paper_risk_decision(
    user_id: str,
    proposal: TradeProposal,
    *,
    market_open: bool,
    now: datetime | None = None,
) -> RiskDecision:
    """Evaluate against one user's stored state and append a decision atomically.

    A positive result is not an order authorization. The future order service
    must recheck risk while reserving funds and creating an order in one transaction.
    """
    evaluated_at = now or datetime.now(timezone.utc)
    if evaluated_at.tzinfo is None or evaluated_at.utcoffset() is None:
        raise ValueError("now must be timezone-aware")
    if not isinstance(proposal.proposal_id, str) or not proposal.proposal_id.strip():
        raise ValueError("proposal_id is required for an audit record")
    with get_connection() as conn:
        conn.execute("BEGIN IMMEDIATE")
        account = _account_row(conn, user_id)
        prior = conn.execute(
            "SELECT 1 FROM paper_risk_decisions WHERE user_id = ? AND proposal_id = ?",
            (user_id, proposal.proposal_id),
        ).fetchone()
        if prior:
            raise DuplicateProposal("Proposal already evaluated for this account")
        snapshot = _snapshot(conn, user_id, account, market_open)
        limits = _limits(account)
        decision = evaluate_risk(proposal, snapshot, limits, now=evaluated_at)
        conn.execute(
            """INSERT INTO paper_risk_decisions (
                user_id, proposal_id, input_json, approved, reasons_json,
                notional, evaluated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (
                user_id,
                proposal.proposal_id,
                _audit_input(proposal, snapshot, limits),
                int(decision.approved),
                json.dumps(decision.reasons),
                str(decision.notional) if decision.notional is not None else None,
                evaluated_at.isoformat(),
            ),
        )
    return decision


def list_paper_risk_decisions(user_id: str, *, limit: int = 50) -> list[dict[str, Any]]:
    """Return only this user's recent decisions for future account UI/API use."""
    if not isinstance(user_id, str) or not user_id.strip():
        raise PaperAccountNotFound("A valid account is required")
    if type(limit) is not int or not 1 <= limit <= 100:
        raise ValueError("limit must be between 1 and 100")
    with get_connection() as conn:
        rows = conn.execute(
            """SELECT proposal_id, input_json, approved, reasons_json,
                      notional, evaluated_at
               FROM paper_risk_decisions WHERE user_id = ?
               ORDER BY evaluated_at DESC, proposal_id DESC LIMIT ?""",
            (user_id, limit),
        ).fetchall()
    return [
        {
            "proposal_id": row["proposal_id"],
            "input": json.loads(row["input_json"]),
            "approved": bool(row["approved"]),
            "reasons": json.loads(row["reasons_json"]),
            "notional": Decimal(row["notional"]) if row["notional"] is not None else None,
            "evaluated_at": row["evaluated_at"],
        }
        for row in rows
    ]

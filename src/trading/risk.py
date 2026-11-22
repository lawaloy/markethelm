"""Deterministic, fail-closed checks for proposed paper trades.

The caller supplies a fresh portfolio snapshot and explicit account limits.
Decisions are proposals for an order service, not permission to bypass its own
atomic buying-power reservation and duplicate-order checks.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation

from src.utils.tickers import normalize_ticker


@dataclass(frozen=True)
class TradeProposal:
    proposal_id: str
    symbol: str
    side: str
    quantity: Decimal
    reference_price: Decimal
    quote_at: datetime
    strategy_version: str


@dataclass(frozen=True)
class PortfolioSnapshot:
    as_of: datetime
    market_open: bool
    cash: Decimal
    reserved_cash: Decimal
    gross_exposure: Decimal
    positions: Mapping[str, Decimal] = field(default_factory=dict)
    pending_symbols: frozenset[str] = frozenset()
    pending_proposal_ids: frozenset[str] = frozenset()
    realized_loss_today: Decimal = Decimal("0")


@dataclass(frozen=True)
class RiskLimits:
    max_order_notional: Decimal
    max_position_notional: Decimal
    max_total_exposure: Decimal
    max_daily_loss: Decimal
    max_data_age_seconds: int
    trading_enabled: bool = False
    kill_switch: bool = True


@dataclass(frozen=True)
class RiskDecision:
    approved: bool
    reasons: tuple[str, ...]
    notional: Decimal | None
    evaluated_at: datetime


def _amount(value: object) -> Decimal | None:
    """Reject non-finite or non-numeric values before money calculations."""
    if isinstance(value, bool):
        return None
    try:
        amount = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None
    return amount if amount.is_finite() else None


def _aware(value: object) -> bool:
    return (
        isinstance(value, datetime) and value.tzinfo is not None and value.utcoffset() is not None
    )


def evaluate_risk(
    proposal: TradeProposal,
    snapshot: PortfolioSnapshot,
    limits: RiskLimits,
    *,
    now: datetime | None = None,
) -> RiskDecision:
    """Return a reasoned decision; malformed or missing inputs never approve."""
    evaluated_at = now or datetime.now(timezone.utc)
    if not _aware(evaluated_at):
        raise ValueError("now must be timezone-aware")

    reasons: list[str] = []
    if limits.trading_enabled is not True:
        reasons.append("trading_disabled")
    if limits.kill_switch is not False:
        reasons.append("kill_switch_active")
    if snapshot.market_open is not True:
        reasons.append("market_closed")

    max_order = _amount(limits.max_order_notional)
    max_position = _amount(limits.max_position_notional)
    max_exposure = _amount(limits.max_total_exposure)
    max_loss = _amount(limits.max_daily_loss)
    if (
        any(
            value is None or value <= 0
            for value in (max_order, max_position, max_exposure, max_loss)
        )
        or type(limits.max_data_age_seconds) is not int
        or limits.max_data_age_seconds <= 0
    ):
        reasons.append("invalid_limits")

    symbol = normalize_ticker(proposal.symbol) if isinstance(proposal.symbol, str) else None
    side = (
        proposal.side
        if isinstance(proposal.side, str) and proposal.side in {"BUY", "SELL"}
        else None
    )
    quantity = _amount(proposal.quantity)
    price = _amount(proposal.reference_price)
    if (
        not isinstance(proposal.proposal_id, str)
        or not proposal.proposal_id.strip()
        or not isinstance(proposal.strategy_version, str)
        or not proposal.strategy_version.strip()
        or symbol is None
        or proposal.symbol != symbol
        or side is None
        or quantity is None
        or quantity <= 0
        or price is None
        or price <= 0
    ):
        reasons.append("invalid_proposal")

    cash = _amount(snapshot.cash)
    reserved = _amount(snapshot.reserved_cash)
    exposure = _amount(snapshot.gross_exposure)
    loss = _amount(snapshot.realized_loss_today)
    positions_valid = isinstance(snapshot.positions, Mapping) and all(
        isinstance(key, str)
        and normalize_ticker(key) == key
        and (value := _amount(quantity_held)) is not None
        and value >= 0
        for key, quantity_held in snapshot.positions.items()
    )
    pending_valid = (
        isinstance(snapshot.pending_symbols, frozenset)
        and all(
            isinstance(key, str) and normalize_ticker(key) == key
            for key in snapshot.pending_symbols
        )
        and isinstance(snapshot.pending_proposal_ids, frozenset)
        and all(isinstance(key, str) and key.strip() for key in snapshot.pending_proposal_ids)
    )
    holdings = _amount(snapshot.positions.get(symbol, 0)) if positions_valid and symbol else None
    if (
        not positions_valid
        or not pending_valid
        or cash is None
        or cash < 0
        or reserved is None
        or reserved < 0
        or exposure is None
        or exposure < 0
        or loss is None
        or loss < 0
        or holdings is None
        or holdings < 0
    ):
        reasons.append("invalid_portfolio")

    if _aware(proposal.quote_at) and _aware(snapshot.as_of):
        quote_age = (evaluated_at - proposal.quote_at).total_seconds()
        snapshot_age = (evaluated_at - snapshot.as_of).total_seconds()
        if (
            quote_age < 0
            or snapshot_age < 0
            or (
                "invalid_limits" not in reasons
                and (
                    quote_age > limits.max_data_age_seconds
                    or snapshot_age > limits.max_data_age_seconds
                )
            )
        ):
            reasons.append("stale_or_future_data")
    else:
        reasons.append("invalid_data_timestamp")

    if pending_valid and (
        proposal.proposal_id in snapshot.pending_proposal_ids
        or (symbol is not None and symbol in snapshot.pending_symbols)
    ):
        reasons.append("pending_order_conflict")

    if reasons and any(
        reason in reasons for reason in ("invalid_limits", "invalid_proposal", "invalid_portfolio")
    ):
        return RiskDecision(False, tuple(reasons), None, evaluated_at)

    assert quantity is not None and price is not None
    assert max_order is not None and max_position is not None and max_exposure is not None
    assert max_loss is not None and cash is not None and reserved is not None
    assert exposure is not None and loss is not None and holdings is not None
    notional = quantity * price
    if notional > max_order:
        reasons.append("order_limit_exceeded")
    if loss >= max_loss:
        reasons.append("daily_loss_limit_reached")
    if side == "BUY":
        if notional > cash - reserved:
            reasons.append("insufficient_buying_power")
        if (holdings + quantity) * price > max_position:
            reasons.append("position_limit_exceeded")
        if exposure + notional > max_exposure:
            reasons.append("exposure_limit_exceeded")
    elif quantity > holdings:
        reasons.append("insufficient_holdings")

    return RiskDecision(not reasons, tuple(reasons), notional, evaluated_at)

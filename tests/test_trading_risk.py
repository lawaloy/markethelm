"""Risk checks must reject unsafe paper-trade proposals before order creation."""

from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from src.trading.risk import PortfolioSnapshot, RiskLimits, TradeProposal, evaluate_risk

NOW = datetime(2026, 11, 20, 15, 0, tzinfo=timezone.utc)


@pytest.fixture
def proposal():
    return TradeProposal(
        proposal_id="p-1",
        symbol="AAPL",
        side="BUY",
        quantity=Decimal("2"),
        reference_price=Decimal("100.25"),
        quote_at=NOW - timedelta(seconds=10),
        strategy_version="strategy-v1",
    )


@pytest.fixture
def snapshot():
    return PortfolioSnapshot(
        as_of=NOW - timedelta(seconds=5),
        market_open=True,
        cash=Decimal("1000"),
        reserved_cash=Decimal("0"),
        gross_exposure=Decimal("300"),
        positions={"AAPL": Decimal("1")},
    )


@pytest.fixture
def limits():
    return RiskLimits(
        max_order_notional=Decimal("500"),
        max_position_notional=Decimal("500"),
        max_total_exposure=Decimal("1000"),
        max_daily_loss=Decimal("100"),
        max_data_age_seconds=30,
        trading_enabled=True,
        kill_switch=False,
    )


def test_approved_buy_has_exact_decimal_notional(proposal, snapshot, limits):
    decision = evaluate_risk(proposal, snapshot, limits, now=NOW)
    assert decision.approved
    assert decision.reasons == ()
    assert decision.notional == Decimal("200.50")
    assert decision.evaluated_at == NOW


@pytest.mark.parametrize(
    "limit_changes, reason",
    [
        ({"trading_enabled": False}, "trading_disabled"),
        ({"kill_switch": True}, "kill_switch_active"),
        ({"max_order_notional": Decimal("200")}, "order_limit_exceeded"),
        ({"max_position_notional": Decimal("300")}, "position_limit_exceeded"),
        ({"max_total_exposure": Decimal("500")}, "exposure_limit_exceeded"),
        ({"max_data_age_seconds": 0}, "invalid_limits"),
        ({"max_order_notional": Decimal("NaN")}, "invalid_limits"),
    ],
)
def test_limit_failures_reject(proposal, snapshot, limits, limit_changes, reason):
    decision = evaluate_risk(proposal, snapshot, replace(limits, **limit_changes), now=NOW)
    assert not decision.approved
    assert reason in decision.reasons


@pytest.mark.parametrize(
    "snapshot_changes, reason",
    [
        ({"cash": Decimal("200")}, "insufficient_buying_power"),
        ({"market_open": False}, "market_closed"),
        ({"reserved_cash": Decimal("900")}, "insufficient_buying_power"),
        ({"realized_loss_today": Decimal("100")}, "daily_loss_limit_reached"),
        ({"gross_exposure": Decimal("NaN")}, "invalid_portfolio"),
        ({"positions": {"aapl": Decimal("1")}}, "invalid_portfolio"),
        ({"positions": {"AAPL": Decimal("-1")}}, "invalid_portfolio"),
        ({"pending_symbols": None}, "invalid_portfolio"),
        ({"pending_symbols": frozenset({"AAPL"})}, "pending_order_conflict"),
        ({"pending_proposal_ids": frozenset({"p-1"})}, "pending_order_conflict"),
        ({"as_of": NOW - timedelta(minutes=1)}, "stale_or_future_data"),
        ({"as_of": NOW + timedelta(seconds=1)}, "stale_or_future_data"),
        ({"as_of": NOW.replace(tzinfo=None)}, "invalid_data_timestamp"),
    ],
)
def test_portfolio_and_freshness_failures_reject(
    proposal, snapshot, limits, snapshot_changes, reason
):
    decision = evaluate_risk(proposal, replace(snapshot, **snapshot_changes), limits, now=NOW)
    assert not decision.approved
    assert reason in decision.reasons


@pytest.mark.parametrize(
    "proposal_changes, reason",
    [
        ({"symbol": "../AAPL"}, "invalid_proposal"),
        ({"symbol": "aapl"}, "invalid_proposal"),
        ({"side": "SHORT"}, "invalid_proposal"),
        ({"quantity": Decimal("0")}, "invalid_proposal"),
        ({"reference_price": Decimal("Infinity")}, "invalid_proposal"),
        ({"quote_at": NOW - timedelta(minutes=1)}, "stale_or_future_data"),
        ({"quote_at": NOW + timedelta(seconds=1)}, "stale_or_future_data"),
        ({"quote_at": NOW.replace(tzinfo=None)}, "invalid_data_timestamp"),
    ],
)
def test_invalid_proposals_and_quotes_reject(proposal, snapshot, limits, proposal_changes, reason):
    decision = evaluate_risk(replace(proposal, **proposal_changes), snapshot, limits, now=NOW)
    assert not decision.approved
    assert reason in decision.reasons


def test_sell_rejects_oversell_and_accepts_available_holdings(proposal, snapshot, limits):
    sell = replace(proposal, side="SELL", quantity=Decimal("2"))
    assert "insufficient_holdings" in evaluate_risk(sell, snapshot, limits, now=NOW).reasons
    sell = replace(sell, quantity=Decimal("1"))
    decision = evaluate_risk(sell, snapshot, limits, now=NOW)
    assert decision.approved
    assert decision.notional == Decimal("100.25")


def test_naive_decision_clock_is_rejected(proposal, snapshot, limits):
    with pytest.raises(ValueError, match="timezone-aware"):
        evaluate_risk(proposal, snapshot, limits, now=NOW.replace(tzinfo=None))


def test_defaults_block_trading(proposal, snapshot, limits):
    defaults = replace(limits, trading_enabled=False, kill_switch=True)
    decision = evaluate_risk(proposal, snapshot, defaults, now=NOW)
    assert not decision.approved
    assert "trading_disabled" in decision.reasons
    assert "kill_switch_active" in decision.reasons


def test_exact_limit_is_permitted(proposal, snapshot, limits):
    exact = replace(
        limits,
        max_order_notional=Decimal("200.50"),
        max_position_notional=Decimal("300.75"),
        max_total_exposure=Decimal("500.50"),
    )
    assert evaluate_risk(proposal, snapshot, exact, now=NOW).approved

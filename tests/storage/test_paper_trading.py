"""Paper risk decisions are account-scoped, disabled by default, and auditable."""

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from src.storage.database import get_connection, init_database
from src.storage.paper_trading import (
    DuplicateProposal,
    PaperAccountNotFound,
    list_paper_risk_decisions,
    record_paper_risk_decision,
)
from src.storage.users import create_user
from src.trading.risk import TradeProposal

NOW = datetime(2026, 11, 20, 15, 0, tzinfo=timezone.utc)


@pytest.fixture
def accounts(tmp_path, monkeypatch):
    db = tmp_path / "paper-trading.db"
    monkeypatch.setenv("MARKETHELM_DATABASE_URL", f"sqlite:///{db.as_posix()}")
    init_database()
    first = create_user("paper-one@example.com", "password123")
    second = create_user("paper-two@example.com", "password123")
    return first["id"], second["id"]


@pytest.fixture
def proposal():
    return TradeProposal(
        proposal_id="signal-1",
        symbol="AAPL",
        side="BUY",
        quantity=Decimal("1"),
        reference_price=Decimal("100.25"),
        quote_at=NOW - timedelta(seconds=5),
        strategy_version="test-v1",
    )


def _enable_account(user_id: str, *, cash: str = "500", verified_at: datetime = NOW) -> None:
    with get_connection() as conn:
        conn.execute(
            """UPDATE paper_accounts
               SET cash = ?, portfolio_verified_at = ?, trading_enabled = 1,
                   kill_switch = 0, max_order_notional = '500'
               WHERE user_id = ?""",
            (cash, verified_at.isoformat(), user_id),
        )


def test_new_account_is_disabled_and_audits_rejection(accounts, proposal):
    first, _ = accounts
    decision = record_paper_risk_decision(first, proposal, market_open=True, now=NOW)
    assert not decision.approved
    assert "trading_disabled" in decision.reasons
    assert "kill_switch_active" in decision.reasons
    assert "invalid_data_timestamp" in decision.reasons
    history = list_paper_risk_decisions(first)
    assert len(history) == 1
    assert history[0]["input"]["proposal"]["symbol"] == "AAPL"
    assert history[0]["input"]["limits"]["trading_enabled"] is False
    assert history[0]["reasons"] == list(decision.reasons)


def test_account_state_and_limits_are_loaded_only_for_owner(accounts, proposal):
    first, second = accounts
    record_paper_risk_decision(first, proposal, market_open=True, now=NOW)
    record_paper_risk_decision(second, proposal, market_open=True, now=NOW)
    _enable_account(first)
    approved = record_paper_risk_decision(
        first,
        replace(proposal, proposal_id="signal-2"),
        market_open=True,
        now=NOW,
    )
    rejected = record_paper_risk_decision(
        second,
        replace(proposal, proposal_id="signal-2"),
        market_open=True,
        now=NOW,
    )
    assert approved.approved
    assert not rejected.approved
    assert "trading_disabled" in rejected.reasons
    first_history = list_paper_risk_decisions(first)
    second_history = list_paper_risk_decisions(second)
    assert len(first_history) == len(second_history) == 2
    assert first_history[0]["input"]["portfolio"]["cash"] == "500"
    assert second_history[0]["input"]["portfolio"]["cash"] == "0"


def test_stale_portfolio_and_positions_fail_closed(accounts, proposal):
    first, _ = accounts
    record_paper_risk_decision(first, proposal, market_open=True, now=NOW)
    _enable_account(first, verified_at=NOW - timedelta(minutes=2))
    stale = record_paper_risk_decision(
        first,
        replace(proposal, proposal_id="stale"),
        market_open=True,
        now=NOW,
    )
    assert not stale.approved
    assert "stale_or_future_data" in stale.reasons

    _enable_account(first)
    with get_connection() as conn:
        conn.execute(
            "INSERT INTO paper_positions (user_id, symbol, quantity) VALUES (?, ?, ?)",
            (first, "AAPL", "2"),
        )
        conn.execute(
            "UPDATE paper_accounts SET gross_exposure = '200.50' WHERE user_id = ?",
            (first,),
        )
    sell = replace(proposal, proposal_id="sell", side="SELL", quantity=Decimal("2"))
    assert record_paper_risk_decision(first, sell, market_open=True, now=NOW).approved


def test_duplicate_proposal_is_not_reapproved_or_rewritten(accounts, proposal):
    first, _ = accounts
    record_paper_risk_decision(first, proposal, market_open=True, now=NOW)
    _enable_account(first)
    with pytest.raises(DuplicateProposal):
        record_paper_risk_decision(first, proposal, market_open=True, now=NOW)
    assert len(list_paper_risk_decisions(first)) == 1
    assert not list_paper_risk_decisions(first)[0]["approved"]


def test_concurrent_duplicate_proposals_append_only_once(accounts, proposal):
    first, _ = accounts

    def record(_):
        try:
            record_paper_risk_decision(first, proposal, market_open=True, now=NOW)
            return "recorded"
        except DuplicateProposal:
            return "duplicate"

    with ThreadPoolExecutor(max_workers=4) as pool:
        outcomes = list(pool.map(record, range(4)))
    assert outcomes.count("recorded") == 1
    assert outcomes.count("duplicate") == 3
    assert len(list_paper_risk_decisions(first)) == 1


def test_unknown_or_deleted_user_cannot_record_and_data_cascades(accounts, proposal):
    first, _ = accounts
    with pytest.raises(PaperAccountNotFound):
        record_paper_risk_decision("missing", proposal, market_open=True, now=NOW)
    record_paper_risk_decision(first, proposal, market_open=True, now=NOW)
    with get_connection() as conn:
        conn.execute("DELETE FROM users WHERE id = ?", (first,))
        assert (
            conn.execute("SELECT 1 FROM paper_accounts WHERE user_id = ?", (first,)).fetchone()
            is None
        )
        assert (
            conn.execute(
                "SELECT 1 FROM paper_risk_decisions WHERE user_id = ?", (first,)
            ).fetchone()
            is None
        )
    with pytest.raises(PaperAccountNotFound):
        record_paper_risk_decision(
            first, replace(proposal, proposal_id="next"), market_open=True, now=NOW
        )


def test_history_limit_is_bounded(accounts):
    first, _ = accounts
    for invalid in (0, -1, 101, True):
        with pytest.raises(ValueError):
            list_paper_risk_decisions(first, limit=invalid)

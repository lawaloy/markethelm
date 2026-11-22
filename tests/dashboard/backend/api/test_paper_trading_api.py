"""Paper decision history must use the authenticated account identity."""

from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient

from dashboard.backend.main import app
from src.storage.database import init_database
from src.storage.paper_trading import record_paper_risk_decision
from src.storage.session import create_access_token
from src.storage.users import create_user, revoke_user_sessions
from src.trading.risk import TradeProposal

NOW = datetime(2026, 11, 20, 15, 0, tzinfo=timezone.utc)


@pytest.fixture
def db_users(tmp_path, monkeypatch):
    db = tmp_path / "paper-api.db"
    monkeypatch.setenv("MARKETHELM_DATABASE_URL", f"sqlite:///{db.as_posix()}")
    monkeypatch.setenv("MARKETHELM_AUTH_SECRET", "test-secret-min-16-chars")
    init_database()
    first = create_user("paper-api-one@example.com", "password123")
    second = create_user("paper-api-two@example.com", "password123")
    return first, second


def _proposal(proposal_id: str) -> TradeProposal:
    return TradeProposal(
        proposal_id=proposal_id,
        symbol="AAPL",
        side="BUY",
        quantity=Decimal("1"),
        reference_price=Decimal("100"),
        quote_at=NOW - timedelta(seconds=5),
        strategy_version="test-v1",
    )


def test_history_requires_auth_and_never_accepts_another_user_id(db_users):
    first, second = db_users
    record_paper_risk_decision(first["id"], _proposal("first-only"), market_open=True, now=NOW)
    record_paper_risk_decision(second["id"], _proposal("second-only"), market_open=True, now=NOW)
    token = create_access_token(first["id"])
    with TestClient(app) as client:
        assert client.get("/api/paper/decisions").status_code == 401
        response = client.get(
            f"/api/paper/decisions?user_id={second['id']}",
            headers={"Authorization": f"Bearer {token}"},
        )
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert [row["proposal_id"] for row in response.json()["decisions"]] == ["first-only"]


def test_history_is_unavailable_without_account_database(monkeypatch):
    monkeypatch.delenv("MARKETHELM_DATABASE_URL", raising=False)
    with TestClient(app) as client:
        response = client.get("/api/paper/decisions")
    assert response.status_code == 404


def test_history_rejects_revoked_session(db_users):
    first, _ = db_users
    token = create_access_token(first["id"])
    revoke_user_sessions(first["id"])
    with TestClient(app) as client:
        response = client.get("/api/paper/decisions", headers={"Authorization": f"Bearer {token}"})
    assert response.status_code == 401

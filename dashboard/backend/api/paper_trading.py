"""Authenticated, read-only paper-trading decision history."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from fastapi.concurrency import run_in_threadpool

from dashboard.backend.auth import require_user_id
from src.storage.database import database_enabled
from src.storage.paper_trading import list_paper_risk_decisions

router = APIRouter()


@router.get("/decisions")
async def get_paper_decisions(
    response: Response,
    limit: int = Query(50, ge=1, le=100),
    user_id: str | None = Depends(require_user_id),
) -> dict:
    """Expose only the signed-in account's recorded paper risk decisions."""
    if not database_enabled():
        raise HTTPException(status_code=404, detail="Paper trading requires an account database.")
    if not user_id:
        raise HTTPException(status_code=401, detail="Authentication required.")
    rows = await run_in_threadpool(list_paper_risk_decisions, user_id, limit=limit)
    response.headers["Cache-Control"] = "no-store"
    return {
        "decisions": [
            {**row, "notional": str(row["notional"]) if row["notional"] is not None else None}
            for row in rows
        ]
    }

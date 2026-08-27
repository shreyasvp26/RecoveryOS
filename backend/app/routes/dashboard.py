"""Phase 10 read-only dashboard HTTP boundaries.

These GET endpoints reflect persisted state for the operator dashboard
(Recovery Command Center, Event Decision Trace, Policy & Blocked Actions).
They hold no business logic and no SQL; they only wire HTTP to the read
helpers in ``app/dashboard.py``. Nothing here makes, changes, or fabricates a
decision, and the routes never recompute policy or benchmark logic.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator

from fastapi import APIRouter, Depends, HTTPException, status

from .. import db
from ..dashboard import (
    build_blocked_decisions,
    build_dashboard_summary,
    build_event_trace,
)

router = APIRouter(tags=["dashboard"])


def get_db() -> Iterator[sqlite3.Connection]:
    """FastAPI dependency: connect to the configured SQLite DB (read-only use)."""
    conn = db.connect_database()
    db.init_db(conn)
    try:
        yield conn
    finally:
        conn.close()


@router.get("/dashboard/summary")
def dashboard_summary(
    conn: sqlite3.Connection = Depends(get_db),
) -> dict:
    """Command Center metrics plus the persisted benchmark comparison."""
    return build_dashboard_summary(conn)


@router.get("/events")
def list_events(
    limit: int = 50,
    query: str | None = None,
    risk_flag: str | None = None,
    conn: sqlite3.Connection = Depends(get_db),
) -> dict:
    """List persisted events (newest first), optionally filtered."""
    limit = max(1, min(int(limit), 200))
    events = db.list_payment_events(
        conn, limit=limit, query=query, risk_flag=risk_flag
    )
    return {"count": len(events), "events": events}


@router.get("/events/{event_id}/trace")
def event_trace(
    event_id: str,
    conn: sqlite3.Connection = Depends(get_db),
) -> dict:
    """The historical decision chain for one event."""
    trace = build_event_trace(conn, event_id)
    if trace is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"event {event_id} not found",
        )
    return trace


@router.get("/decisions/blocked")
def blocked_decisions(
    limit: int = 100,
    conn: sqlite3.Connection = Depends(get_db),
) -> dict:
    """Denied policy decisions with event/customer context."""
    limit = max(1, min(int(limit), 500))
    return build_blocked_decisions(conn, limit=limit)

"""Phase 21 Recovery Operations HTTP boundaries.

    GET /recovery/queue    the operational projection over persisted state

The queue endpoint holds no decision logic and no SQL: it wires HTTP to
``recovery_operations``, exactly as the dashboard routes wire HTTP to
``dashboard``. It reads persisted records, derives no new authority, and
writes nothing.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator

from fastapi import APIRouter, Depends, status
from fastapi.responses import JSONResponse

from .. import db
from ..recovery_operations import (
    DEFAULT_LIMIT,
    DEFAULT_SCAN_LIMIT,
    MAX_LIMIT,
    SORT_NEWEST,
    RecoveryQueueError,
    build_recovery_queue,
)

router = APIRouter(tags=["recovery-operations"])


def get_db() -> Iterator[sqlite3.Connection]:
    """FastAPI dependency: connect to the configured SQLite DB."""
    conn = db.connect_database()
    db.init_db(conn)
    try:
        yield conn
    finally:
        conn.close()


@router.get("/recovery/queue")
def recovery_queue(
    lifecycle_state: str | None = None,
    execution_mode: str | None = None,
    risk_flag: str | None = None,
    failure_reason: str | None = None,
    intervention: str | None = None,
    policy_status: str | None = None,
    sort: str = SORT_NEWEST,
    limit: int = DEFAULT_LIMIT,
    conn: sqlite3.Connection = Depends(get_db),
) -> JSONResponse:
    """Return the Recovery Operations queue for the requested filter and order.

    An unknown lifecycle state or sort order is rejected explicitly rather than
    silently ignored, so an operator never reads a filtered view that quietly
    did not apply the filter they asked for.
    """
    try:
        payload = build_recovery_queue(
            conn,
            lifecycle_state=lifecycle_state,
            execution_mode=execution_mode,
            risk_flag=risk_flag,
            failure_reason=failure_reason,
            intervention=intervention,
            policy_status=policy_status,
            sort=sort,
            limit=max(1, min(int(limit), MAX_LIMIT)),
            scan_limit=DEFAULT_SCAN_LIMIT,
        )
    except RecoveryQueueError as exc:
        return JSONResponse(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            content={"status": "invalid_request", "detail": str(exc)},
        )
    return JSONResponse(status_code=status.HTTP_200_OK, content=payload)

"""HTTP ingestion boundary for payment events.

Phase 4: exposes a minimal POST /events endpoint. Routes hold no business
logic and no SQL; they only translate between HTTP and the ingestion service.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from typing import Any

from fastapi import APIRouter, Depends, status
from fastapi.responses import JSONResponse

from ..db import connect_database, init_db
from ..ingestion import IngestionStatus, ingest_event

router = APIRouter(tags=["events"])


def get_db() -> Iterator[sqlite3.Connection]:
    """FastAPI dependency: provide a connection to the configured SQLite DB."""
    conn = connect_database()
    init_db(conn)
    try:
        yield conn
    finally:
        conn.close()


@router.post("/events")
def create_event(
    payload: dict[str, Any],
    conn: sqlite3.Connection = Depends(get_db),
) -> JSONResponse:
    """Validate and persist an ingested payment event."""
    result = ingest_event(conn, payload)

    if result.status is IngestionStatus.SUCCESS:
        return JSONResponse(
            status_code=status.HTTP_201_CREATED,
            content={"status": "success", "event_id": result.event_id},
        )
    if result.status is IngestionStatus.DUPLICATE:
        return JSONResponse(
            status_code=status.HTTP_409_CONFLICT,
            content={
                "status": "duplicate",
                "event_id": result.event_id,
                "detail": result.detail,
            },
        )
    if result.status is IngestionStatus.INVALID:
        return JSONResponse(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            content={"status": "invalid", "detail": result.detail},
        )
    return JSONResponse(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        content={
            "status": "error",
            "event_id": result.event_id,
            "detail": result.detail,
        },
    )

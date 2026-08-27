"""HTTP boundaries for payment events.

Phase 4: exposes a minimal POST /events ingestion endpoint. Phase 5: exposes
POST /events/{event_id}/classify, which loads a persisted event, runs the
advisory AI classifier, persists the classification, and returns it. Routes
hold no business logic and no SQL; they only wire HTTP to the services.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from typing import Any

from fastapi import APIRouter, Depends, status
from fastapi.responses import JSONResponse

from ..classifier import (
    ClassificationValidationError,
    OmniRouteClassifier,
    OmniRouteError,
    build_omniroute_adapter,
    classify_event,
)
from ..db import (
    connect_database,
    get_payment_event,
    init_db,
    insert_classification_result,
)
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


def get_classifier() -> OmniRouteClassifier:
    """FastAPI dependency: provide the configured OmniRoute classifier."""
    return build_omniroute_adapter()


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


@router.post("/events/{event_id}/classify")
def classify_event_endpoint(
    event_id: str,
    conn: sqlite3.Connection = Depends(get_db),
    classifier: OmniRouteClassifier = Depends(get_classifier),
) -> JSONResponse:
    """Load an event, classify it, persist the classification, and return it.

    The classification is advisory only; this endpoint never selects or
    executes an action and never calls the payment provider.
    """
    event = get_payment_event(conn, event_id)
    if event is None:
        return JSONResponse(
            status_code=status.HTTP_404_NOT_FOUND,
            content={"status": "not_found", "event_id": event_id},
        )

    try:
        result = classify_event(event, classifier)
    except ClassificationValidationError:
        return JSONResponse(
            status_code=status.HTTP_502_BAD_GATEWAY,
            content={
                "status": "classification_validation_failure",
                "event_id": event_id,
            },
        )
    except OmniRouteError:
        return JSONResponse(
            status_code=status.HTTP_502_BAD_GATEWAY,
            content={"status": "classification_llm_error", "event_id": event_id},
        )
    except Exception:
        return JSONResponse(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            content={"status": "classification_error", "event_id": event_id},
        )

    try:
        insert_classification_result(conn, result)
    except sqlite3.Error:
        return JSONResponse(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            content={
                "status": "classification_persistence_failure",
                "event_id": event_id,
            },
        )

    return JSONResponse(
        status_code=status.HTTP_200_OK,
        content={
            "status": "classification_success",
            "classification": result.to_dict(),
        },
    )

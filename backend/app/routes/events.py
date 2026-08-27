"""HTTP boundaries for payment events.

Phase 4: exposes a minimal POST /events ingestion endpoint. Phase 5: exposes
POST /events/{event_id}/classify, which loads a persisted event, runs the
advisory AI classifier, persists the classification, and returns it. Phase 6:
exposes POST /events/{event_id}/policy, which loads the event and its
classification, derives historical policy context from persisted state,
evaluates one proposed intervention through the deterministic policy gate,
persists the decision, and returns it. Routes hold no business logic and no
SQL; they only wire HTTP to the services. The policy endpoint never executes
an intervention and never calls the payment provider.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from datetime import datetime, timezone
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
from ..config import build_policy_config
from ..db import (
    connect_database,
    get_classification_result,
    get_payment_event,
    get_policy_history,
    init_db,
    insert_classification_result,
    insert_policy_decision,
)
from ..ingestion import IngestionStatus, ingest_event
from ..policy import (
    PolicyConfig,
    PolicyEngine,
    PolicyInput,
    PolicyValidationError,
    parse_aware_datetime,
)

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


def get_policy_config() -> PolicyConfig:
    """FastAPI dependency: provide the configured deterministic policy gate."""
    return build_policy_config()


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


@router.post("/events/{event_id}/policy")
def evaluate_event_policy(
    event_id: str,
    payload: dict[str, Any],
    conn: sqlite3.Connection = Depends(get_db),
    config: PolicyConfig = Depends(get_policy_config),
) -> JSONResponse:
    """Evaluate one proposed intervention through the deterministic gate.

    Orchestrates load event -> load classification -> derive historical
    policy context from persisted state -> evaluate policy -> persist the
    decision -> return it. This endpoint never executes an intervention, never
    selects among candidates, and never calls the payment provider.
    """
    event = get_payment_event(conn, event_id)
    if event is None:
        return JSONResponse(
            status_code=status.HTTP_404_NOT_FOUND,
            content={"status": "not_found", "event_id": event_id},
        )

    classification = get_classification_result(conn, event_id)
    if classification is None:
        return JSONResponse(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            content={"status": "no_classification", "event_id": event_id},
        )

    if "proposed_intervention" not in payload:
        return JSONResponse(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            content={
                "status": "invalid_request",
                "event_id": event_id,
                "detail": "proposed_intervention is required",
            },
        )
    proposed_intervention = payload["proposed_intervention"]

    if "evaluation_time" in payload and payload["evaluation_time"] is not None:
        try:
            evaluation_time = parse_aware_datetime(payload["evaluation_time"])
        except PolicyValidationError as exc:
            return JSONResponse(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                content={
                    "status": "policy_validation_failure",
                    "event_id": event_id,
                    "detail": str(exc),
                },
            )
    else:
        evaluation_time = datetime.now(timezone.utc)

    try:
        history = get_policy_history(conn, event, evaluation_time)
    except PolicyValidationError as exc:
        return JSONResponse(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            content={
                "status": "policy_validation_failure",
                "event_id": event_id,
                "detail": str(exc),
            },
        )
    except sqlite3.Error:
        return JSONResponse(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            content={
                "status": "policy_history_error",
                "event_id": event_id,
            },
        )

    try:
        decision = PolicyEngine().evaluate(
            PolicyInput(
                event=event,
                classification=classification,
                proposed_intervention=proposed_intervention,
                history=history,
                evaluation_time=evaluation_time,
            ),
            config,
        )
    except PolicyValidationError as exc:
        return JSONResponse(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            content={
                "status": "policy_validation_failure",
                "event_id": event_id,
                "detail": str(exc),
            },
        )

    try:
        insert_policy_decision(conn, decision)
    except sqlite3.Error:
        return JSONResponse(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            content={
                "status": "policy_decision_persistence_failure",
                "event_id": event_id,
            },
        )

    return JSONResponse(
        status_code=status.HTTP_200_OK,
        content={"status": "policy_success", "decision": decision.to_dict()},
    )

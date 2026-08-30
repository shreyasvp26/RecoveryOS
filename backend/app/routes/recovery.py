"""Phase 21 Recovery Operations HTTP boundaries.

    GET  /recovery/queue                the operational projection
    POST /recovery/{event_id}/execute   the operator execution entry point

The queue endpoint holds no decision logic and no SQL: it wires HTTP to
``recovery_operations``, exactly as the dashboard routes wire HTTP to
``dashboard``. It reads persisted records, derives no new authority, and
writes nothing.

THE EXECUTE ENDPOINT IS AN ENTRY POINT, NOT A DECISION PATH
-----------------------------------------------------------
It re-runs the SAME ``execution_service.execute_event`` the Phase 7 endpoint
runs, which re-derives classification, the deterministic policy decisions, the
economic selection and the bounded execution from authoritative server state.
The route reimplements none of them.

The operator therefore chooses only WHETHER to act on an event, never WHAT is
done or whether it is permitted. A request that tries to carry an intervention
or an authorization is refused outright rather than quietly ignored, so a
client can never believe it influenced the decision. The evaluation time comes
from the server, so cooldown and customer limits cannot be sidestepped either.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from datetime import datetime
from typing import Any

from fastapi import APIRouter, Body, Depends, status
from fastapi.responses import JSONResponse

from .. import calibration_service
from .. import db
from ..economics import EconomicsError
from ..execution_service import (
    STATUS_ALREADY_EXECUTED,
    STATUS_EXECUTION_FAILED,
    STATUS_EXECUTION_IN_PROGRESS,
    STATUS_EXECUTION_SUCCESS,
    STATUS_MISSING_CLASSIFICATION,
    STATUS_NOT_FOUND,
    STATUS_NO_ACTION,
    STATUS_PROVIDER_RESULT_UNKNOWN,
    execute_event,
)
from ..optimizer import OptimizerError
from ..optimizer_audit import OptimizerAuditError
from ..policy import PolicyConfig, PolicyValidationError
from ..recovery_operations import (
    DEFAULT_LIMIT,
    DEFAULT_SCAN_LIMIT,
    MAX_LIMIT,
    SORT_NEWEST,
    RecoveryQueueError,
    build_queue_row_for_event,
    build_recovery_queue,
)
from .events import get_now, get_policy_config, get_razorpay_client

router = APIRouter(tags=["recovery-operations"])

# A request carrying any of these is trying to supply authority the client does
# not have. Refusing it loudly is the point: silence would let a caller believe
# its value mattered.
FORBIDDEN_REQUEST_FIELDS: tuple[str, ...] = (
    "intervention",
    "selected_intervention",
    "allowed",
    "policy_decision",
    "authorization",
    "authorized",
    "evaluation_time",
    "execution_mode",
    "force",
)

# The single permitted attempt for this action belongs to another request, has
# already happened, or ended in a state RecoveryOS cannot confirm.
_CONFLICT_STATUSES: frozenset[str] = frozenset(
    {
        STATUS_EXECUTION_IN_PROGRESS,
        STATUS_ALREADY_EXECUTED,
        STATUS_PROVIDER_RESULT_UNKNOWN,
    }
)

_CONFLICT_DETAIL: dict[str, str] = {
    STATUS_EXECUTION_IN_PROGRESS: (
        "another execution for this action is already in flight; nothing was "
        "executed"
    ),
    STATUS_ALREADY_EXECUTED: (
        "this action has already been executed; it is never executed twice"
    ),
    STATUS_PROVIDER_RESULT_UNKNOWN: (
        "a previous attempt called the provider and its result could not be "
        "confirmed; this action is not retried automatically because doing so "
        "could duplicate a real provider-side effect"
    ),
}


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


@router.post("/recovery/{event_id}/execute")
def execute_from_recovery_queue(
    event_id: str,
    payload: dict[str, Any] | None = Body(default=None),
    conn: sqlite3.Connection = Depends(get_db),
    config: PolicyConfig = Depends(get_policy_config),
    now: datetime = Depends(get_now),
    razorpay_client: object | None = Depends(get_razorpay_client),
) -> JSONResponse:
    """Run the authoritative execution flow for one event, on operator request.

    The response carries the freshly projected queue row, so the operator sees
    the state the server actually recorded rather than an optimistic guess. A
    real Payment Link that was created successfully comes back as
    PENDING_OUTCOME, never as recovered.
    """
    if isinstance(payload, dict):
        supplied = [field for field in FORBIDDEN_REQUEST_FIELDS if field in payload]
        if supplied:
            return JSONResponse(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                content={
                    "status": "client_authority_rejected",
                    "event_id": event_id,
                    "detail": (
                        f"the request supplied {sorted(supplied)}, which the client "
                        "does not decide; the intervention, the authorization and "
                        "the evaluation time are derived from authoritative server "
                        "state. Nothing was executed."
                    ),
                },
            )

    try:
        # Phase 23 (additive): when an active calibration snapshot exists, the
        # operator path ranks with calibrated posteriors, exactly as the Phase 7
        # execute endpoint does; otherwise it keeps the frozen baseline. A read
        # failure degrades to the baseline rather than guessing a probability,
        # never altering authorization or execution.
        estimator = None
        try:
            estimator = calibration_service.build_production_estimator(conn)
        except Exception:
            estimator = None
        result = execute_event(
            conn, event_id, now, config, razorpay_client, estimator=estimator
        )
    except (EconomicsError, OptimizerError, OptimizerAuditError) as exc:
        return JSONResponse(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            content={
                "status": "economic_selection_failure",
                "event_id": event_id,
                "detail": str(exc) or "the economic decision could not be made",
            },
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
    except sqlite3.Error:
        return JSONResponse(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            content={
                "status": "execution_persistence_failure",
                "event_id": event_id,
            },
        )

    if result.status == STATUS_NOT_FOUND:
        return JSONResponse(
            status_code=status.HTTP_404_NOT_FOUND,
            content={"status": STATUS_NOT_FOUND, "event_id": event_id},
        )
    if result.status == STATUS_MISSING_CLASSIFICATION:
        return JSONResponse(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            content={
                "status": STATUS_MISSING_CLASSIFICATION,
                "event_id": event_id,
                "detail": "no valid ClassificationResult; nothing was executed",
            },
        )

    content: dict[str, Any] = {
        "status": result.status,
        "event_id": event_id,
        "selected_intervention": result.selected_intervention,
        "policy_decision": (
            result.decision.to_dict() if result.decision is not None else None
        ),
        "execution": result.outcome.to_dict() if result.outcome is not None else None,
        "row": build_queue_row_for_event(conn, event_id),
    }

    if result.status in _CONFLICT_STATUSES:
        content["detail"] = _CONFLICT_DETAIL[result.status]
        return JSONResponse(status_code=status.HTTP_409_CONFLICT, content=content)

    if result.status == STATUS_NO_ACTION:
        # Every candidate was denied, or none was economically worthwhile. The
        # queue row explains which, from the persisted decisions.
        content["detail"] = (
            "no intervention was authorized and economically selected for this "
            "event; nothing was executed"
        )
    elif result.status not in (STATUS_EXECUTION_SUCCESS, STATUS_EXECUTION_FAILED):
        content["detail"] = "the execution flow returned an unrecognized state"

    return JSONResponse(status_code=status.HTTP_200_OK, content=content)

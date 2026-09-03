"""Phase 23 estimator-evidence HTTP boundary.

    GET  /estimator-evidence            current + historical calibration state
    POST /estimator-evidence/recalibrate   append the next immutable snapshot

The GET is read-only: it reports the latest versioned snapshot, whether any
intervention is active (and thus feeding production ranking), the underlying
sample counts, and the full snapshot history. It holds no analytics logic and
no raw SQL; it wires HTTP to ``calibration_service``.

The POST is the operator's explicit recalibrate trigger. It rebuilds the
calibration evidence (verified webhook recoveries + a read-only provider poll of
still-unsettled links) and appends exactly ONE new immutable, versioned snapshot
row. It NEVER executes an intervention, NEVER authorizes anything, and NEVER
rewrites a historical snapshot or decision. A snapshot only becomes active (and
only then may change the probabilities that rank decisions) when an intervention
meets every calibration threshold with its own terminal evidence.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, status
from fastapi.responses import JSONResponse

from .. import calibration_service, db
from ..calibration import calibration_samples, outcome_counts
from ..config import build_razorpay_client
from ..razorpay_client import RazorpayConfigurationError

router = APIRouter(tags=["estimator-evidence"])


def get_db() -> Iterator[sqlite3.Connection]:
    """FastAPI dependency: connect to the configured SQLite DB."""
    conn = db.connect_database()
    db.init_db(conn)
    try:
        yield conn
    finally:
        conn.close()


def get_provider() -> object | None:
    """FastAPI dependency: the configured Razorpay Test Mode boundary (or None).

    ``None`` (credentials absent) is allowed: calibration then uses only the
    durable webhook/provider evidence already persisted and performs no live
    poll. An explicitly invalid configuration surfaces as a controlled 500.
    """
    try:
        return build_razorpay_client()
    except RazorpayConfigurationError as exc:
        return JSONResponse(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            content={"status": "razorpay_configuration_error", "detail": str(exc)},
        )


@router.get("/estimator-evidence")
def estimator_evidence(
    conn: sqlite3.Connection = Depends(get_db),
) -> JSONResponse:
    """Return the current and historical calibration state (read-only).

    ``active_version`` is the version feeding production when it has at least
    one active (gated) intervention; otherwise ``None`` (production stays on the
    frozen baseline). ``snapshots`` lists every immutable snapshot, oldest first,
    so provenance of past estimates is always reconstructable.
    """
    snapshots = db.list_calibration_snapshots(conn)
    latest = snapshots[-1] if snapshots else None
    active_version = (
        latest["version"]
        if latest is not None and latest.get("active_bps", {})
        else None
    )

    # The frontend's calibration screen reads a `samples` block from the latest
    # snapshot (total + per-outcome counts). The persisted snapshot row stores
    # only active_bps/evidenced, not the per-observation samples it was built
    # from, so the GET reconstructs them from the CURRENT durable evidence.
    # Read-only: provider is None, so no live poll and no write occurs — only
    # already-persisted webhook recoveries and provider outcomes are counted.
    if latest is not None:
        observations = calibration_service.build_calibration_observations(
            conn, provider=None
        )
        latest = {**latest, "samples": {
            "total": len(calibration_samples(observations)),
            "outcome_counts": outcome_counts(observations),
        }}

    return JSONResponse(
        status_code=status.HTTP_200_OK,
        content={
            "latest": latest,
            "active_version": active_version,
            "snapshot_count": len(snapshots),
            "snapshots": snapshots,
        },
    )


@router.post("/estimator-evidence/recalibrate")
def recalibrate(
    conn: sqlite3.Connection = Depends(get_db),
    provider: object | None = Depends(get_provider),
) -> JSONResponse:
    """Append the next immutable calibration snapshot from current evidence.

    The operator explicitly requests this; nothing in the system recalibrates on
    its own. The build projects terminal REAL_RAZORPAY payment_link evidence and
    appends a NEW version; if the gate is met the new snapshot becomes active.
    This endpoint is an estimator-evidence write only and never executes or
    authorizes anything.
    """
    if isinstance(provider, JSONResponse):
        return provider
    recorded_at = datetime.now(timezone.utc).isoformat()
    try:
        snapshot = calibration_service.build_calibration_snapshot(
            conn, provider, recorded_at
        )
    except Exception as exc:
        return JSONResponse(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            content={
                "status": "estimation_recalibrate_failure",
                "detail": str(exc) or "calibration snapshot could not be built",
            },
        )
    return JSONResponse(status_code=status.HTTP_200_OK, content=snapshot)

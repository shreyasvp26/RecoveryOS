"""Phase 22 Recovery Intelligence HTTP boundary.

    GET /recovery-intelligence   calibration, intervention and segment evidence

One endpoint, because one payload answers the whole question and endpoints
added for symmetry are endpoints that can drift apart. It holds no analytics
logic and no SQL: it wires HTTP to ``recovery_intelligence``, exactly as the
recovery routes wire HTTP to ``recovery_operations``.

READ-ONLY BY CONSTRUCTION
-------------------------
There is deliberately no POST/PUT/PATCH/DELETE here. This module imports no
executor, no policy engine, no optimizer and no estimator, so no request that
reaches it can execute an intervention, authorize an action, or change a
decision. Measurement never acquires authority.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator

from fastapi import APIRouter, Depends, status
from fastapi.responses import JSONResponse

from .. import db
from ..outcome_feedback import DEFAULT_OBSERVATION_LIMIT, build_observations
from ..recovery_intelligence import build_recovery_intelligence, observation_rows

router = APIRouter(tags=["recovery-intelligence"])

MAX_OBSERVATION_ROWS = 200


def get_db() -> Iterator[sqlite3.Connection]:
    """FastAPI dependency: connect to the configured SQLite DB."""
    conn = db.connect_database()
    db.init_db(conn)
    try:
        yield conn
    finally:
        conn.close()


@router.get("/recovery-intelligence")
def recovery_intelligence(
    include_observations: bool = False,
    conn: sqlite3.Connection = Depends(get_db),
) -> JSONResponse:
    """Return the Recovery Intelligence evidence derived from persisted state.

    Every figure is computed from the persisted optimizer decisions, execution
    outcomes and verified webhook recoveries. Nothing is hardcoded, and a
    metric with too little evidence behind it is reported as insufficient
    rather than estimated. ``evidence.population`` states whether the figures
    cover every recorded execution.

    ``include_observations`` attaches the underlying per-observation rows so an
    aggregate can be traced back to the exact execution and provider evidence
    it came from.
    """
    payload = build_recovery_intelligence(conn, limit=DEFAULT_OBSERVATION_LIMIT)
    if include_observations:
        observations = build_observations(conn, limit=DEFAULT_OBSERVATION_LIMIT)
        payload["observations"] = observation_rows(observations)[
            :MAX_OBSERVATION_ROWS
        ]
    return JSONResponse(status_code=status.HTTP_200_OK, content=payload)

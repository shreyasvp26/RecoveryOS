"""Phase 20 Revenue Health HTTP boundaries.

Three read endpoints, because the Revenue Health screen needs exactly three
things: which incidents exist, the full evidence for one of them, and the
payments one covers.

    GET /incidents                  every currently detected incident
    GET /incidents/{id}             one incident's complete evidence
    GET /incidents/{id}/events      the payments that incident covers

These routes hold no detection logic, no metric and no threshold. They wire
HTTP to ``incident_analysis`` (evidence) and ``incidents`` (detection), exactly
as the Policy Lab routes wire HTTP to ``policy_scenario`` and ``replay``.

NO SECOND EVENT-DETAIL SYSTEM
-----------------------------
The events endpoint returns the locked ``PaymentEvent`` contract plus a pointer
to the EXISTING ``/events/{id}/trace`` decision trace. It deliberately does not
restate diagnosis, policy, optimizer or execution detail: there is one Event
Decision Trace in RecoveryOS and this is a link to it, not a copy of it.

READ ONLY
---------
Incidents are derived on demand and persisted nowhere. Nothing in this module
writes to the database, mutates a policy, or performs a payment action.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, status

from .. import db
from ..incident_analysis import (
    affected_events,
    analyse_workload,
    evaluated_outcomes,
    evaluation_identity,
    find_incident,
    incident_evidence,
)
from ..incidents import INCIDENT_RESULT_MODE

router = APIRouter(tags=["revenue-health"])

# The one place the simulated/modelled nature of these figures is worded, so
# every incident response carries the same disclaimer verbatim.
INCIDENT_DISCLAIMER = (
    "Incidents are analytical readings of the controlled simulated evaluation "
    "over the persisted RecoveryOS workload. Recovery figures are simulated "
    "evaluation results and simulated revenue at risk is a modelled estimate, "
    "not production revenue, merchant loss, or confirmed recoverable money. "
    "Detection performs no execution and changes no policy."
)


def get_db() -> Iterator[sqlite3.Connection]:
    """FastAPI dependency: connect to the configured SQLite DB (read-only use)."""
    conn = db.connect_database()
    db.init_db(conn)
    try:
        yield conn
    finally:
        conn.close()


@router.get("/incidents")
def list_incidents(conn: sqlite3.Connection = Depends(get_db)) -> dict[str, Any]:
    """Every incident the persisted workload currently produces.

    Ordered by modelled financial impact descending, then by degradation, then
    by incident id — a total order fixed by the data, so two identical requests
    return an identical list.
    """
    analysis = analyse_workload(conn)
    incidents = analysis["incidents"]
    return {
        "status": "incident_analysis_success",
        "result_mode": INCIDENT_RESULT_MODE,
        "disclaimer": INCIDENT_DISCLAIMER,
        "detection": analysis["detection_config"].to_dict(),
        "windows": (
            incidents[0].windows.to_dict() if incidents else None
        ),
        "evaluation": (
            None
            if analysis["result"] is None
            else evaluation_identity(analysis["result"])
        ),
        "analysed_event_count": len(analysis["events"]),
        "count": len(incidents),
        "incidents": [incident.to_dict() for incident in incidents],
    }


@router.get("/incidents/{incident_id}")
def get_incident(
    incident_id: str, conn: sqlite3.Connection = Depends(get_db)
) -> dict[str, Any]:
    """One incident's complete computed evidence.

    A 404 here means the dataset no longer produces that incident. Ids are
    deterministic, so an id that cannot be found was either never produced by
    this dataset or has been resolved by the data moving on.
    """
    analysis = analyse_workload(conn)
    incident = find_incident(analysis["incidents"], incident_id)
    if incident is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"incident {incident_id} is not present in the current analysis",
        )
    return {
        "status": "incident_success",
        "result_mode": INCIDENT_RESULT_MODE,
        "disclaimer": INCIDENT_DISCLAIMER,
        "detection": analysis["detection_config"].to_dict(),
        "incident": incident_evidence(incident, analysis["result"]),
    }


@router.get("/incidents/{incident_id}/events")
def get_incident_events(
    incident_id: str, conn: sqlite3.Connection = Depends(get_db)
) -> dict[str, Any]:
    """The payments an incident covers, each pointing at its existing trace."""
    analysis = analyse_workload(conn)
    incident = find_incident(analysis["incidents"], incident_id)
    if incident is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"incident {incident_id} is not present in the current analysis",
        )
    outcomes = {
        outcome.event_id: outcome
        for outcome in evaluated_outcomes(analysis["result"])
    }
    events = []
    for event in affected_events(incident, analysis["events"]):
        outcome = outcomes.get(event.event_id)
        events.append(
            {
                "event": event.to_dict(),
                "trace_path": f"/events/{event.event_id}/trace",
                "simulated_recovered": None if outcome is None else outcome.recovered,
                "simulated_recovered_amount_paise": (
                    None if outcome is None else outcome.recovered_amount_paise
                ),
            }
        )
    return {
        "status": "incident_events_success",
        "result_mode": INCIDENT_RESULT_MODE,
        "disclaimer": INCIDENT_DISCLAIMER,
        "incident_id": incident.incident_id,
        "segment": incident.segment.to_dict(),
        "count": len(events),
        "events": events,
    }

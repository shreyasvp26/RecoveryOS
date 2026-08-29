"""Phase 20 Revenue Health HTTP boundaries.

Three read endpoints, because the Revenue Health screen needs exactly three
things: which incidents exist, the full evidence for one of them, and the
payments one covers.

    GET  /incidents                 every currently detected incident
    GET  /incidents/{id}            one incident's complete evidence
    GET  /incidents/{id}/events     the payments that incident covers
    POST /incidents/{id}/replay     the Policy Lab, run on that exact subset

These routes hold no detection logic, no metric and no threshold. They wire
HTTP to ``incident_analysis`` (evidence) and ``incidents`` (detection), exactly
as the Policy Lab routes wire HTTP to ``policy_scenario`` and ``replay``.

NO SECOND EVENT-DETAIL SYSTEM
-----------------------------
The events endpoint returns the locked ``PaymentEvent`` contract plus a pointer
to the EXISTING ``/events/{id}/trace`` decision trace. It deliberately does not
restate diagnosis, policy, optimizer or execution detail: there is one Event
Decision Trace in RecoveryOS and this is a link to it, not a copy of it.

NOTHING EXECUTES, NOTHING IS WRITTEN
-----------------------------------
Incidents are derived on demand and persisted nowhere. The replay endpoint runs
the existing Phase 19 engine over the incident's affected events: it is
simulated throughout, contacts no provider, creates no Payment Link, writes
nothing, and cannot change the policy the live system runs on.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import JSONResponse

from .. import db
from ..incident_analysis import (
    IncidentAnalysisError,
    affected_events,
    analyse_workload,
    evaluated_outcomes,
    evaluation_identity,
    find_incident,
    incident_evidence,
    replay_incident,
)
from ..incidents import INCIDENT_RESULT_MODE, observed_failure_rate_bps
from ..policy_scenario import (
    BUILT_IN_SCENARIO_IDS,
    SCENARIO_CURRENT,
    PolicyScenario,
    PolicyScenarioError,
    resolve_scenario,
)
from ..replay import ReplayError

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

# The same ceiling the Policy Lab applies, for the same reason: one request
# replays the subset once per scenario and must stay bounded.
MAX_SCENARIOS_PER_INCIDENT_REPLAY = 6

# Replaying an incident with no scenarios named compares the active policy
# against its two derived alternatives — the Policy Lab's own default arms.
DEFAULT_INCIDENT_REPLAY_SCENARIOS: tuple[dict[str, str], ...] = tuple(
    {"scenario_id": scenario_id} for scenario_id in BUILT_IN_SCENARIO_IDS
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
        "population": {
            "observed_failure_rate_bps": observed_failure_rate_bps(
                len(analysis["events"])
            ),
            "basis": (
                "failed payments / total payments over the analysed population; "
                "RecoveryOS only ingests already-failed payments, so this is "
                "100% by construction, is identical in every window and segment, "
                "and is never an input to detection"
            ),
        },
        "status_contract": {
            "OPEN": "in the current detection result",
            "RESOLVED": (
                "absent from the current result when reconciling against a "
                "previously observed set; Phase 20 derives incidents and does "
                "not persist incident history, so a stateless request returns "
                "only OPEN incidents"
            ),
        },
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


@router.post("/incidents/{incident_id}/replay")
def replay_incident_events(
    incident_id: str,
    payload: dict[str, Any] | None = None,
    conn: sqlite3.Connection = Depends(get_db),
) -> Any:
    """Replay policy scenarios over one incident's affected payments.

    Request (both fields optional)::

        {
          "scenarios": [{"scenario_id": "current"}, {"scenario_id": "custom", ...}],
          "reference_scenario_id": "current"
        }

    Answers the only question worth asking about an incident: would a different
    policy have done better on exactly these payments? The comparison is
    produced by the existing Phase 19 machinery over the affected subset, is
    SIMULATED throughout, and changes nothing about the running system.
    """
    payload = payload or {}
    if not isinstance(payload, dict):
        return _invalid("request body must be an object")

    analysis = analyse_workload(conn)
    incident = find_incident(analysis["incidents"], incident_id)
    if incident is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"incident {incident_id} is not present in the current analysis",
        )

    definitions = payload.get("scenarios", list(DEFAULT_INCIDENT_REPLAY_SCENARIOS))
    if not isinstance(definitions, list) or not definitions:
        return _invalid("scenarios must be a non-empty list")
    if len(definitions) > MAX_SCENARIOS_PER_INCIDENT_REPLAY:
        return _invalid(
            f"at most {MAX_SCENARIOS_PER_INCIDENT_REPLAY} scenarios can be "
            f"compared in one request, got {len(definitions)}"
        )

    reference_id = payload.get("reference_scenario_id", SCENARIO_CURRENT)
    if not isinstance(reference_id, str) or not reference_id.strip():
        return _invalid("reference_scenario_id must be a non-empty string")

    scenarios: list[PolicyScenario] = []
    for index, definition in enumerate(definitions):
        try:
            scenarios.append(resolve_scenario(definition))
        except PolicyScenarioError as exc:
            # A malformed policy is refused before anything is evaluated.
            return _invalid(str(exc), index=index)

    identifiers = [scenario.scenario_id for scenario in scenarios]
    if len(set(identifiers)) != len(identifiers):
        return _invalid(
            f"each scenario may appear at most once in a comparison; got "
            f"{identifiers}"
        )
    if reference_id not in identifiers:
        return _invalid(
            f"reference_scenario_id {reference_id!r} must be one of the "
            f"requested scenarios {identifiers}"
        )

    try:
        comparison = replay_incident(
            incident,
            analysis["events"],
            scenarios,
            reference_scenario_id=reference_id,
        )
    except (ReplayError, PolicyScenarioError, IncidentAnalysisError) as exc:
        return _invalid(str(exc))
    except ValueError as exc:
        # A comparison that cannot be shown fair is refused, not caveated.
        return JSONResponse(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            content={"status": "replay_comparison_failure", "detail": str(exc)},
        )

    return JSONResponse(
        status_code=status.HTTP_200_OK,
        content={
            "status": "incident_replay_success",
            "disclaimer": INCIDENT_DISCLAIMER,
            **comparison,
        },
    )


def _invalid(detail: str, *, index: int | None = None) -> JSONResponse:
    """Refuse a request explicitly; nothing is evaluated."""
    content: dict[str, Any] = {"status": "invalid_scenario", "detail": detail}
    if index is not None:
        content["scenario_index"] = index
    return JSONResponse(
        status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, content=content
    )

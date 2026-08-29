"""Phase 20 incident analysis over the persisted RecoveryOS workload.

WHAT THIS MODULE IS
-------------------
The adapter between the pure detector in ``incidents.py`` and the evidence
RecoveryOS already holds. It answers three questions and nothing else:

* which payments are we analysing?  -> the persisted ``payment_events``
* what happened to them?            -> the existing Phase 19 replay of the
                                       ACTIVE policy over exactly those events
* which incidents does that imply?  -> ``incidents.detect_incidents``

WHY REPLAY IS THE OUTCOME SOURCE
--------------------------------
The durable pipeline records EXECUTION, not per-event recovery: as the Phase 10
dashboard already states, "the durable pipeline records execution, not per-event
simulated recovery". Recovery evidence in RecoveryOS is produced by the
controlled evaluation, so the detector reads it from the same Phase 19 replay
engine the Policy Lab uses, run over the persisted events under the active
policy. That reuses existing machinery rather than inventing a second outcome
system, and it keeps every recovery figure explicitly SIMULATED.

Only the OBSERVED result of that evaluation crosses into detection — a boolean
and an integer amount per event. Hidden probabilities, expected values and
oracle options are not read here and have no field to travel in.

NOTHING IS WRITTEN, NOTHING EXECUTES
------------------------------------
Every function here is a read. Replay persists nothing, contacts no provider,
creates no Payment Link and cannot mutate the active policy. Incidents
themselves are derived on demand: there is no incidents table, no copy of any
payment event, and no stored incident state that could drift from the data.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from typing import Any, Sequence

from . import db
from .benchmark_config import Phase17BenchmarkConfig
from .incidents import (
    DetectionConfig,
    EvaluatedOutcome,
    Incident,
    detect_incidents,
)
from .models import PaymentEvent
from .policy_scenario import (
    SCENARIO_CURRENT,
    PolicyScenario,
    current_scenario,
)
from .replay import (
    REPLAY_METHODOLOGY_VERSION,
    ReplayResult,
    replay_scenario,
    replay_scenarios,
)
from .replay_metrics import compare_replays


class IncidentAnalysisError(Exception):
    """The workload cannot be analysed honestly."""


def load_workload(conn: sqlite3.Connection) -> tuple[PaymentEvent, ...]:
    """Every persisted payment event, in canonical (event id) order.

    The analysed population is deliberately the SAME persisted events the
    Event Decision Trace renders, so an incident's affected event ids always
    resolve to a real decision chain the operator can open.
    """
    total = db.count_payment_events(conn)
    if total <= 0:
        return ()
    rows = db.list_payment_events(conn, limit=total)
    return tuple(
        sorted(
            (PaymentEvent.from_dict(row) for row in rows),
            key=lambda event: event.event_id,
        )
    )


def evaluation_config(events: Sequence[PaymentEvent]) -> Phase17BenchmarkConfig:
    """The benchmark configuration this analysis evaluates under.

    Built with the event count of the ACTUAL workload so the recorded identity
    describes what was evaluated rather than the canonical 500-event dataset.
    Everything else — the hidden world, the seeds, the economic model — is left
    at the frozen canonical values.
    """
    return Phase17BenchmarkConfig(event_count=max(1, len(events)))


def evaluate_workload(
    events: Sequence[PaymentEvent],
    *,
    scenario: PolicyScenario | None = None,
) -> ReplayResult:
    """Replay the active policy over ``events`` to obtain evaluated outcomes.

    Uses the existing Phase 19 engine unchanged, on the explicit event subset it
    already supports. Nothing is persisted and nothing is executed.
    """
    scenario = scenario or current_scenario()
    return replay_scenario(
        scenario, config=evaluation_config(events), events=events
    )


def evaluated_outcomes(result: ReplayResult) -> tuple[EvaluatedOutcome, ...]:
    """Project replay records onto the detector's evidence contract.

    Records whose replay FAILED are dropped rather than reported as
    unrecovered: a failed evaluation produced no outcome, and counting it as a
    miss would manufacture degradation out of a harness error.
    """
    return tuple(
        EvaluatedOutcome(
            event_id=record.event_id,
            recovered=record.recovered,
            recovered_amount_paise=record.recovered_amount_paise,
        )
        for record in result.records
        if record.failure is None
    )


def analyse_workload(
    conn: sqlite3.Connection,
    *,
    config: DetectionConfig | None = None,
) -> dict[str, Any]:
    """Detect every incident in the persisted workload, with its provenance.

    Returns the incidents together with the events and the replay result they
    were derived from, so callers that need the affected payments or a policy
    comparison do not have to re-derive either.
    """
    events = load_workload(conn)
    if not events:
        return {
            "events": (),
            "result": None,
            "incidents": (),
            "detection_config": (config or DetectionConfig()),
        }
    result = evaluate_workload(events)
    incidents = detect_incidents(
        events, evaluated_outcomes(result), config=config
    )
    return {
        "events": events,
        "result": result,
        "incidents": incidents,
        "detection_config": config or DetectionConfig(),
    }


def find_incident(
    incidents: Sequence[Incident], incident_id: str
) -> Incident | None:
    """The incident with this id, or None. Ids are deterministic, so a stale id
    simply means the dataset no longer produces that incident."""
    for incident in incidents:
        if incident.incident_id == incident_id:
            return incident
    return None


def affected_events(
    incident: Incident, events: Sequence[PaymentEvent]
) -> tuple[PaymentEvent, ...]:
    """The payment events an incident covers, in the incident's own order.

    Resolves ids against the workload rather than carrying copies: the incident
    references existing events, and this is the only place that reference is
    dereferenced.
    """
    by_id = {event.event_id: event for event in events}
    missing = [
        event_id
        for event_id in incident.affected_event_ids
        if event_id not in by_id
    ]
    if missing:
        raise IncidentAnalysisError(
            f"incident {incident.incident_id!r} references events that are not "
            f"in the analysed workload: {missing[:5]}"
        )
    return tuple(by_id[event_id] for event_id in incident.affected_event_ids)


def incident_evidence(
    incident: Incident, result: ReplayResult | None
) -> dict[str, Any]:
    """One incident's payload plus the identity of the evaluation behind it.

    Publishing the evaluation identity is what makes an incident auditable: it
    names the world, the seeds and the policy the recovery evidence came from,
    and it carries no per-event probability.
    """
    payload = incident.to_dict()
    payload["evaluation"] = None if result is None else evaluation_identity(result)
    return payload


# ---------------------------------------------------------------------------
# Incident -> Policy Lab
# ---------------------------------------------------------------------------

# Bumping this identifies a deliberate change to how an incident subset is
# assembled for replay. It is Phase 20's own version and does not touch the
# canonical Phase 19 replay identity.
INCIDENT_REPLAY_VERSION = "phase20-incident-subset-replay-v1"


def incident_replay_id(
    incident: Incident, scenarios: Sequence[PolicyScenario]
) -> str:
    """The deterministic identity of one incident's policy comparison.

    The canonical Phase 19 replay id identifies a scenario against a benchmark
    CONFIGURATION, which does not distinguish one incident's event subset from
    another's. Phase 20 therefore adds its own small identity around the
    incident, the exact events replayed and the policies compared, and leaves
    the Phase 19 identity untouched — every underlying ReplayResult still
    carries its own canonical id.
    """
    payload = {
        "incident_replay_version": INCIDENT_REPLAY_VERSION,
        "replay_methodology": REPLAY_METHODOLOGY_VERSION,
        "incident_id": incident.incident_id,
        "event_ids": sorted(incident.affected_event_ids),
        "policy_fingerprints": sorted(
            f"{scenario.scenario_id}:{scenario.fingerprint()}"
            for scenario in scenarios
        ),
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    digest = hashlib.blake2b(encoded.encode("utf-8"), digest_size=12).hexdigest()
    return f"incident-replay:{INCIDENT_REPLAY_VERSION}:{digest}"


def replay_incident(
    incident: Incident,
    events: Sequence[PaymentEvent],
    scenarios: Sequence[PolicyScenario],
    *,
    reference_scenario_id: str = SCENARIO_CURRENT,
) -> dict[str, Any]:
    """Compare policy scenarios over ONE incident's affected payments.

    This is wiring, not a second replay engine: the affected ids are resolved
    to their existing ``PaymentEvent`` records and handed to the Phase 19
    ``replay_scenarios`` — which already accepts an explicit event subset — and
    the comparison is produced by the Phase 19 ``compare_replays``. Phase 20
    adds an identity for the subset and nothing else.

    The result is SIMULATED throughout. No Razorpay call, no Payment Link, no
    customer-facing action, no database write, and no change to the active
    policy: the scenarios are values passed to the frozen engine, and the
    active policy is only ever READ.
    """
    subset = affected_events(incident, events)
    if not subset:
        raise IncidentAnalysisError(
            f"incident {incident.incident_id!r} covers no payments to replay"
        )
    scenarios = tuple(scenarios)
    results = replay_scenarios(
        scenarios, config=evaluation_config(subset), events=subset
    )
    comparison = compare_replays(results, reference_scenario_id)
    return {
        "incident_id": incident.incident_id,
        "incident_replay_id": incident_replay_id(incident, scenarios),
        "incident_replay_version": INCIDENT_REPLAY_VERSION,
        "segment": incident.segment.to_dict(),
        "affected_event_ids": list(incident.affected_event_ids),
        **comparison,
    }


def evaluation_identity(result: ReplayResult) -> dict[str, Any]:
    """The identity of the replay that produced the recovery evidence."""
    identity = dict(result.identity())
    identity["evidence_source"] = (
        "Phase 19 simulated replay of the active policy over the persisted "
        "workload; observed outcomes only"
    )
    return identity

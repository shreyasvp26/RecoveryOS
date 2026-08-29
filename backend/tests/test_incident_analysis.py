"""Phase 20: incident analysis over the persisted workload.

These tests exercise the adapter between the persisted events, the existing
Phase 19 replay that supplies recovery evidence, and the pure detector. They
use the real generator dataset so the analysed population is the same one the
operator dashboard renders.
"""

from __future__ import annotations

import pytest

from app import db
from app.generator import generate_events
from app.incident_analysis import (
    IncidentAnalysisError,
    affected_events,
    analyse_workload,
    evaluate_workload,
    evaluated_outcomes,
    evaluation_identity,
    find_incident,
    load_workload,
)
from app.incidents import Incident, EvaluatedOutcome
from app.policy import parse_aware_datetime

WORKLOAD_SEED = 42
WORKLOAD_COUNT = 500


@pytest.fixture
def workload_conn(db_conn):
    """A database holding the canonical generated workload."""
    for event in generate_events(seed=WORKLOAD_SEED, count=WORKLOAD_COUNT):
        db.insert_payment_event(db_conn, event)
    return db_conn


# ---------------------------------------------------------------------------
# Workload
# ---------------------------------------------------------------------------


def test_the_analysed_population_is_every_persisted_event(workload_conn):
    events = load_workload(workload_conn)
    assert len(events) == WORKLOAD_COUNT
    assert [event.event_id for event in events] == sorted(
        event.event_id for event in events
    )


def test_an_empty_database_yields_no_incidents(db_conn):
    analysis = analyse_workload(db_conn)
    assert analysis["events"] == ()
    assert analysis["incidents"] == ()
    assert analysis["result"] is None


# ---------------------------------------------------------------------------
# Evidence
# ---------------------------------------------------------------------------


def test_recovery_evidence_comes_from_the_existing_replay_engine(workload_conn):
    events = load_workload(workload_conn)
    result = evaluate_workload(events)

    assert result.replay_mode == "SIMULATED"
    assert result.event_count == len(events)
    outcomes = evaluated_outcomes(result)
    assert outcomes
    assert all(isinstance(outcome, EvaluatedOutcome) for outcome in outcomes)


def test_evaluated_outcomes_carry_only_observed_results(workload_conn):
    events = load_workload(workload_conn)
    outcomes = evaluated_outcomes(evaluate_workload(events))
    for outcome in outcomes:
        assert set(outcome.__dict__) == {
            "event_id",
            "recovered",
            "recovered_amount_paise",
        }


def test_the_evaluation_identity_is_published_with_the_workload(workload_conn):
    events = load_workload(workload_conn)
    identity = evaluation_identity(evaluate_workload(events))
    assert identity["replay_mode"] == "SIMULATED"
    assert identity["event_count"] == WORKLOAD_COUNT
    assert "policy_fingerprint" in identity
    assert not any("true_" in key for key in identity)


# ---------------------------------------------------------------------------
# Detection over real persisted data
# ---------------------------------------------------------------------------


def test_the_canonical_workload_produces_incidents(workload_conn):
    """A real dataset, analysed end to end. Whatever it says, it says twice."""
    incidents = analyse_workload(workload_conn)["incidents"]
    assert incidents, "the canonical workload is expected to degrade somewhere"
    assert all(isinstance(incident, Incident) for incident in incidents)


def test_analysis_is_reproducible_for_the_same_database(workload_conn):
    first = analyse_workload(workload_conn)["incidents"]
    second = analyse_workload(workload_conn)["incidents"]
    assert [incident.to_dict() for incident in first] == [
        incident.to_dict() for incident in second
    ]


def test_incidents_are_ordered_by_modelled_impact(workload_conn):
    incidents = analyse_workload(workload_conn)["incidents"]
    impacts = [
        incident.simulated_revenue_at_risk_paise for incident in incidents
    ]
    assert impacts == sorted(impacts, reverse=True)


def test_every_reported_metric_matches_its_source_evidence(workload_conn):
    """Recompute one incident's headline numbers straight from the evidence."""
    analysis = analyse_workload(workload_conn)
    incident = analysis["incidents"][0]
    outcomes = {
        outcome.event_id: outcome
        for outcome in evaluated_outcomes(analysis["result"])
    }
    windows = incident.windows

    current = [
        event
        for event in analysis["events"]
        if incident.segment.matches(event)
        and windows.contains_current(parse_aware_datetime(event.timestamp))
    ]
    scored = [event for event in current if event.event_id in outcomes]
    recovered = [
        event for event in scored if outcomes[event.event_id].recovered
    ]

    assert incident.current.events == len(current)
    assert incident.current.scored == len(scored)
    assert incident.current.recovery_rate_bps == len(recovered) * 10_000 // len(scored)
    assert incident.current.amount_paise == sum(
        event.amount_paise for event in current
    )
    assert incident.simulated_revenue_at_risk_paise == (
        incident.degradation_bps * incident.current.amount_paise // 10_000
    )


# ---------------------------------------------------------------------------
# Drilldown
# ---------------------------------------------------------------------------


def test_affected_event_ids_resolve_to_persisted_events(workload_conn):
    analysis = analyse_workload(workload_conn)
    incident = analysis["incidents"][0]
    resolved = affected_events(incident, analysis["events"])

    assert [event.event_id for event in resolved] == list(
        incident.affected_event_ids
    )
    for event in resolved:
        assert db.get_payment_event(workload_conn, event.event_id) is not None


def test_affected_events_belong_to_the_incident_segment(workload_conn):
    analysis = analyse_workload(workload_conn)
    incident = analysis["incidents"][0]
    for event in affected_events(incident, analysis["events"]):
        assert incident.segment.matches(event)


def test_an_unresolvable_affected_event_is_refused_not_silently_dropped(
    workload_conn,
):
    analysis = analyse_workload(workload_conn)
    incident = analysis["incidents"][0]
    with pytest.raises(IncidentAnalysisError):
        affected_events(incident, ())


def test_find_incident_returns_none_for_an_unknown_id(workload_conn):
    incidents = analyse_workload(workload_conn)["incidents"]
    assert find_incident(incidents, "incident:does-not-exist") is None
    assert (
        find_incident(incidents, incidents[0].incident_id) is incidents[0]
    )

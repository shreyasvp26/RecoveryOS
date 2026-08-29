"""Phase 20: investigating an incident with the existing Policy Lab.

An incident replay must operate on exactly the affected subset, must be served
by the frozen Phase 19 engine rather than a second implementation, must be
deterministic, and must change nothing about the running system.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app import db
from app.generator import generate_events
from app.incident_analysis import (
    IncidentAnalysisError,
    analyse_workload,
    incident_replay_id,
    replay_incident,
)
from app.main import app
from app.policy_scenario import (
    aggressive_scenario,
    conservative_scenario,
    current_scenario,
)

client = TestClient(app)

WORKLOAD_SEED = 42
WORKLOAD_COUNT = 500


@pytest.fixture
def workload(monkeypatch, tmp_path) -> str:
    db_path = tmp_path / "incident_replay.db"
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{db_path}")
    conn = db.connect(str(db_path))
    db.init_db(conn)
    for event in generate_events(seed=WORKLOAD_SEED, count=WORKLOAD_COUNT):
        db.insert_payment_event(conn, event)
    conn.close()
    return str(db_path)


@pytest.fixture
def analysis(workload):
    conn = db.connect(workload)
    db.init_db(conn)
    try:
        yield analyse_workload(conn)
    finally:
        conn.close()


def scenarios():
    return (current_scenario(), conservative_scenario(), aggressive_scenario())


# ---------------------------------------------------------------------------
# The domain call
# ---------------------------------------------------------------------------


def test_replay_covers_exactly_the_affected_subset(analysis):
    incident = analysis["incidents"][0]
    comparison = replay_incident(incident, analysis["events"], scenarios())

    assert comparison["event_count"] == incident.affected_event_count
    assert comparison["affected_event_ids"] == list(incident.affected_event_ids)
    for arm in comparison["scenarios"]:
        assert arm["metrics"]["event_count"] == incident.affected_event_count


def test_replay_compares_the_current_policy_with_alternatives(analysis):
    incident = analysis["incidents"][0]
    comparison = replay_incident(incident, analysis["events"], scenarios())

    arms = {arm["scenario"]["scenario_id"]: arm for arm in comparison["scenarios"]}
    assert set(arms) == {"current", "conservative", "aggressive"}
    assert arms["current"]["is_reference"] is True
    for arm in arms.values():
        assert "incremental_recovered_revenue_paise" in arm["vs_reference"]
        assert isinstance(
            arm["metrics"]["financial"]["simulated_recovered_revenue_paise"], int
        )


def test_replay_reuses_the_phase19_engine_and_its_fairness_checks(analysis):
    incident = analysis["incidents"][0]
    comparison = replay_incident(incident, analysis["events"], scenarios())

    assert comparison["result_type"] == "simulated_policy_replay"
    assert comparison["replay_mode"] == "SIMULATED"
    assert all(comparison["fairness"].values())
    for arm in comparison["scenarios"]:
        assert arm["identity"]["replay_methodology"] == "phase19-policy-replay-v1"


def test_the_incident_replay_identity_is_deterministic(analysis):
    incident = analysis["incidents"][0]
    first = replay_incident(incident, analysis["events"], scenarios())
    second = replay_incident(incident, analysis["events"], scenarios())

    assert first["incident_replay_id"] == second["incident_replay_id"]
    assert first == second


def test_the_incident_replay_identity_covers_the_subset_and_the_policies(analysis):
    incidents = analysis["incidents"]
    first, other = incidents[0], incidents[1]

    assert incident_replay_id(first, scenarios()) != incident_replay_id(
        other, scenarios()
    )
    assert incident_replay_id(first, scenarios()) != incident_replay_id(
        first, (current_scenario(),)
    )


def test_replaying_an_incident_never_mutates_the_active_policy(analysis):
    before = current_scenario().parameters
    replay_incident(analysis["incidents"][0], analysis["events"], scenarios())
    assert current_scenario().parameters == before


def test_replay_refuses_an_incident_whose_events_are_missing(analysis):
    with pytest.raises(IncidentAnalysisError):
        replay_incident(analysis["incidents"][0], (), scenarios())


# ---------------------------------------------------------------------------
# POST /incidents/{id}/replay
# ---------------------------------------------------------------------------


@pytest.fixture
def incident_id(workload) -> str:
    return client.get("/incidents").json()["incidents"][0]["incident_id"]


def test_the_endpoint_defaults_to_the_three_built_in_scenarios(incident_id):
    payload = client.post(f"/incidents/{incident_id}/replay").json()

    assert payload["status"] == "incident_replay_success"
    assert payload["incident_id"] == incident_id
    assert {arm["scenario"]["scenario_id"] for arm in payload["scenarios"]} == {
        "current",
        "conservative",
        "aggressive",
    }
    assert "not production revenue" in payload["disclaimer"]


def test_the_endpoint_accepts_an_explicit_scenario_selection(incident_id):
    response = client.post(
        f"/incidents/{incident_id}/replay",
        json={
            "scenarios": [
                {"scenario_id": "current"},
                {
                    "scenario_id": "custom",
                    "name": "Looser cooldown",
                    "parameters": {
                        **current_scenario().parameters,
                        "event_cooldown_minutes": 5,
                    },
                },
            ],
            "reference_scenario_id": "current",
        },
    )
    payload = response.json()
    assert response.status_code == 200
    assert [arm["scenario"]["scenario_id"] for arm in payload["scenarios"]] == [
        "current",
        "custom",
    ]


def test_the_endpoint_is_deterministic(incident_id):
    first = client.post(f"/incidents/{incident_id}/replay").json()
    second = client.post(f"/incidents/{incident_id}/replay").json()
    assert first == second


def test_the_endpoint_refuses_an_out_of_bounds_policy(incident_id):
    response = client.post(
        f"/incidents/{incident_id}/replay",
        json={
            "scenarios": [
                {"scenario_id": "current"},
                {
                    "scenario_id": "custom",
                    "parameters": {
                        **current_scenario().parameters,
                        "event_cooldown_minutes": 10_000,
                    },
                },
            ]
        },
    )
    assert response.status_code == 422
    assert response.json()["status"] == "invalid_scenario"


def test_the_endpoint_refuses_a_reference_outside_the_comparison(incident_id):
    response = client.post(
        f"/incidents/{incident_id}/replay",
        json={
            "scenarios": [{"scenario_id": "conservative"}],
            "reference_scenario_id": "current",
        },
    )
    assert response.status_code == 422


def test_the_endpoint_refuses_a_duplicated_scenario(incident_id):
    response = client.post(
        f"/incidents/{incident_id}/replay",
        json={
            "scenarios": [{"scenario_id": "current"}, {"scenario_id": "current"}]
        },
    )
    assert response.status_code == 422


def test_replaying_an_unknown_incident_is_a_404(workload):
    assert client.post("/incidents/incident:nope/replay").status_code == 404


def test_the_endpoint_writes_nothing_to_the_database(workload, incident_id):
    def snapshot():
        conn = db.connect(workload)
        try:
            return (
                db.count_payment_events(conn),
                db.get_policy_decision_stats(conn),
                db.get_execution_outcome_stats(conn),
                db.get_latest_benchmark_run(conn),
            )
        finally:
            conn.close()

    before = snapshot()
    assert client.post(f"/incidents/{incident_id}/replay").status_code == 200
    assert snapshot() == before

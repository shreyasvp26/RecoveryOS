"""Phase 20: the Revenue Health HTTP boundary.

Every response must be derived from the persisted workload, deterministic,
explicitly labelled simulated, free of hidden ground truth, and free of any
side effect on the database or on policy.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app import db
from app.generator import generate_events
from app.main import app

client = TestClient(app)

WORKLOAD_SEED = 42
WORKLOAD_COUNT = 500


@pytest.fixture
def workload(monkeypatch, tmp_path) -> str:
    """Point the API at a temporary database holding the canonical workload."""
    db_path = tmp_path / "incidents_api.db"
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{db_path}")
    conn = db.connect(str(db_path))
    db.init_db(conn)
    for event in generate_events(seed=WORKLOAD_SEED, count=WORKLOAD_COUNT):
        db.insert_payment_event(conn, event)
    conn.close()
    return str(db_path)


@pytest.fixture
def listing(workload) -> dict:
    response = client.get("/incidents")
    assert response.status_code == 200
    return response.json()


# ---------------------------------------------------------------------------
# GET /incidents
# ---------------------------------------------------------------------------


def test_listing_returns_detected_incidents_with_their_methodology(listing):
    assert listing["status"] == "incident_analysis_success"
    assert listing["result_mode"] == "SIMULATED"
    assert listing["count"] == len(listing["incidents"])
    assert listing["count"] > 0
    assert listing["detection"]["window_days"] == 28
    assert listing["detection"]["degradation_threshold_bps"] == 1500
    assert listing["analysed_event_count"] == WORKLOAD_COUNT


def test_listing_labels_every_figure_as_simulated_or_modelled(listing):
    assert "not production revenue" in listing["disclaimer"]
    for incident in listing["incidents"]:
        assert incident["result_mode"] == "SIMULATED"
        assert "modelled estimate" in incident["impact"]["basis"]


def test_listing_is_deterministically_ordered(listing):
    impacts = [
        incident["impact"]["simulated_revenue_at_risk_paise"]
        for incident in listing["incidents"]
    ]
    assert impacts == sorted(impacts, reverse=True)


def test_listing_is_identical_across_repeated_requests(listing):
    assert client.get("/incidents").json() == listing


def test_listing_on_an_empty_database_is_honest(monkeypatch, tmp_path):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'empty.db'}")
    payload = client.get("/incidents").json()
    assert payload["count"] == 0
    assert payload["incidents"] == []
    assert payload["evaluation"] is None
    assert payload["windows"] is None


def test_every_incident_carries_complete_computed_evidence(listing):
    for incident in listing["incidents"]:
        assert incident["incident_id"].startswith("incident:phase20-")
        assert incident["status"] == "OPEN"
        assert incident["severity"] in {"LOW", "MEDIUM", "HIGH", "CRITICAL"}
        assert incident["baseline"]["recovery_rate_bps"] is not None
        assert incident["current"]["recovery_rate_bps"] is not None
        assert incident["deltas"]["degradation_bps"] >= 1500
        assert incident["windows"]["window_days"] == 28
        assert incident["impact"]["simulated_revenue_at_risk_paise"] >= 0
        assert incident["evidence"]["top_failure_reasons"]


def test_incident_deltas_match_the_reported_window_metrics(listing):
    for incident in listing["incidents"]:
        expected = (
            incident["baseline"]["recovery_rate_bps"]
            - incident["current"]["recovery_rate_bps"]
        )
        assert incident["deltas"]["degradation_bps"] == expected
        assert incident["deltas"]["recovery_rate_delta_bps"] == -expected
        assert incident["impact"]["simulated_revenue_at_risk_paise"] == (
            expected * incident["current"]["amount_paise"] // 10_000
        )


# ---------------------------------------------------------------------------
# GET /incidents/{id}
# ---------------------------------------------------------------------------


def test_incident_detail_returns_the_same_evidence_as_the_listing(listing):
    incident = listing["incidents"][0]
    payload = client.get(f"/incidents/{incident['incident_id']}").json()
    detail = payload["incident"]

    assert payload["status"] == "incident_success"
    assert {key: detail[key] for key in incident} == incident
    assert detail["evaluation"]["replay_mode"] == "SIMULATED"


def test_an_unknown_incident_is_a_404(workload):
    response = client.get("/incidents/incident:phase20-unknown")
    assert response.status_code == 404
    assert "not present" in response.json()["detail"]


# ---------------------------------------------------------------------------
# GET /incidents/{id}/events
# ---------------------------------------------------------------------------


def test_incident_events_return_the_affected_payments(listing):
    incident = listing["incidents"][0]
    payload = client.get(f"/incidents/{incident['incident_id']}/events").json()

    assert payload["count"] == incident["impact"]["affected_event_count"]
    assert [item["event"]["event_id"] for item in payload["events"]] == (
        incident["affected_event_ids"]
    )
    assert all(item["simulated_recovered"] is False for item in payload["events"])


def test_incident_events_point_at_the_existing_decision_trace(listing, workload):
    incident = listing["incidents"][0]
    payload = client.get(f"/incidents/{incident['incident_id']}/events").json()
    first = payload["events"][0]

    assert first["trace_path"] == f"/events/{first['event']['event_id']}/trace"
    trace = client.get(first["trace_path"])
    assert trace.status_code == 200
    assert trace.json()["event"]["event_id"] == first["event"]["event_id"]


def test_incident_events_use_the_locked_payment_event_contract(listing):
    incident = listing["incidents"][0]
    payload = client.get(f"/incidents/{incident['incident_id']}/events").json()
    assert set(payload["events"][0]["event"]) == {
        "event_id",
        "order_id",
        "payment_id",
        "customer_id",
        "amount_paise",
        "currency",
        "payment_method",
        "failure_reason",
        "bank",
        "risk_flag",
        "customer_history",
        "timestamp",
    }


def test_incident_events_for_an_unknown_incident_is_a_404(workload):
    assert client.get("/incidents/incident:nope/events").status_code == 404


# ---------------------------------------------------------------------------
# Safety at the boundary
# ---------------------------------------------------------------------------


def test_no_response_leaks_hidden_ground_truth(listing):
    encoded = str(listing)
    for forbidden in (
        "true_probability",
        "true_ev",
        "oracle",
        "probability_bps",
        "hidden",
    ):
        assert forbidden not in encoded.lower()


def test_reading_incidents_does_not_mutate_the_database(workload):
    def snapshot() -> list:
        conn = db.connect(workload)
        try:
            return conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
            ).fetchall() and [
                (
                    db.count_payment_events(conn),
                    db.get_policy_decision_stats(conn),
                    db.get_execution_outcome_stats(conn),
                )
            ]
        finally:
            conn.close()

    before = snapshot()
    incident_id = client.get("/incidents").json()["incidents"][0]["incident_id"]
    client.get(f"/incidents/{incident_id}")
    client.get(f"/incidents/{incident_id}/events")
    assert snapshot() == before

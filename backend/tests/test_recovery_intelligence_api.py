"""Phase 22 API tests for GET /recovery-intelligence."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from fastapi.testclient import TestClient

from app.db import (
    connect,
    init_db,
    insert_execution_outcome,
    insert_optimizer_decision,
    insert_payment_event,
    insert_webhook_recovery_outcome,
)
from app.economics import CandidateEvaluation
from app.executor import ExecutionOutcome
from app.main import app
from app.models import PaymentEvent
from app.optimizer_audit import OptimizerDecisionRecord
from app.recovery_intelligence import (
    INSUFFICIENT_OBSERVATIONS,
    MIN_OBSERVATIONS,
    NO_TERMINAL_OUTCOMES,
    POSITIVE_EVIDENCE_ONLY,
)

client = TestClient(app)

NOW = datetime(2026, 8, 30, 9, 0, tzinfo=timezone.utc)


def _event(event_id: str, **overrides) -> PaymentEvent:
    data = {
        "event_id": event_id,
        "order_id": f"order_{event_id}",
        "payment_id": f"pay_{event_id}",
        "customer_id": f"cust_{event_id}",
        "amount_paise": 100_000,
        "currency": "INR",
        "payment_method": "upi",
        "failure_reason": "bank_timeout",
        "bank": "HDFC",
        "risk_flag": "normal",
        "customer_history": {
            "prior_successful_payments": 2,
            "prior_failed_payments": 1,
            "has_active_subscription": True,
        },
        "timestamp": NOW.isoformat(),
    }
    data.update(overrides)
    return PaymentEvent.from_dict(data)


def _seed_recovered_event(conn, index: int, *, recovered: bool = True) -> None:
    event_id = f"evt_{index:03d}"
    insert_payment_event(conn, _event(event_id))
    insert_optimizer_decision(
        conn,
        OptimizerDecisionRecord(
            event_id=event_id,
            decided_at=(NOW - timedelta(minutes=5)).isoformat(),
            selected_intervention="payment_link",
            selection_reason="highest expected value",
            candidates_considered=("payment_link",),
            allowed_candidates=("payment_link",),
            evaluations=(
                CandidateEvaluation(
                    intervention="payment_link",
                    estimated_probability_bps=6_000,
                    amount_paise=100_000,
                    expected_recovered_value_paise=60_000,
                    intervention_cost_paise=100,
                    friction_cost_paise=100,
                    expected_value_paise=59_800,
                ),
            ),
        ),
    )
    insert_execution_outcome(
        conn,
        ExecutionOutcome(
            event_id=event_id,
            intervention="payment_link",
            execution_mode="REAL_RAZORPAY",
            status="SUCCESS",
            external_reference="https://rzp.io/i/x",
            detail=None,
            reported_at=NOW.isoformat(),
            payment_link_id=f"plink_{index:03d}",
        ),
    )
    if recovered:
        insert_webhook_recovery_outcome(
            conn,
            delivery_id=f"delivery_{index:03d}",
            payment_link_id=f"plink_{index:03d}",
            referenced_event_id=event_id,
            amount_paid_paise=100_000,
            currency="INR",
            payment_id=f"pay_v_{index:03d}",
            recovered_at=(NOW + timedelta(minutes=30)).isoformat(),
        )


def _db(monkeypatch, tmp_path, name: str):
    db_path = tmp_path / name
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{db_path}")
    conn = connect(str(db_path))
    init_db(conn)
    return conn


def test_empty_database_reports_insufficient_observations(monkeypatch, tmp_path):
    _db(monkeypatch, tmp_path, "empty.db").close()
    response = client.get("/recovery-intelligence")
    assert response.status_code == 200
    body = response.json()
    assert body["calibration"]["calibration_observations"] == 0
    assert body["calibration"]["verified_recoveries"] == 0
    assert body["calibration"]["status"] == INSUFFICIENT_OBSERVATIONS
    assert body["calibration"]["status_detail"] == NO_TERMINAL_OUTCOMES
    assert body["calibration"]["observed_recovery_rate_bps"] is None
    assert body["interventions"] == []
    assert body["segments"]["bank"] == []
    assert body["expected_vs_realized"]["compared_observations"] == 0


def test_verified_recoveries_are_reported_without_claiming_a_rate(
    monkeypatch, tmp_path
):
    """The audit case, end to end: 10 recoveries, 0 negatives, no 100%."""
    conn = _db(monkeypatch, tmp_path, "recovered.db")
    try:
        for index in range(MIN_OBSERVATIONS):
            _seed_recovered_event(conn, index)
    finally:
        conn.close()

    body = client.get("/recovery-intelligence").json()
    calibration = body["calibration"]
    assert calibration["verified_recoveries"] == MIN_OBSERVATIONS
    assert calibration["recovered_observations"] == MIN_OBSERVATIONS
    assert calibration["not_recovered_observations"] == 0
    assert calibration["has_terminal_negative_evidence"] is False
    assert calibration["sufficient_observations"] is False
    assert calibration["status"] == INSUFFICIENT_OBSERVATIONS
    assert calibration["status_detail"] == POSITIVE_EVIDENCE_ONLY
    assert calibration["observed_recovery_rate_bps"] is None
    assert calibration["calibration_gap_bps"] is None
    assert calibration["outcome_counts"]["RECOVERED"] == MIN_OBSERVATIONS
    assert calibration["outcome_counts"]["NOT_RECOVERED"] == 0

    # Positive evidence stays fully visible.
    interventions = body["interventions"]
    assert [row["key"] for row in interventions] == ["payment_link"]
    assert interventions[0]["verified_recoveries"] == MIN_OBSERVATIONS
    assert interventions[0]["average_recovered_amount_paise"] == 100_000
    assert interventions[0]["observed_recovery_rate_bps"] is None

    banks = body["segments"]["bank"]
    assert [row["key"] for row in banks] == ["HDFC"]
    assert banks[0]["verified_recoveries"] == MIN_OBSERVATIONS
    assert banks[0]["observed_recovery_rate_bps"] is None

    value = body["expected_vs_realized"]
    assert value["compared_observations"] == MIN_OBSERVATIONS
    assert value["expected_recovered_value_paise"] == 60_000 * MIN_OBSERVATIONS
    assert value["realized_recovered_amount_paise"] == 100_000 * MIN_OBSERVATIONS


def test_pending_links_are_reported_as_pending_not_as_failures(
    monkeypatch, tmp_path
):
    conn = _db(monkeypatch, tmp_path, "pending.db")
    try:
        for index in range(3):
            _seed_recovered_event(conn, index, recovered=False)
    finally:
        conn.close()

    body = client.get("/recovery-intelligence").json()
    assert body["calibration"]["calibration_observations"] == 0
    assert body["calibration"]["verified_recoveries"] == 0
    assert body["calibration"]["outcome_counts"]["PENDING"] == 3
    assert body["calibration"]["outcome_counts"]["NOT_RECOVERED"] == 0
    assert body["calibration"]["status"] == INSUFFICIENT_OBSERVATIONS
    assert body["evidence"]["ineligible_reasons"]["awaiting_outcome"] == 3
    row = body["interventions"][0]
    assert row["attempts"] == 3
    assert row["observed_recovery_rate_bps"] is None


def test_observations_can_be_included_for_traceability(monkeypatch, tmp_path):
    conn = _db(monkeypatch, tmp_path, "trace.db")
    try:
        _seed_recovered_event(conn, 0)
    finally:
        conn.close()

    body = client.get(
        "/recovery-intelligence", params={"include_observations": "true"}
    ).json()
    observation = body["observations"][0]
    assert observation["event_id"] == "evt_000"
    assert observation["payment_link_id"] == "plink_000"
    assert observation["evidence_id"] == "delivery_000"
    assert observation["decided_at"] is not None
    assert observation["predicted_probability_bps"] == 6_000


def test_endpoint_is_deterministic(monkeypatch, tmp_path):
    conn = _db(monkeypatch, tmp_path, "deterministic.db")
    try:
        for index in range(MIN_OBSERVATIONS + 2):
            _seed_recovered_event(conn, index, recovered=index % 2 == 0)
    finally:
        conn.close()

    first = client.get("/recovery-intelligence").json()
    second = client.get("/recovery-intelligence").json()
    assert first == second


def test_methodology_names_the_authoritative_sources(monkeypatch, tmp_path):
    _db(monkeypatch, tmp_path, "methodology.db").close()
    body = client.get("/recovery-intelligence").json()
    assert body["methodology"] == {
        "prediction_source": "optimizer_decisions",
        "execution_source": "execution_outcomes",
        "recovery_source": "webhook_recovery_outcomes",
        "correlation_key": "payment_link_id",
        "calibration_denominator": "RECOVERED + NOT_RECOVERED",
        "minimum_observations": MIN_OBSERVATIONS,
        "operational_world_only": True,
    }


def test_only_a_read_method_is_exposed(monkeypatch, tmp_path):
    _db(monkeypatch, tmp_path, "readonly.db").close()
    for method in ("post", "put", "patch", "delete"):
        response = getattr(client, method)("/recovery-intelligence")
        assert response.status_code == 405

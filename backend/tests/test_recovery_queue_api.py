"""Phase 21 API tests for GET /recovery/queue."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from fastapi.testclient import TestClient

from app.db import (
    connect,
    init_db,
    insert_classification_result,
    insert_execution_outcome,
    insert_payment_event,
    insert_policy_decision,
    insert_webhook_recovery_outcome,
)
from app.classification import ClassificationResult
from app.executor import ExecutionOutcome
from app.main import app
from app.models import PaymentEvent
from app.policy import PolicyDecision

client = TestClient(app)

NOW = datetime(2026, 8, 28, 12, 0, tzinfo=timezone.utc)


def _event(event_id: str, **overrides) -> PaymentEvent:
    data = {
        "event_id": event_id,
        "order_id": f"order_{event_id}",
        "payment_id": f"pay_{event_id}",
        "customer_id": f"cust_{event_id}",
        "amount_paise": 50_000,
        "currency": "INR",
        "payment_method": "card",
        "failure_reason": "bank_timeout",
        "bank": "HDFC",
        "risk_flag": "normal",
        "customer_history": {
            "prior_successful_payments": 3,
            "prior_failed_payments": 1,
            "has_active_subscription": True,
        },
        "timestamp": NOW.isoformat(),
    }
    data.update(overrides)
    return PaymentEvent.from_dict(data)


def _seed(monkeypatch, tmp_path) -> str:
    db_path = tmp_path / "queue_api.db"
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{db_path}")
    conn = connect(str(db_path))
    init_db(conn)
    try:
        insert_payment_event(conn, _event("evt_sim"))
        insert_payment_event(
            conn,
            _event(
                "evt_link",
                amount_paise=120_000,
                timestamp=(NOW - timedelta(hours=1)).isoformat(),
            ),
        )
        insert_payment_event(
            conn,
            _event(
                "evt_fraud",
                risk_flag="fraud_suspect",
                timestamp=(NOW - timedelta(hours=2)).isoformat(),
            ),
        )
        for event_id in ("evt_sim", "evt_link", "evt_fraud"):
            insert_classification_result(
                conn,
                ClassificationResult(
                    event_id=event_id,
                    root_cause_category="transient",
                    confidence=0.9,
                    reasoning="transient bank timeout",
                    candidate_interventions=("retry_delayed", "payment_link"),
                ),
            )
        insert_policy_decision(
            conn,
            PolicyDecision(
                event_id="evt_sim",
                proposed_intervention="retry_delayed",
                allowed=True,
                denial_reason=None,
                policy_rules_applied=("fraud_check_passed",),
                evaluated_at=NOW.isoformat(),
            ),
        )
        insert_execution_outcome(
            conn,
            ExecutionOutcome(
                event_id="evt_sim",
                intervention="retry_delayed",
                execution_mode="SIMULATED",
                status="SUCCESS",
                reported_at=NOW.isoformat(),
            ),
        )
        insert_execution_outcome(
            conn,
            ExecutionOutcome(
                event_id="evt_link",
                intervention="payment_link",
                execution_mode="REAL_RAZORPAY",
                status="SUCCESS",
                external_reference="https://rzp.io/l/abc",
                reported_at=NOW.isoformat(),
                payment_link_id="plink_api",
            ),
        )
        insert_policy_decision(
            conn,
            PolicyDecision(
                event_id="evt_fraud",
                proposed_intervention="retry_delayed",
                allowed=False,
                denial_reason="fraud_protection",
                policy_rules_applied=("fraud_protection",),
                evaluated_at=NOW.isoformat(),
            ),
        )
    finally:
        conn.close()
    return str(db_path)


def test_the_queue_endpoint_returns_the_projection(monkeypatch, tmp_path) -> None:
    _seed(monkeypatch, tmp_path)
    response = client.get("/recovery/queue")
    assert response.status_code == 200
    body = response.json()
    assert body["count"] == 3
    states = {row["event_id"]: row["lifecycle_state"] for row in body["rows"]}
    assert states == {
        "evt_sim": "EXECUTED",
        "evt_link": "PENDING_OUTCOME",
        "evt_fraud": "BLOCKED",
    }
    assert body["state_counts"]["PENDING_OUTCOME"] == 1


def test_the_queue_endpoint_distinguishes_real_from_simulated(monkeypatch, tmp_path) -> None:
    _seed(monkeypatch, tmp_path)
    rows = {row["event_id"]: row for row in client.get("/recovery/queue").json()["rows"]}
    assert rows["evt_sim"]["execution"]["execution_mode"] == "SIMULATED"
    assert rows["evt_sim"]["outcome"]["recovered_amount_paise"] is None
    assert rows["evt_link"]["execution"]["execution_mode"] == "REAL_RAZORPAY"
    assert rows["evt_link"]["outcome"]["state"] == "PENDING_OUTCOME"


def test_the_queue_endpoint_applies_filters(monkeypatch, tmp_path) -> None:
    _seed(monkeypatch, tmp_path)
    response = client.get("/recovery/queue", params={"lifecycle_state": "BLOCKED"})
    assert response.status_code == 200
    body = response.json()
    assert [row["event_id"] for row in body["rows"]] == ["evt_fraud"]
    assert body["rows"][0]["policy"]["denial_rule_label"] == "Fraud protection"


def test_the_queue_endpoint_applies_sorting(monkeypatch, tmp_path) -> None:
    _seed(monkeypatch, tmp_path)
    body = client.get("/recovery/queue", params={"sort": "amount_desc"}).json()
    assert [row["event_id"] for row in body["rows"]][0] == "evt_link"


def test_the_queue_endpoint_rejects_an_unknown_filter(monkeypatch, tmp_path) -> None:
    _seed(monkeypatch, tmp_path)
    response = client.get("/recovery/queue", params={"lifecycle_state": "WHATEVER"})
    assert response.status_code == 422
    assert response.json()["status"] == "invalid_request"


def test_the_queue_endpoint_rejects_an_unknown_sort(monkeypatch, tmp_path) -> None:
    _seed(monkeypatch, tmp_path)
    response = client.get("/recovery/queue", params={"sort": "by_vibes"})
    assert response.status_code == 422


def test_a_verified_recovery_appears_as_recovered(monkeypatch, tmp_path) -> None:
    db_path = _seed(monkeypatch, tmp_path)
    conn = connect(db_path)
    try:
        insert_webhook_recovery_outcome(
            conn,
            delivery_id="delivery_api",
            payment_link_id="plink_api",
            referenced_event_id="evt_link",
            amount_paid_paise=120_000,
            currency="INR",
            payment_id="pay_ok",
            recovered_at=(NOW + timedelta(minutes=10)).isoformat(),
        )
    finally:
        conn.close()
    rows = {row["event_id"]: row for row in client.get("/recovery/queue").json()["rows"]}
    assert rows["evt_link"]["lifecycle_state"] == "RECOVERED"
    assert rows["evt_link"]["outcome"]["recovered_amount_paise"] == 120_000


def test_an_empty_database_returns_an_empty_queue(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'empty.db'}")
    body = client.get("/recovery/queue").json()
    assert body["count"] == 0
    assert body["rows"] == []
    assert body["state_counts"]["RECOVERED"] == 0

"""Phase 6 API tests for the POST /events/{event_id}/policy endpoint."""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from app.classifier import OmniRouteError
from app.db import (
    connect,
    get_intervention_attempt,
    get_policy_decision,
    init_db,
    insert_intervention_attempt,
)
from app.main import app
from app.policy import InterventionAttempt
from app.routes.events import get_classifier

client = TestClient(app)

VALID_EVENT = {
    "event_id": "evt_policy_1",
    "order_id": "order_policy_1",
    "payment_id": "pay_policy_1",
    "customer_id": "cust_policy_1",
    "amount_paise": 75000,
    "currency": "INR",
    "payment_method": "card",
    "failure_reason": "bank_timeout",
    "bank": "HDFC",
    "risk_flag": "normal",
    "customer_history": {
        "prior_successful_payments": 4,
        "prior_failed_payments": 1,
        "has_active_subscription": True,
    },
    "timestamp": "2026-08-27T12:00:00+00:00",
}

VALID_RESULT = {
    "event_id": "evt_policy_1",
    "root_cause_category": "transient",
    "confidence": 0.9,
    "reasoning": "The payment gateway returned a transient timeout.",
    "candidate_interventions": ["retry_delayed", "payment_link"],
}

EVALUATION_TIME = "2026-08-27T13:00:00+00:00"


class StubClassifier:
    def __init__(self, responses: list[str]) -> None:
        self.responses = list(responses)

    def generate(self, prompt: str) -> str:
        if not self.responses:
            raise OmniRouteError("stub classifier exhausted")
        return self.responses.pop(0)


@pytest.fixture(autouse=True)
def _reset_dependency_overrides():
    yield
    app.dependency_overrides.clear()


def _set_test_db(monkeypatch, tmp_path) -> str:
    db_path = tmp_path / "policy_api.db"
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{db_path}")
    return str(db_path)


def _stub_classifier() -> None:
    app.dependency_overrides[get_classifier] = lambda: StubClassifier(
        [json.dumps(VALID_RESULT)]
    )


def _seed_event(monkeypatch, tmp_path, risk_flag: str = "normal") -> str:
    db_path = _set_test_db(monkeypatch, tmp_path)
    event_payload = VALID_EVENT if risk_flag == "normal" else dict(VALID_EVENT, risk_flag=risk_flag)
    response = client.post("/events", json=event_payload)
    assert response.status_code == 201
    return db_path


def _seed_event_and_classification(
    monkeypatch, tmp_path, risk_flag: str = "normal"
) -> str:
    db_path = _seed_event(monkeypatch, tmp_path, risk_flag=risk_flag)
    _stub_classifier()
    response = client.post("/events/evt_policy_1/classify")
    assert response.status_code == 200
    return db_path


def _policy_body(**overrides) -> dict:
    base = {
        "proposed_intervention": "retry_delayed",
        "evaluation_time": EVALUATION_TIME,
    }
    base.update(overrides)
    return base


def test_policy_allows_normal_event_and_persists_decision(
    monkeypatch, tmp_path
) -> None:
    db_path = _seed_event_and_classification(monkeypatch, tmp_path)
    response = client.post("/events/evt_policy_1/policy", json=_policy_body())
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "policy_success"
    decision = body["decision"]
    assert decision["event_id"] == "evt_policy_1"
    assert decision["proposed_intervention"] == "retry_delayed"
    assert decision["allowed"] is True
    assert decision["denial_reason"] is None
    assert decision["policy_rules_applied"] == [
        "fraud_check_passed",
        "terminal_check_passed",
        "duplicate_check_passed",
        "retry_limit_passed",
        "cooldown_check_passed",
        "spend_cap_passed",
    ]
    assert decision["evaluated_at"] == EVALUATION_TIME
    conn = connect(db_path)
    try:
        init_db(conn)
        persisted = get_policy_decision(
            conn, "evt_policy_1", "retry_delayed", EVALUATION_TIME
        )
        assert persisted is not None
        assert persisted.to_dict() == decision
    finally:
        conn.close()


def test_policy_denies_fraud_event(monkeypatch, tmp_path) -> None:
    _seed_event_and_classification(
        monkeypatch, tmp_path, risk_flag="fraud_suspect"
    )
    response = client.post(
        "/events/evt_policy_1/policy",
        json=_policy_body(proposed_intervention="payment_link"),
    )
    assert response.status_code == 200
    decision = response.json()["decision"]
    assert decision["allowed"] is False
    assert decision["denial_reason"] == "fraud_protection"


def test_policy_event_not_found(monkeypatch, tmp_path) -> None:
    _set_test_db(monkeypatch, tmp_path)
    response = client.post(
        "/events/evt_ghost/policy", json=_policy_body()
    )
    assert response.status_code == 404
    assert response.json()["status"] == "not_found"


def test_policy_requires_classification(monkeypatch, tmp_path) -> None:
    _seed_event(monkeypatch, tmp_path)
    response = client.post("/events/evt_policy_1/policy", json=_policy_body())
    assert response.status_code == 422
    assert response.json()["status"] == "no_classification"


def test_policy_requires_proposed_intervention(monkeypatch, tmp_path) -> None:
    _seed_event_and_classification(monkeypatch, tmp_path)
    response = client.post(
        "/events/evt_policy_1/policy",
        json={"evaluation_time": EVALUATION_TIME},
    )
    assert response.status_code == 422
    assert response.json()["status"] == "invalid_request"


def test_policy_rejects_naive_evaluation_time(monkeypatch, tmp_path) -> None:
    _seed_event_and_classification(monkeypatch, tmp_path)
    response = client.post(
        "/events/evt_policy_1/policy",
        json=_policy_body(evaluation_time="2026-08-27T13:00:00"),
    )
    assert response.status_code == 422
    assert response.json()["status"] == "policy_validation_failure"


def test_policy_rejects_unknown_intervention_explicitly(
    monkeypatch, tmp_path
) -> None:
    _seed_event_and_classification(monkeypatch, tmp_path)
    response = client.post(
        "/events/evt_policy_1/policy",
        json=_policy_body(proposed_intervention="wire_transfer"),
    )
    assert response.status_code == 422
    assert response.json()["status"] == "policy_validation_failure"


def test_policy_spend_cap_uses_configured_value(monkeypatch, tmp_path) -> None:
    db_path = _seed_event_and_classification(monkeypatch, tmp_path)
    conn = connect(db_path)
    try:
        init_db(conn)
        insert_intervention_attempt(
            conn,
            InterventionAttempt.from_dict(
                {
                    "event_id": "evt_other",
                    "intervention": "payment_link",
                    "customer_id": "cust_other",
                    "cost_paise": 100,
                    "attempted_at": "2026-08-27T12:00:00+00:00",
                    "status": "attempted",
                }
            ),
        )
    finally:
        conn.close()
    generous = client.post("/events/evt_policy_1/policy", json=_policy_body())
    assert generous.status_code == 200
    assert generous.json()["decision"]["allowed"] is True
    monkeypatch.setenv("POLICY_DAILY_SPEND_CAP_PAISE", "50")
    strict = client.post(
        "/events/evt_policy_1/policy",
        json=_policy_body(evaluation_time="2026-08-27T14:00:00+00:00"),
    )
    assert strict.status_code == 200
    assert strict.json()["decision"]["allowed"] is False
    assert strict.json()["decision"]["denial_reason"] == "spend_cap_exceeded"


def test_policy_duplicate_decision_is_rejected_consistently(
    monkeypatch, tmp_path
) -> None:
    _seed_event_and_classification(monkeypatch, tmp_path)
    first = client.post("/events/evt_policy_1/policy", json=_policy_body())
    assert first.status_code == 200
    second = client.post("/events/evt_policy_1/policy", json=_policy_body())
    assert second.status_code == 500
    assert second.json()["status"] == "policy_decision_persistence_failure"


def test_policy_defaults_evaluation_time_when_absent(monkeypatch, tmp_path) -> None:
    _seed_event_and_classification(monkeypatch, tmp_path)
    response = client.post(
        "/events/evt_policy_1/policy",
        json={"proposed_intervention": "retry_delayed"},
    )
    assert response.status_code == 200
    assert response.json()["status"] == "policy_success"


def test_intervention_attempt_persistence_via_api_seed(db_conn) -> None:
    attempt = InterventionAttempt.from_dict(
        {
            "event_id": "evt_seed",
            "intervention": "retry_immediate",
            "customer_id": "cust_seed",
            "cost_paise": 0,
            "attempted_at": "2026-08-27T12:00:00+00:00",
            "status": "attempted",
        }
    )
    insert_intervention_attempt(db_conn, attempt)
    retrieved = get_intervention_attempt(
        db_conn, "evt_seed", "retry_immediate", "2026-08-27T12:00:00+00:00"
    )
    assert retrieved == attempt

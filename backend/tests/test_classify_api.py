"""Phase 5 API tests for the POST /events/{event_id}/classify endpoint."""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from app.classification import ClassificationResult
from app.classifier import OmniRouteError
from app.db import connect, get_classification_result, init_db, insert_classification_result
from app.main import app
from app.routes.events import get_classifier

client = TestClient(app)

VALID_EVENT = {
    "event_id": "evt_api_1",
    "order_id": "order_api_1",
    "payment_id": "pay_api_1",
    "customer_id": "cust_api_1",
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
    "event_id": "evt_api_1",
    "root_cause_category": "transient",
    "confidence": 0.9,
    "reasoning": "The payment gateway returned a transient timeout.",
    "candidate_interventions": ["retry_delayed", "payment_link"],
}


class StubClassifier:
    """Return a fixed sequence of raw model outputs."""

    def __init__(self, *responses: str) -> None:
        self.responses = list(responses)
        self.calls = 0

    def generate(self, prompt: str) -> str:
        self.calls += 1
        if not self.responses:
            raise OmniRouteError("stub classifier exhausted")
        return self.responses.pop(0)


@pytest.fixture(autouse=True)
def _reset_dependency_overrides():
    yield
    app.dependency_overrides.clear()


def _set_test_db(monkeypatch, tmp_path) -> str:
    db_path = tmp_path / "classify_api.db"
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{db_path}")
    return str(db_path)


def _stub_classifier(*responses: str) -> None:
    app.dependency_overrides[get_classifier] = lambda: StubClassifier(*responses)


def _seed_event(monkeypatch, tmp_path) -> str:
    db_path = _set_test_db(monkeypatch, tmp_path)
    response = client.post("/events", json=VALID_EVENT)
    assert response.status_code == 201
    return db_path


def test_classify_success(monkeypatch, tmp_path) -> None:
    db_path = _seed_event(monkeypatch, tmp_path)
    _stub_classifier(json.dumps(VALID_RESULT))
    response = client.post("/events/evt_api_1/classify")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "classification_success"
    assert body["classification"]["root_cause_category"] == "transient"
    assert body["classification"]["candidate_interventions"] == [
        "retry_delayed",
        "payment_link",
    ]
    conn = connect(db_path)
    try:
        init_db(conn)
        persisted = get_classification_result(conn, "evt_api_1")
        assert persisted == ClassificationResult.from_dict(VALID_RESULT)
    finally:
        conn.close()


def test_classify_missing_event_is_not_found(monkeypatch, tmp_path) -> None:
    _set_test_db(monkeypatch, tmp_path)
    _stub_classifier(json.dumps(VALID_RESULT))
    response = client.post("/events/evt_ghost/classify")
    assert response.status_code == 404
    assert response.json()["status"] == "not_found"


def test_classify_llm_failure_is_explicit(monkeypatch, tmp_path) -> None:
    _seed_event(monkeypatch, tmp_path)
    _stub_classifier()
    response = client.post("/events/evt_api_1/classify")
    assert response.status_code == 502
    assert response.json()["status"] == "classification_llm_error"


def test_classify_validation_failure_is_explicit(monkeypatch, tmp_path) -> None:
    _seed_event(monkeypatch, tmp_path)
    bad = dict(VALID_RESULT, root_cause_category="unknown")
    _stub_classifier(json.dumps(bad), json.dumps(bad))
    response = client.post("/events/evt_api_1/classify")
    assert response.status_code == 502
    assert response.json()["status"] == "classification_validation_failure"


def test_classify_persistence_failure_is_explicit(monkeypatch, tmp_path) -> None:
    db_path = _seed_event(monkeypatch, tmp_path)
    conn = connect(db_path)
    try:
        init_db(conn)
        insert_classification_result(
            conn, ClassificationResult.from_dict(VALID_RESULT)
        )
    finally:
        conn.close()
    _stub_classifier(json.dumps(VALID_RESULT))
    response = client.post("/events/evt_api_1/classify")
    assert response.status_code == 500
    assert response.json()["status"] == "classification_persistence_failure"

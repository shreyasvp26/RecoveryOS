"""Phase 7 API tests for the POST /events/{event_id}/execute endpoint."""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

from app.classifier import OmniRouteError
from app.db import connect, init_db
from app.main import app
from app.razorpay_client import (
    PaymentLinkResult,
    RazorpayExecutionError,
)
from app.routes.events import get_classifier, get_now, get_razorpay_client

client = TestClient(app)

EVALUATION_TIME = datetime(2026, 8, 27, 13, 0, tzinfo=timezone.utc)

VALID_EVENT = {
    "event_id": "evt_exec_api",
    "order_id": "order_exec_api",
    "payment_id": "pay_exec_api",
    "customer_id": "cust_exec_api",
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


def _classification(candidates: list[str], root: str = "transient") -> dict:
    return {
        "event_id": "evt_exec_api",
        "root_cause_category": root,
        "confidence": 0.9,
        "reasoning": "transient bank timeout; retry or send a payment link.",
        "candidate_interventions": candidates,
    }


class StubClassifier:
    def __init__(self, responses: list[str]) -> None:
        self.responses = list(responses)

    def generate(self, prompt: str) -> str:
        if not self.responses:
            raise OmniRouteError("stub classifier exhausted")
        return self.responses.pop(0)


class StubPaymentLinkClient:
    def __init__(self, result: PaymentLinkResult | None = None, error: Exception | None = None):
        self.result = result
        self.error = error
        self.calls: list[dict] = []

    def create_payment_link(self, **kwargs) -> PaymentLinkResult:
        self.calls.append(kwargs)
        if self.error is not None:
            raise self.error
        if self.result is not None:
            return self.result
        raise AssertionError("stub client has neither result nor error")


@pytest.fixture(autouse=True)
def _reset_dependency_overrides():
    yield
    app.dependency_overrides.clear()


def _overrides(classification: dict, razorpay_client=None, risk_flag: str = "normal") -> None:
    app.dependency_overrides[get_classifier] = lambda: StubClassifier(
        [json.dumps(classification)]
    )
    app.dependency_overrides[get_now] = lambda: EVALUATION_TIME
    app.dependency_overrides[get_razorpay_client] = lambda: razorpay_client


def _set_test_db(monkeypatch, tmp_path) -> str:
    db_path = tmp_path / "exec_api.db"
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{db_path}")
    return str(db_path)


def _seed_event(monkeypatch, tmp_path, risk_flag: str = "normal") -> str:
    db_path = _set_test_db(monkeypatch, tmp_path)
    event_payload = VALID_EVENT if risk_flag == "normal" else dict(VALID_EVENT, risk_flag=risk_flag)
    response = client.post("/events", json=event_payload)
    assert response.status_code == 201
    return db_path


def _seed_classified(
    monkeypatch,
    tmp_path,
    candidates: list[str],
    root: str = "transient",
    risk_flag: str = "normal",
    razorpay_client=None,
) -> str:
    db_path = _seed_event(monkeypatch, tmp_path, risk_flag=risk_flag)
    _overrides(_classification(candidates, root=root), razorpay_client=razorpay_client)
    response = client.post("/events/evt_exec_api/classify")
    assert response.status_code == 200
    return db_path


def test_execute_simulated_retry_delayed(monkeypatch, tmp_path) -> None:
    db_path = _seed_classified(
        monkeypatch, tmp_path, candidates=["retry_delayed", "payment_link"]
    )
    response = client.post("/events/evt_exec_api/execute")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "execution_success"
    assert body["selected_intervention"] == "retry_delayed"
    assert body["policy_decision"]["allowed"] is True
    exec_outcome = body["execution"]
    assert exec_outcome["intervention"] == "retry_delayed"
    assert exec_outcome["execution_mode"] == "SIMULATED"
    assert exec_outcome["status"] == "SUCCESS"

    conn = connect(db_path)
    try:
        init_db(conn)
        row = conn.execute(
            "SELECT * FROM execution_outcomes WHERE event_id = ?",
            ("evt_exec_api",),
        ).fetchone()
        assert row is not None
        assert row["execution_mode"] == "SIMULATED"
        attempt = conn.execute(
            "SELECT * FROM intervention_attempts WHERE event_id = ?",
            ("evt_exec_api",),
        ).fetchone()
        assert attempt is not None
        assert attempt["status"] == "successful"
        decisions = conn.execute(
            "SELECT COUNT(*) FROM policy_decisions WHERE event_id = ?",
            ("evt_exec_api",),
        ).fetchone()[0]
        assert decisions == 2
    finally:
        conn.close()


def test_execute_payment_link_real_razorpay(monkeypatch, tmp_path) -> None:
    client_stub = StubPaymentLinkClient(
        result=PaymentLinkResult(id="plink_real", short_url="https://rzp.io/l/real123")
    )
    _seed_classified(
        monkeypatch,
        tmp_path,
        candidates=["payment_link", "reminder"],
        razorpay_client=client_stub,
    )
    response = client.post("/events/evt_exec_api/execute")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "execution_success"
    assert body["selected_intervention"] == "payment_link"
    exec_outcome = body["execution"]
    assert exec_outcome["execution_mode"] == "REAL_RAZORPAY"
    assert exec_outcome["status"] == "SUCCESS"
    assert exec_outcome["external_reference"] == "https://rzp.io/l/real123"
    assert client_stub.calls[0]["amount_paise"] == 75000
    assert client_stub.calls[0]["reference_id"] == "evtexecapi"


def test_execute_payment_link_configuration_missing(monkeypatch, tmp_path) -> None:
    for var in ("RAZORPAY_KEY_ID", "RAZORPAY_KEY_SECRET"):
        monkeypatch.delenv(var, raising=False)
    _seed_classified(
        monkeypatch, tmp_path, candidates=["payment_link"], razorpay_client=None
    )
    response = client.post("/events/evt_exec_api/execute")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "execution_failed"
    assert body["selected_intervention"] == "payment_link"
    exec_outcome = body["execution"]
    assert exec_outcome["execution_mode"] == "REAL_RAZORPAY"
    assert exec_outcome["status"] == "FAILED"
    assert "configuration_missing" in exec_outcome["detail"]
    assert exec_outcome["external_reference"] is None


def test_execute_payment_link_provider_failure(monkeypatch, tmp_path) -> None:
    client_stub = StubPaymentLinkClient(
        error=RazorpayExecutionError("razorpay_api_error: rejected by provider")
    )
    _seed_classified(
        monkeypatch,
        tmp_path,
        candidates=["payment_link"],
        razorpay_client=client_stub,
    )
    response = client.post("/events/evt_exec_api/execute")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "execution_failed"
    assert body["execution"]["status"] == "FAILED"
    assert "razorpay_api_error" in body["execution"]["detail"]
    assert body["execution"]["external_reference"] is None

    db_path = _set_test_db(monkeypatch, tmp_path)
    conn = connect(db_path)
    try:
        init_db(conn)
        attempt = conn.execute(
            "SELECT status FROM intervention_attempts WHERE event_id = ?",
            ("evt_exec_api",),
        ).fetchone()
        assert attempt["status"] == "failed"
    finally:
        conn.close()


def test_execute_all_denied_selects_no_action(monkeypatch, tmp_path) -> None:
    _seed_classified(
        monkeypatch,
        tmp_path,
        candidates=["retry_delayed", "payment_link"],
        root="transient",
        risk_flag="fraud_suspect",
    )
    response = client.post("/events/evt_exec_api/execute")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "no_action"
    assert body["selected_intervention"] == "no_action"
    assert "execution" not in body


def test_execute_missing_classification_never_executes(monkeypatch, tmp_path) -> None:
    _seed_event(monkeypatch, tmp_path)
    response = client.post("/events/evt_exec_api/execute")
    assert response.status_code == 422
    assert response.json()["status"] == "missing_classification"


def test_execute_event_not_found(monkeypatch, tmp_path) -> None:
    _set_test_db(monkeypatch, tmp_path)
    response = client.post("/events/evt_ghost/execute")
    assert response.status_code == 404
    assert response.json()["status"] == "not_found"


def test_execute_ignores_client_supplied_intervention(monkeypatch, tmp_path) -> None:
    """A client cannot force an arbitrary intervention into execution."""
    _seed_classified(
        monkeypatch, tmp_path, candidates=["reminder", "retry_immediate"]
    )
    response = client.post(
        "/events/evt_exec_api/execute",
        json={"intervention": "wire_transfer", "allowed": True},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["selected_intervention"] == "reminder"
    assert body["execution"]["intervention"] == "reminder"
    assert body["policy_decision"]["allowed"] is True

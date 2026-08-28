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
    RazorpayUnexpectedResponseError,
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
    assert exec_outcome["payment_link_id"] is None

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
    assert exec_outcome["payment_link_id"] == "plink_real"
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
    assert body["execution"]["payment_link_id"] is None

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


def test_execute_payment_link_malformed_provider_response_fails(monkeypatch, tmp_path) -> None:
    """A response that cannot yield an id/short_url must never be a success."""
    client_stub = StubPaymentLinkClient(
        error=RazorpayUnexpectedResponseError(
            "razorpay_api_unexpected_response: payment link id missing"
        )
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
    assert body["selected_intervention"] == "payment_link"
    exec_outcome = body["execution"]
    assert exec_outcome["execution_mode"] == "REAL_RAZORPAY"
    assert exec_outcome["status"] == "FAILED"
    assert "razorpay_api_unexpected_response" in exec_outcome["detail"]
    assert exec_outcome["external_reference"] is None
    assert exec_outcome["payment_link_id"] is None


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


def test_execute_live_razorpay_config_error_is_explicit(monkeypatch, tmp_path) -> None:
    """Present-but-invalid (live) Razorpay credentials must be an explicit,
    controllable configuration error at the dependency boundary — never an
    opaque 'Internal Server Error' and never a fake success.
    """
    _seed_classified(
        monkeypatch, tmp_path, candidates=["payment_link"], razorpay_client=None
    )
    # Force the REAL dependency path with a live key, bypassing the override.
    app.dependency_overrides.pop(get_razorpay_client, None)
    monkeypatch.setenv("RAZORPAY_KEY_ID", "rzp_live_abc123")
    monkeypatch.setenv("RAZORPAY_KEY_SECRET", "secret")
    response = client.post("/events/evt_exec_api/execute")
    assert response.status_code == 500
    body = response.json()
    assert "razorpay_configuration_error" in body["detail"]
    assert "rzp_live_" in body["detail"]


def _count(db_path: str, table: str, evid: str) -> int:
    conn = connect(db_path)
    try:
        init_db(conn)
        return conn.execute(
            f"SELECT COUNT(*) FROM {table} WHERE event_id = ?", (evid,)
        ).fetchone()[0]
    finally:
        conn.close()


def test_execute_cannot_duplicate_successful_execution(monkeypatch, tmp_path) -> None:
    """Phase 3: a previously SUCCESSFULLY executed intervention for an event
    can never execute again. The deterministic policy DUPLICATE rule must deny
    a re-execution even when the evaluation time moves well past the cooldown
    window, so no second execution outcome or intervention attempt is created.

    This actively attempts to violate the invariant via a later re-request.
    """
    db_path = _seed_classified(
        monkeypatch, tmp_path, candidates=["retry_delayed", "payment_link"]
    )

    first = client.post("/events/evt_exec_api/execute")
    assert first.status_code == 200
    assert first.json()["status"] == "execution_success"
    assert first.json()["selected_intervention"] == "retry_delayed"

    assert _count(db_path, "execution_outcomes", "evt_exec_api") == 1
    assert _count(db_path, "intervention_attempts", "evt_exec_api") == 1

    # Move the authoritative time 3 hours later (past cooldown + customer 24h).
    LATER = datetime(2026, 8, 27, 16, 0, tzinfo=timezone.utc)
    app.dependency_overrides[get_now] = lambda: LATER

    second = client.post("/events/evt_exec_api/execute")
    assert second.status_code == 200
    body = second.json()
    # The duplicate-successful-intervention rule denies every candidate, so
    # selection falls through to the explicit, non-executable no_action.
    assert body["status"] == "no_action"
    assert body["selected_intervention"] == "no_action"
    assert "execution" not in body

    # No second outcome or attempt may have been created by the re-request.
    assert _count(db_path, "execution_outcomes", "evt_exec_api") == 1
    assert _count(db_path, "intervention_attempts", "evt_exec_api") == 1


def test_execute_repeated_requests_never_execute_twice(monkeypatch, tmp_path) -> None:
    """Hammering /execute for the same event must not produce a second real
    execution: the outcome/attempt row count stays at one and every later call
    degrades to an explicit no_action."""
    db_path = _seed_classified(
        monkeypatch, tmp_path, candidates=["reminder", "retry_immediate"]
    )
    first = client.post("/events/evt_exec_api/execute")
    assert first.json()["status"] == "execution_success"
    assert first.json()["selected_intervention"] == "reminder"

    for i in range(5):
        LATER = datetime(2026, 8, 27, 13, i + 1, tzinfo=timezone.utc)
        app.dependency_overrides[get_now] = lambda: LATER
        resp = client.post("/events/evt_exec_api/execute")
        assert resp.status_code == 200
        assert resp.json()["status"] == "no_action"

    assert _count(db_path, "execution_outcomes", "evt_exec_api") == 1
    assert _count(db_path, "intervention_attempts", "evt_exec_api") == 1


def test_execute_retry_after_success_is_denied_not_re_executed(
    monkeypatch, tmp_path
) -> None:
    """Repeating execute after a success must deny (not re-run) with the SAME
    candidate set and a later evaluation time — the duplicate-successful rule is
    the guard, independent of the candidate list on the event."""
    db_path = _seed_classified(
        monkeypatch, tmp_path, candidates=["retry_delayed", "payment_link"]
    )
    first = client.post("/events/evt_exec_api/execute")
    assert first.json()["status"] == "execution_success"
    assert first.json()["selected_intervention"] == "retry_delayed"

    LATER = datetime(2026, 8, 27, 20, 0, tzinfo=timezone.utc)
    app.dependency_overrides[get_now] = lambda: LATER

    re_exec = client.post("/events/evt_exec_api/execute")
    assert re_exec.status_code == 200
    assert re_exec.json()["status"] == "no_action"
    assert _count(db_path, "execution_outcomes", "evt_exec_api") == 1
    assert _count(db_path, "intervention_attempts", "evt_exec_api") == 1

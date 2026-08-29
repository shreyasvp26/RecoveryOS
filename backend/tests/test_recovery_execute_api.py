"""Phase 21 API tests for POST /recovery/{event_id}/execute.

The operator entry point must add convenience and no authority. These tests
attack it from the client side: forged authorization, a chosen intervention, a
chosen evaluation time, and repeated/duplicate requests. Every policy
protection must survive, and execution must never be reported as recovery.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from app.classifier import OmniRouteError
from app.db import connect, init_db
from app.main import app
from app.razorpay_client import PaymentLinkResult, RazorpayExecutionError
from app.routes.events import get_classifier, get_now, get_razorpay_client

client = TestClient(app)

NOW = datetime(2026, 8, 28, 10, 0, tzinfo=timezone.utc)

EVENT = {
    "event_id": "evt_ops",
    "order_id": "order_ops",
    "payment_id": "pay_ops",
    "customer_id": "cust_ops",
    "amount_paise": 85_000,
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
    "timestamp": "2026-08-28T09:00:00+00:00",
}


class StubClassifier:
    def __init__(self, responses: list[str]) -> None:
        self.responses = list(responses)

    def generate(self, prompt: str) -> str:
        if not self.responses:
            raise OmniRouteError("stub classifier exhausted")
        return self.responses.pop(0)


class StubPaymentLinkClient:
    def __init__(self, result=None, error=None):
        self.result = result
        self.error = error
        self.calls: list[dict] = []

    def create_payment_link(self, **kwargs) -> PaymentLinkResult:
        self.calls.append(kwargs)
        if self.error is not None:
            raise self.error
        return self.result


@pytest.fixture(autouse=True)
def _reset_overrides():
    yield
    app.dependency_overrides.clear()


def _classification(candidates: list[str], root: str = "transient") -> dict:
    return {
        "event_id": "evt_ops",
        "root_cause_category": root,
        "confidence": 0.9,
        "reasoning": "transient bank timeout; retry or send a payment link.",
        "candidate_interventions": candidates,
    }


def _seed(
    monkeypatch,
    tmp_path,
    candidates: list[str],
    root: str = "transient",
    risk_flag: str = "normal",
    razorpay_client=None,
) -> str:
    db_path = tmp_path / "ops_exec.db"
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{db_path}")
    payload = EVENT if risk_flag == "normal" else dict(EVENT, risk_flag=risk_flag)
    assert client.post("/events", json=payload).status_code == 201
    app.dependency_overrides[get_classifier] = lambda: StubClassifier(
        [json.dumps(_classification(candidates, root=root))]
    )
    app.dependency_overrides[get_now] = lambda: NOW
    app.dependency_overrides[get_razorpay_client] = lambda: razorpay_client
    assert client.post("/events/evt_ops/classify").status_code == 200
    return str(db_path)


def _count(db_path: str, table: str) -> int:
    conn = connect(db_path)
    try:
        init_db(conn)
        return conn.execute(
            f"SELECT COUNT(*) FROM {table} WHERE event_id = ?", ("evt_ops",)
        ).fetchone()[0]
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# The happy paths, and what they are allowed to claim
# ---------------------------------------------------------------------------


def test_an_allowed_event_executes_and_returns_its_row(monkeypatch, tmp_path) -> None:
    _seed(monkeypatch, tmp_path, candidates=["retry_delayed", "payment_link"])
    response = client.post("/recovery/evt_ops/execute")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "execution_success"
    assert body["selected_intervention"] == "retry_delayed"
    assert body["execution"]["execution_mode"] == "SIMULATED"
    assert body["row"]["lifecycle_state"] == "EXECUTED"


def test_a_simulated_execution_never_reports_recovery(monkeypatch, tmp_path) -> None:
    _seed(monkeypatch, tmp_path, candidates=["reminder"])
    body = client.post("/recovery/evt_ops/execute").json()
    assert body["row"]["lifecycle_state"] == "EXECUTED"
    assert body["row"]["outcome"]["recovered_amount_paise"] is None
    assert body["row"]["outcome"]["state"] != "RECOVERED"


def test_a_real_payment_link_is_pending_outcome_not_recovered(monkeypatch, tmp_path) -> None:
    """The central outcome rule: a created link is waiting, not recovered."""
    provider = StubPaymentLinkClient(
        result=PaymentLinkResult(id="plink_ops", short_url="https://rzp.io/l/ops")
    )
    _seed(monkeypatch, tmp_path, candidates=["payment_link"], razorpay_client=provider)
    body = client.post("/recovery/evt_ops/execute").json()
    assert body["status"] == "execution_success"
    assert body["execution"]["execution_mode"] == "REAL_RAZORPAY"
    assert body["execution"]["payment_link_id"] == "plink_ops"
    assert body["row"]["lifecycle_state"] == "PENDING_OUTCOME"
    assert body["row"]["outcome"]["recovered_amount_paise"] is None
    assert len(provider.calls) == 1


def test_a_provider_failure_is_reported_honestly(monkeypatch, tmp_path) -> None:
    provider = StubPaymentLinkClient(
        error=RazorpayExecutionError("razorpay_api_error: rejected by provider")
    )
    _seed(monkeypatch, tmp_path, candidates=["payment_link"], razorpay_client=provider)
    body = client.post("/recovery/evt_ops/execute").json()
    assert body["status"] == "execution_failed"
    assert body["execution"]["status"] == "FAILED"
    assert body["row"]["lifecycle_state"] == "FAILED"
    assert body["row"]["outcome"]["recovered_amount_paise"] is None


# ---------------------------------------------------------------------------
# The client has no authority
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "forged",
    [
        {"allowed": True},
        {"intervention": "payment_link"},
        {"selected_intervention": "payment_link"},
        {"policy_decision": {"allowed": True, "denial_reason": None}},
        {"authorization": "operator"},
        {"evaluation_time": "2030-01-01T00:00:00+00:00"},
        {"execution_mode": "REAL_RAZORPAY"},
        {"force": True},
    ],
)
def test_a_client_cannot_supply_authority(monkeypatch, tmp_path, forged) -> None:
    db_path = _seed(monkeypatch, tmp_path, candidates=["retry_delayed"])
    response = client.post("/recovery/evt_ops/execute", json=forged)
    assert response.status_code == 422
    assert response.json()["status"] == "client_authority_rejected"
    assert _count(db_path, "execution_outcomes") == 0
    assert _count(db_path, "intervention_attempts") == 0


def test_a_forged_authorization_cannot_execute_a_blocked_event(monkeypatch, tmp_path) -> None:
    db_path = _seed(
        monkeypatch,
        tmp_path,
        candidates=["retry_delayed", "payment_link"],
        risk_flag="fraud_suspect",
    )
    response = client.post(
        "/recovery/evt_ops/execute", json={"allowed": True, "intervention": "payment_link"}
    )
    assert response.status_code == 422
    assert _count(db_path, "execution_outcomes") == 0


def test_a_client_cannot_choose_an_unauthorized_intervention(monkeypatch, tmp_path) -> None:
    """Even the shape of a request that names an intervention is refused."""
    provider = StubPaymentLinkClient(
        result=PaymentLinkResult(id="plink_never", short_url="https://rzp.io/l/never")
    )
    _seed(
        monkeypatch,
        tmp_path,
        candidates=["reminder", "retry_immediate"],
        razorpay_client=provider,
    )
    rejected = client.post("/recovery/evt_ops/execute", json={"intervention": "payment_link"})
    assert rejected.status_code == 422
    assert provider.calls == []

    # Without the forged field the server picks from its own authorized set,
    # which never contained payment_link for this event.
    allowed = client.post("/recovery/evt_ops/execute").json()
    assert allowed["selected_intervention"] in ("reminder", "retry_immediate")
    assert provider.calls == []


def test_an_empty_body_is_accepted(monkeypatch, tmp_path) -> None:
    _seed(monkeypatch, tmp_path, candidates=["reminder"])
    assert client.post("/recovery/evt_ops/execute", json={}).status_code == 200


# ---------------------------------------------------------------------------
# Policy protections survive the new entry point
# ---------------------------------------------------------------------------


def test_a_fraud_event_is_never_executed(monkeypatch, tmp_path) -> None:
    db_path = _seed(
        monkeypatch,
        tmp_path,
        candidates=["retry_delayed", "payment_link"],
        risk_flag="fraud_suspect",
    )
    body = client.post("/recovery/evt_ops/execute").json()
    assert body["status"] == "no_action"
    assert body["row"]["lifecycle_state"] == "BLOCKED"
    assert body["row"]["policy"]["denial_reason"] == "fraud_protection"
    assert _count(db_path, "execution_outcomes") == 0


def test_a_terminal_failure_is_never_executed(monkeypatch, tmp_path) -> None:
    db_path = _seed(
        monkeypatch, tmp_path, candidates=["retry_delayed"], root="terminal"
    )
    body = client.post("/recovery/evt_ops/execute").json()
    assert body["status"] == "no_action"
    assert body["row"]["policy"]["denial_reason"] == "terminal_failure"
    assert _count(db_path, "execution_outcomes") == 0


def test_the_cooldown_cannot_be_bypassed_by_the_operator(monkeypatch, tmp_path) -> None:
    db_path = _seed(monkeypatch, tmp_path, candidates=["retry_delayed", "reminder"])
    assert client.post("/recovery/evt_ops/execute").json()["status"] == "execution_success"

    # Ten minutes later, inside the 30-minute cooldown.
    app.dependency_overrides[get_now] = lambda: NOW + timedelta(minutes=10)
    second = client.post("/recovery/evt_ops/execute").json()
    assert second["status"] == "no_action"
    assert _count(db_path, "execution_outcomes") == 1


def test_the_duplicate_rule_cannot_be_bypassed_by_the_operator(monkeypatch, tmp_path) -> None:
    db_path = _seed(monkeypatch, tmp_path, candidates=["retry_delayed", "reminder"])
    assert client.post("/recovery/evt_ops/execute").json()["status"] == "execution_success"

    # Well past every cooldown: only the duplicate-successful rule can hold.
    app.dependency_overrides[get_now] = lambda: NOW + timedelta(days=2)
    second = client.post("/recovery/evt_ops/execute").json()
    assert second["status"] == "no_action"
    assert second["row"]["policy"]["denial_reason"] == "duplicate_intervention"
    assert _count(db_path, "execution_outcomes") == 1


def test_a_double_click_executes_once(monkeypatch, tmp_path) -> None:
    """Two identical requests at the same instant: one execution, total."""
    db_path = _seed(monkeypatch, tmp_path, candidates=["reminder"])
    first = client.post("/recovery/evt_ops/execute")
    second = client.post("/recovery/evt_ops/execute")
    assert first.json()["status"] == "execution_success"
    assert second.status_code in (200, 409)
    assert second.json()["status"] != "execution_success"
    assert _count(db_path, "execution_outcomes") == 1
    assert _count(db_path, "intervention_attempts") == 1


def test_an_http_retry_never_creates_a_second_payment_link(monkeypatch, tmp_path) -> None:
    provider = StubPaymentLinkClient(
        result=PaymentLinkResult(id="plink_once", short_url="https://rzp.io/l/once")
    )
    _seed(monkeypatch, tmp_path, candidates=["payment_link"], razorpay_client=provider)
    for _ in range(4):
        client.post("/recovery/evt_ops/execute")
    assert len(provider.calls) == 1


# ---------------------------------------------------------------------------
# Absent and unknown events
# ---------------------------------------------------------------------------


def test_an_unknown_event_is_not_found(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'empty.db'}")
    app.dependency_overrides[get_razorpay_client] = lambda: None
    response = client.post("/recovery/evt_ghost/execute")
    assert response.status_code == 404


def test_an_unclassified_event_never_executes(monkeypatch, tmp_path) -> None:
    db_path = tmp_path / "unclassified.db"
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{db_path}")
    app.dependency_overrides[get_razorpay_client] = lambda: None
    app.dependency_overrides[get_now] = lambda: NOW
    assert client.post("/events", json=EVENT).status_code == 201
    response = client.post("/recovery/evt_ops/execute")
    assert response.status_code == 422
    assert response.json()["status"] == "missing_classification"
    assert _count(str(db_path), "execution_outcomes") == 0

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

from app import calibration_service
from app.calibration import OUTCOME_NOT_RECOVERED
from app.classifier import OmniRouteError
from app.db import (
    connect,
    get_optimizer_decisions_for_event,
    init_db,
    insert_execution_outcome,
    insert_optimizer_decision,
    insert_provider_payment_link_outcome,
    insert_webhook_recovery_outcome,
)
from app.executor import PAYMENT_LINK, ExecutionOutcome
from app.main import app
from app.optimizer_audit import OptimizerDecisionRecord
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


def _calibration_part(
    conn, *, intervention: str, link_id: str, recovered: bool
) -> None:
    """One terminal provider observation that feeds a calibration snapshot."""
    insert_execution_outcome(
        conn,
        ExecutionOutcome(
            event_id=f"cal_{link_id}",
            intervention=intervention,
            execution_mode="REAL_RAZORPAY",
            status="SUCCESS",
            external_reference=f"https://rzp.io/rzp/{link_id}",
            reported_at="2026-01-01T00:00:00+00:00",
            payment_link_id=link_id,
        ),
    )
    insert_optimizer_decision(
        conn,
        OptimizerDecisionRecord(
            event_id=f"cal_{link_id}",
            decided_at="2026-01-01T00:00:00+00:00",
            selected_intervention=intervention,
            selection_reason="max_expected_value",
            candidates_considered=(intervention,),
            allowed_candidates=(intervention,),
            evaluations=(),
        ),
    )
    if recovered:
        insert_webhook_recovery_outcome(
            conn,
            delivery_id=f"del_{link_id}",
            payment_link_id=link_id,
            referenced_event_id=f"cal_{link_id}",
            amount_paid_paise=10_000,
            currency="INR",
            payment_id=f"pay_{link_id}",
            recovered_at="2026-01-02T00:00:00+00:00",
        )


def _activate_calibration(db_path: str, *, intervention: str) -> int:
    """Provide gated recovery evidence and build an immutable active snapshot.

    The snapshot is built on the SAME database the recovery endpoint reads (via
    DATABASE_URL), so ``build_production_estimator`` observes it on the request
    path. Returns the snapshot version.
    """
    conn = connect(db_path)
    try:
        init_db(conn)
        for i in range(6):
            _calibration_part(conn, intervention=intervention, link_id=f"r{i}", recovered=True)
        for i in range(4):
            _calibration_part(conn, intervention=intervention, link_id=f"e{i}", recovered=False)
            insert_provider_payment_link_outcome(
                conn,
                payment_link_id=f"e{i}",
                event_id=f"cal_e{i}",
                status="expired",
                outcome=OUTCOME_NOT_RECOVERED,
                observed_at="2026-01-03T00:00:00+00:00",
            )
        built = calibration_service.build_calibration_snapshot(conn, None)
        return built["version"]
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


# ---------------------------------------------------------------------------
# Phase 23 estimator wiring through the operator entry point
# ---------------------------------------------------------------------------


def _latest_optimizer_decision(db_path: str) -> dict:
    conn = connect(db_path)
    try:
        init_db(conn)
        decisions = get_optimizer_decisions_for_event(conn, "evt_ops")
        assert decisions, "the recovery path must persist an economic decision"
        return decisions[-1]
    finally:
        conn.close()


def test_recovery_execute_records_baseline_without_calibration(
    monkeypatch, tmp_path
) -> None:
    """No snapshot yet: the operator path stays on the frozen baseline.

    The persisted decision must be honest about ranking on the baseline, never
    fabricated as calibrated just because the production estimator is wired in.
    """
    db_path = _seed(monkeypatch, tmp_path, candidates=["retry_delayed", "payment_link"])
    response = client.post("/recovery/evt_ops/execute")
    assert response.status_code == 200
    assert response.json()["status"] == "execution_success"

    stage = _latest_optimizer_decision(db_path)
    assert stage["estimator_mode"] == "BASELINE"
    assert stage["estimator_version"] is None
    assert stage["estimator_reason"] == "no_calibration_evidence"


def test_recovery_execute_uses_the_active_calibration_snapshot(
    monkeypatch, tmp_path
) -> None:
    """The operator execute path consumes the production adaptive estimator.

    Regression: the endpoint previously called ``execute_event`` without the
    estimator, so even with an active snapshot it silently ranked with the
    frozen baseline and recorded no_calibration_evidence. With the wiring in
    place it ranks with the gated posterior and records CALIBRATED.
    """
    provider = StubPaymentLinkClient(
        result=PaymentLinkResult(id="plink_cal", short_url="https://rzp.io/l/cal")
    )
    db_path = _seed(
        monkeypatch, tmp_path, candidates=["payment_link"], razorpay_client=provider
    )
    version = _activate_calibration(db_path, intervention=PAYMENT_LINK)
    assert version >= 1

    response = client.post("/recovery/evt_ops/execute")
    assert response.status_code == 200
    assert response.json()["status"] == "execution_success"
    assert response.json()["execution"]["execution_mode"] == "REAL_RAZORPAY"

    stage = _latest_optimizer_decision(db_path)
    assert stage["estimator_mode"] == "CALIBRATED"
    assert stage["estimator_version"] == version
    assert stage["estimator_reason"] == "active_calibration"


def _corrupt_snapshot(db_path: str) -> None:
    """Persist an unreadable snapshot row, driving calibration_unavailable.

    A snapshot row whose ``active_bps_json`` is not valid JSON makes
    ``load_active_snapshot`` raise inside ``build_production_estimator``, which
    (already owning fallback semantics) returns the unavailable wrapper rather
    than raising. This is the legitimate "calibration cannot be read" state, not
    a route-level exception.
    """
    conn = connect(db_path)
    try:
        init_db(conn)
        conn.execute(
            "INSERT INTO estimator_calibration_snapshots "
            "(version, built_at, active_bps_json, evidenced_json) "
            "VALUES (?, ?, ?, ?)",
            (1, "2026-01-01T00:00:00+00:00", "NOT_JSON", "{}"),
        )
        conn.commit()
    finally:
        conn.close()


def test_recovery_execute_preserves_calibration_unavailable(
    monkeypatch, tmp_path
) -> None:
    """A calibration-unavailable snapshot stays CALIBRATION_UNAVAILABLE here.

    The recovery route must not collapse a calibration that exists but cannot
    be read into ``no_calibration_evidence``. The calibration service owns the
    fallback and reports ``calibration_unavailable``; the route must carry that
    provenance through to the persisted decision unchanged.
    """
    db_path = _seed(monkeypatch, tmp_path, candidates=["retry_delayed", "payment_link"])
    _corrupt_snapshot(db_path)

    response = client.post("/recovery/evt_ops/execute")
    assert response.status_code == 200
    assert response.json()["status"] == "execution_success"

    stage = _latest_optimizer_decision(db_path)
    assert stage["estimator_mode"] == "BASELINE"
    assert stage["estimator_version"] is None
    assert stage["estimator_reason"] == "calibration_unavailable"

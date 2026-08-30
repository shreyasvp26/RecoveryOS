"""Phase 24 end-to-end integration tests.

One canonical golden path plus the operator/API-level safety scenarios that were
only covered at the service boundary before this phase.

SCENARIO A (golden path) — a single database and a single event walk the full
loop through HTTP only:

    ingest  -> classify -> policy gate -> economic decision (estimator
    provenance recorded) -> REAL_RAZORPAY execution (link created) -> the
    event is PENDING_OUTCOME / ``waiting`` (created is never recovery) ->
    a verified Razorpay webhook on the same event -> trace reports
    ``recovered`` with the trusted amount -> the recovery is observable
    feedback (recovery intelligence) on the same database.

The remaining tests close API-level coverage gaps proven to exist in the
audit: the customer retry limit and the spend cap were only enforced through
``execute_event`` at the service layer (not over the HTTP boundary), the
calibrated future decision never proved a rank change or historical
immutability, and ``threshold_not_met`` was only asserted at the unit level.
"""

from __future__ import annotations

import hashlib
import hmac
import json
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from app import calibration_service
from app.calibration import (
    OUTCOME_NOT_RECOVERED,
    PRIOR_STRENGTH,
    PROBABILITY_SCALE,
)
from app.classifier import OmniRouteError
from app.db import (
    connect,
    get_optimizer_decisions_for_event,
    init_db,
    insert_execution_outcome,
    insert_intervention_attempt,
    insert_optimizer_decision,
    insert_provider_payment_link_outcome,
    insert_webhook_recovery_outcome,
)
from app.executor import PAYMENT_LINK, ExecutionOutcome
from app.main import app
from app.optimizer_audit import OptimizerDecisionRecord
from app.policy import InterventionAttempt
from app.razorpay_client import PaymentLinkResult
from app.routes.events import get_classifier, get_now, get_razorpay_client

client = TestClient(app)

NOW = datetime(2026, 8, 28, 10, 0, tzinfo=timezone.utc)

TEST_WEBHOOK_SECRET = "test-webhook-secret"

EVENT = {
    "event_id": "evt_gold",
    "order_id": "order_gold",
    "payment_id": "pay_gold",
    "customer_id": "cust_gold",
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


def _event(
    event_id: str,
    *,
    customer_id: str = "cust_gold",
    risk_flag: str = "normal",
) -> dict:
    ev = dict(EVENT)
    ev["event_id"] = event_id
    ev["order_id"] = f"order_{event_id}"
    ev["payment_id"] = f"pay_{event_id}"
    ev["customer_id"] = customer_id
    ev["risk_flag"] = risk_flag
    return ev


def _classification(event_id: str, candidates: list[str], root: str = "transient") -> str:
    return json.dumps(
        {
            "event_id": event_id,
            "root_cause_category": root,
            "confidence": 0.9,
            "reasoning": "Phase 24 deterministic classification.",
            "candidate_interventions": candidates,
        }
    )


def _open_db(monkeypatch, tmp_path, name: str) -> str:
    db_path = tmp_path / name
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{db_path}")
    return str(db_path)


def _ingest(
    event_id: str,
    candidates: list[str],
    *,
    risk_flag: str = "normal",
    root: str = "transient",
    customer_id: str = "cust_gold",
) -> None:
    assert client.post("/events", json=_event(event_id, risk_flag=risk_flag, customer_id=customer_id)).status_code == 201
    app.dependency_overrides[get_classifier] = lambda: StubClassifier(
        [_classification(event_id, candidates, root=root)]
    )
    assert client.post(f"/events/{event_id}/classify").status_code == 200


def _seed(
    monkeypatch,
    tmp_path,
    event_id: str,
    candidates: list[str],
    *,
    risk_flag: str = "normal",
    razorpay_client=None,
) -> str:
    db_path = _open_db(monkeypatch, tmp_path, f"{event_id}.db")
    _ingest(event_id, candidates, risk_flag=risk_flag)
    app.dependency_overrides[get_now] = lambda: NOW
    app.dependency_overrides[get_razorpay_client] = lambda: razorpay_client
    return db_path


def _count(db_path: str, table: str, event_id: str) -> int:
    conn = connect(db_path)
    try:
        init_db(conn)
        return conn.execute(
            f"SELECT COUNT(*) FROM {table} WHERE event_id = ?", (event_id,)
        ).fetchone()[0]
    finally:
        conn.close()


def _attempt(
    conn,
    *,
    event_id: str,
    customer_id: str,
    intervention: str = "retry_delayed",
    cost_paise: int = 0,
    status: str = "successful",
    when: datetime | None = None,
) -> None:
    insert_intervention_attempt(
        conn,
        InterventionAttempt(
            event_id=event_id,
            intervention=intervention,
            customer_id=customer_id,
            cost_paise=cost_paise,
            attempted_at=(when or NOW).isoformat(),
            status=status,
        ),
    )


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
    else:
        insert_provider_payment_link_outcome(
            conn,
            payment_link_id=link_id,
            event_id=f"cal_{link_id}",
            status="expired",
            outcome=OUTCOME_NOT_RECOVERED,
            observed_at="2026-01-03T00:00:00+00:00",
        )


def _activate_calibration(
    db_path: str, *, recovered: int = 6, not_recovered: int = 4
) -> dict:
    """Provide gated recovery evidence and build an immutable active snapshot.

    Built on the SAME database the execute endpoints read (DATABASE_URL), so
    ``build_production_estimator`` observes it on the request path. Returns the
    built snapshot so the caller can assert its persisted posterior directly.
    """
    conn = connect(db_path)
    try:
        init_db(conn)
        for i in range(recovered):
            _calibration_part(conn, intervention=PAYMENT_LINK, link_id=f"r{i}", recovered=True)
        for i in range(not_recovered):
            _calibration_part(conn, intervention=PAYMENT_LINK, link_id=f"e{i}", recovered=False)
        return calibration_service.build_calibration_snapshot(conn, None)
    finally:
        conn.close()


def _latest_optimizer_decision(db_path: str, event_id: str) -> dict:
    conn = connect(db_path)
    try:
        init_db(conn)
        decisions = get_optimizer_decisions_for_event(conn, event_id)
        assert decisions, "the execute path must persist an economic decision"
        return decisions[-1]
    finally:
        conn.close()


def _evaluation_probability(decision: dict, intervention: str) -> int:
    for evaluation in decision["evaluations"]:
        if evaluation["intervention"] == intervention:
            return evaluation["estimated_probability_bps"]
    raise AssertionError(f"no evaluation recorded for {intervention}")


def _paid_event_bytes(link_id: str, payment_id: str, amount_paise: int) -> bytes:
    payload = {
        "entity": "event",
        "account_id": "acc_test",
        "event": "payment_link.paid",
        "contains": ["payment_link", "order", "payment"],
        "payload": {
            "payment_link": {
                "entity": {
                    "id": link_id,
                    "status": "paid",
                    "amount": amount_paise,
                    "amount_paid": amount_paise,
                    "currency": "INR",
                    "short_url": "https://rzp.io/rzp/abc",
                }
            },
            "payment": {"entity": {"id": payment_id, "status": "captured"}},
            "order": {"entity": {"id": f"order_{link_id}", "amount_paid": amount_paise}},
        },
    }
    return json.dumps(payload).encode("utf-8")


def _webhook_headers(raw: bytes, delivery_id: str) -> dict:
    signature = hmac.new(
        TEST_WEBHOOK_SECRET.encode("utf-8"), raw, hashlib.sha256
    ).hexdigest()
    return {
        "X-Razorpay-Signature": signature,
        "X-Razorpay-Event-Id": delivery_id,
    }


# ---------------------------------------------------------------------------
# SCENARIO A — the canonical golden path, one database, one event, HTTP only
# ---------------------------------------------------------------------------


def test_a_golden_path_execute_webhook_recovered_trace(monkeypatch, tmp_path) -> None:
    provider = StubPaymentLinkClient(
        result=PaymentLinkResult(id="plink_gold", short_url="https://rzp.io/l/gold")
    )
    db_path = _seed(
        monkeypatch, tmp_path, "evt_gold", ["payment_link"], razorpay_client=provider
    )
    monkeypatch.setenv("RAZORPAY_WEBHOOK_SECRET", TEST_WEBHOOK_SECRET)

    # 1. Ingested + classified, then the full decision chain runs and a real
    #    razorpay link is created. A created link is waiting, never recovery.
    executed = client.post("/events/evt_gold/execute")
    assert executed.status_code == 200
    body = executed.json()
    assert body["status"] == "execution_success"
    assert body["selected_intervention"] == "payment_link"
    assert body["execution"]["execution_mode"] == "REAL_RAZORPAY"
    assert body["execution"]["status"] == "SUCCESS"
    assert body["execution"]["payment_link_id"] == "plink_gold"
    assert len(provider.calls) == 1

    trace_before = client.get("/events/evt_gold/trace").json()
    assert trace_before["phase12"]["closed_loop"] is True
    assert len(trace_before["phase12"]["payment_links"]) == 1
    link_before = trace_before["phase12"]["payment_links"][0]
    assert link_before["payment_link_id"] == "plink_gold"
    assert link_before["status"] == "waiting"
    assert link_before["recovered_amount_paise"] is None

    # 2. The verified Razorpay delivery (OUTCOME channel) settles the link.
    raw = _paid_event_bytes("plink_gold", "pay_gold_wh", 85_000)
    webhook = client.post(
        "/webhook/razorpay", content=raw, headers=_webhook_headers(raw, "del_gold_1")
    )
    assert webhook.status_code == 200
    assert webhook.json()["status"] == "processed"

    # 3. The same event's trace now reports the trusted recovered amount.
    trace_after = client.get("/events/evt_gold/trace").json()
    link_after = trace_after["phase12"]["payment_links"][0]
    assert link_after["status"] == "recovered"
    assert link_after["recovered_amount_paise"] == 85_000
    assert link_after["payment_id"] == "pay_gold_wh"

    # 4. The decision trace carries the economic stage with honest estimator
    #    provenance and the persisted policy ALLOW.
    optimizer = trace_after["optimizer_decisions"]
    assert optimizer and optimizer[-1]["selected_intervention"] == "payment_link"
    assert optimizer[-1]["estimator_mode"] == "BASELINE"
    assert optimizer[-1]["estimator_version"] is None
    assert optimizer[-1]["estimator_reason"] == "no_calibration_evidence"
    assert (
        trace_after["policy_decisions"][-1]["proposed_intervention"] == "payment_link"
    )

    # 5. The recovery is observable feedback on the same database: recovery
    #    intelligence sees the verified, calibration-eligible recovery.
    observed = client.get("/recovery-intelligence?include_observations=true").json()
    gold = [row for row in observed["observations"] if row["event_id"] == "evt_gold"]
    assert len(gold) == 1
    assert gold[0]["intervention"] == "payment_link"
    assert gold[0]["outcome"] == "RECOVERED"
    assert gold[0]["terminal"] is True
    assert gold[0]["verified_recovery"] is True
    assert gold[0]["recovered"] is True
    assert gold[0]["recovered_amount_paise"] == 85_000


# ---------------------------------------------------------------------------
# SCENARIO C — customer retry limit enforced over the HTTP execute boundary
# ---------------------------------------------------------------------------


def test_c_retry_limit_is_enforced_at_the_http_execute_boundary(
    monkeypatch, tmp_path
) -> None:
    """Two prior interventions in the window: the operator execute path denies.

    The service layer already enforces this (test_policy_adversarial), but the
    HTTP boundary used to be untested; an execute request here must not create
    the third intervention.
    """
    db_path = _seed(monkeypatch, tmp_path, "evt_c", ["retry_delayed", "payment_link"])
    conn = connect(db_path)
    try:
        init_db(conn)
        _attempt(conn, event_id="evt_c_prior_1", customer_id="cust_gold", when=NOW - timedelta(hours=1))
        _attempt(conn, event_id="evt_c_prior_2", customer_id="cust_gold", when=NOW - timedelta(hours=2))
    finally:
        conn.close()

    response = client.post("/recovery/evt_c/execute")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "no_action"
    assert body["row"]["policy"]["denial_reason"] == "customer_intervention_limit_exceeded"
    assert _count(db_path, "execution_outcomes", "evt_c") == 0
    assert _count(db_path, "intervention_attempts", "evt_c") == 0


# ---------------------------------------------------------------------------
# Spend cap enforced over the HTTP execute boundary (configured policy)
# ---------------------------------------------------------------------------


def test_spend_cap_is_enforced_at_the_http_execute_boundary(
    monkeypatch, tmp_path
) -> None:
    """Prior spend of 1005 paise at a 1000 paise cap denies before execution."""
    monkeypatch.setenv("POLICY_DAILY_SPEND_CAP_PAISE", "1000")
    provider = StubPaymentLinkClient(
        result=PaymentLinkResult(id="plink_never_cap", short_url="https://rzp.io/l/never")
    )
    db_path = _seed(
        monkeypatch, tmp_path, "evt_cap", ["payment_link"], razorpay_client=provider
    )
    conn = connect(db_path)
    try:
        init_db(conn)
        _attempt(
            conn,
            event_id="evt_cap_prior",
            customer_id="cust_gold",
            intervention="payment_link",
            cost_paise=1005,
            status="attempted",
            when=NOW - timedelta(hours=1),
        )
    finally:
        conn.close()

    response = client.post("/recovery/evt_cap/execute")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "no_action"
    assert body["row"]["policy"]["denial_reason"] == "spend_cap_exceeded"
    assert provider.calls == []
    assert _count(db_path, "execution_outcomes", "evt_cap") == 0


# ---------------------------------------------------------------------------
# SCENARIO G — calibration visibly changes future economics, never history
# ---------------------------------------------------------------------------


def test_a_calibrated_decision_reorders_rank_but_history_is_immutable(
    monkeypatch, tmp_path,
) -> None:
    """One database: a baseline decision first, then an active snapshot.

    The earlier decision must keep its BASELINE provenance and numbers after
    the snapshot exists (history is never rewritten), while a later decision on
    the same event shape consumes the calibrated posterior, reorders the
    candidates, and records versioned CALIBRATED provenance.
    """
    provider = StubPaymentLinkClient(
        result=PaymentLinkResult(id="plink_never_cal", short_url="https://rzp.io/l/never")
    )
    db_path = _open_db(monkeypatch, tmp_path, "calib.db")
    _ingest("evt_base", ["retry_delayed", "payment_link"], customer_id="cust_base")
    _ingest("evt_cal", ["retry_delayed", "payment_link"], customer_id="cust_cal")
    app.dependency_overrides[get_now] = lambda: NOW
    app.dependency_overrides[get_razorpay_client] = lambda: provider

    # -- baseline decision (both candidates economically evaluated) ---------
    assert client.post("/events/evt_base/execute").json()["status"] == "execution_success"
    baseline_before = _latest_optimizer_decision(db_path, "evt_base")
    assert baseline_before["estimator_mode"] == "BASELINE"
    assert baseline_before["estimator_reason"] == "no_calibration_evidence"
    pl_baseline_bps = _evaluation_probability(baseline_before, "payment_link")
    selected_before = baseline_before["selected_intervention"]
    assert pl_baseline_bps > 0

    # -- calibration evidence that flips the economics for payment_link ------
    # The posterior is gated per intervention and computed once at build time
    # from the canonical baseline BASE_RECOVERY_BPS (not the per-event estimate).
    built = _activate_calibration(db_path, recovered=40, not_recovered=4)
    version = built["version"]
    assert version >= 1
    snapshot_posterior = built["active_bps"][PAYMENT_LINK]
    from app.estimator import BASE_RECOVERY_BPS

    expected_posterior = (
        (40 + (BASE_RECOVERY_BPS[PAYMENT_LINK] * PRIOR_STRENGTH) // PROBABILITY_SCALE)
        * PROBABILITY_SCALE
    ) // ((40 + 4) + PRIOR_STRENGTH)
    assert snapshot_posterior == expected_posterior

    # -- history is immutable: the earlier decision is byte-identical --------
    baseline_after = _latest_optimizer_decision(db_path, "evt_base")
    assert baseline_after == baseline_before
    assert baseline_after["estimator_mode"] == "BASELINE"
    assert baseline_after["estimator_version"] is None
    assert baseline_after["selected_intervention"] == selected_before
    assert _evaluation_probability(baseline_after, "payment_link") == pl_baseline_bps

    # -- the future decision consumes the calibrated posterior ---------------
    assert client.post("/events/evt_cal/execute").json()["status"] == "execution_success"
    calibrated = _latest_optimizer_decision(db_path, "evt_cal")
    assert calibrated["estimator_mode"] == "CALIBRATED"
    assert calibrated["estimator_version"] == version
    assert calibrated["estimator_reason"] == "active_calibration"

    expected = _evaluation_probability(calibrated, "payment_link")
    assert expected == snapshot_posterior

    # The uncalibrated retry intervion is untouched by the payment_link posterior.
    assert _evaluation_probability(calibrated, "retry_delayed") == _evaluation_probability(
        baseline_before, "retry_delayed"
    )

    # The economics reordered the candidates: the baseline pick and the
    # calibrated pick are different, and that is visible in the trace.
    assert selected_before != calibrated["selected_intervention"]
    assert calibrated["selected_intervention"] == "payment_link"


# ---------------------------------------------------------------------------
# threshold_not_met through the execute API (previously unit-level only)
# ---------------------------------------------------------------------------


def test_threshold_not_met_is_recorded_through_the_execute_api(
    monkeypatch, tmp_path,
) -> None:
    """A snapshot that exists but is below the gate stays an honest BASELINE.

    Six recoveries with no confirmed non-recoveries cannot gate a posterior;
    the execute route must record BASELINE with reason ``threshold_not_met``,
    not collapse it into ``no_calibration_evidence`` and not fabricate a
    calibrated ranking.
    """
    provider = StubPaymentLinkClient(
        result=PaymentLinkResult(id="plink_th", short_url="https://rzp.io/l/th")
    )
    db_path = _seed(
        monkeypatch, tmp_path, "evt_th", ["payment_link"], razorpay_client=provider
    )

    conn = connect(db_path)
    try:
        init_db(conn)
        for i in range(6):
            _calibration_part(conn, intervention=PAYMENT_LINK, link_id=f"r{i}", recovered=True)
        built = calibration_service.build_calibration_snapshot(conn, None)
        assert built["active_bps"] == {}
    finally:
        conn.close()

    response = client.post("/events/evt_th/execute")
    assert response.status_code == 200
    assert response.json()["status"] == "execution_success"

    stage = _latest_optimizer_decision(db_path, "evt_th")
    assert stage["estimator_mode"] == "BASELINE"
    assert stage["estimator_version"] == 1
    assert stage["estimator_reason"] == "threshold_not_met"
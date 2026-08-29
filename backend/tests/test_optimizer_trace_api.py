"""Phase 18 tests: the Event Decision Trace reconstructs the economic stage.

The operator-facing trace must show the real chain

    AI -> Policy -> Economic Optimization -> Execution -> Outcome

using persisted backend values only, and must never surface benchmark ground
truth or an economic figure the backend did not compute.
"""

from __future__ import annotations

import json
import pathlib
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

from app.classification import ClassificationResult
from app.dashboard import build_event_trace
from app.db import (
    connect,
    init_db,
    insert_classification_result,
    insert_payment_event,
)
from app.execution_service import (
    SELECTION_V1_FIXED_PRIORITY,
    execute_event,
)
from app.main import app
from app.models import CustomerHistory, PaymentEvent
from app.optimizer import REASON_MAX_EXPECTED_VALUE, REASON_NO_ALLOWED_CANDIDATE
from app.policy import PolicyConfig
from app.routes import dashboard as dashboard_routes
from app.routes import events as events_routes

NOW = datetime(2026, 8, 29, 14, 0, tzinfo=timezone.utc)
DEFAULT_CONFIG = PolicyConfig()

ALL_CANDIDATES = [
    "retry_immediate",
    "retry_delayed",
    "payment_link",
    "reminder",
    "alternate_method_prompt",
]

# Terms that would mean benchmark ground truth had leaked into the operator
# surface. The trace is a MODEL ESTIMATE view; hidden truth belongs to the
# post-decision benchmark evaluator alone.
GROUND_TRUTH_TERMS: tuple[str, ...] = (
    "true_probability",
    "hidden_probability",
    "hidden_world",
    "true_expected_value",
    "oracle",
    "outcome_draw",
    "realized",
    "ground_truth",
)


def _event(
    event_id: str,
    customer_id: str = "cust_trace",
    risk_flag: str = "normal",
    failure_reason: str = "bank_timeout",
    amount_paise: int = 10_000,
) -> PaymentEvent:
    return PaymentEvent.from_dict(
        {
            "event_id": event_id,
            "order_id": f"order_{event_id}",
            "payment_id": f"pay_{event_id}",
            "customer_id": customer_id,
            "amount_paise": amount_paise,
            "currency": "INR",
            "payment_method": "card",
            "failure_reason": failure_reason,
            "bank": "HDFC",
            "risk_flag": risk_flag,
            "customer_history": CustomerHistory(4, 1, True).to_dict(),
            "timestamp": "2026-08-29T12:00:00+00:00",
        }
    )


def _classification(event_id: str, root: str = "transient") -> ClassificationResult:
    return ClassificationResult.from_dict(
        {
            "event_id": event_id,
            "root_cause_category": root,
            "confidence": 0.9,
            "reasoning": "phase 18 trace test classification",
            "candidate_interventions": list(ALL_CANDIDATES),
        }
    )


def _seed(conn, event: PaymentEvent, classification: ClassificationResult) -> None:
    insert_payment_event(conn, event)
    insert_classification_result(conn, classification)


def _run(conn, event_id: str, **kwargs):
    return execute_event(
        conn, event_id, NOW, DEFAULT_CONFIG, razorpay_client=None, **kwargs
    )


# ---------------------------------------------------------------------------
# Test 10 — trace reconstruction
# ---------------------------------------------------------------------------


def test_the_trace_exposes_the_economic_stage_between_policy_and_execution(
    db_conn,
) -> None:
    event_id = "evt_trace_full"
    _seed(db_conn, _event(event_id), _classification(event_id))
    result = _run(db_conn, event_id)

    trace = build_event_trace(db_conn, event_id)

    assert trace["classification"]["candidate_interventions"] == ALL_CANDIDATES
    assert trace["policy_decisions"], "the policy stage is missing"
    assert len(trace["optimizer_decisions"]) == 1
    stage = trace["optimizer_decisions"][0]
    assert stage["selected_intervention"] == result.selected_intervention
    assert stage["selection_reason"] == REASON_MAX_EXPECTED_VALUE
    assert trace["executions"][-1]["intervention"] == result.selected_intervention
    assert trace["summary"]["execution_state"] == "EXECUTED"


def test_the_traced_evaluations_match_the_persisted_decision_exactly(db_conn) -> None:
    event_id = "evt_trace_values"
    _seed(db_conn, _event(event_id), _classification(event_id))
    result = _run(db_conn, event_id)

    stage = build_event_trace(db_conn, event_id)["optimizer_decisions"][0]

    assert stage["evaluations"] == [
        evaluation.to_dict()
        for evaluation in result.optimizer_decision.evaluations
    ]


def test_the_trace_reconstructs_the_full_audit_chain(db_conn) -> None:
    """Given an event id, every stage that occurred is recoverable."""
    event_id = "evt_trace_chain"
    _seed(db_conn, _event(event_id), _classification(event_id))
    _run(db_conn, event_id)

    trace = build_event_trace(db_conn, event_id)

    assert trace["event"]["event_id"] == event_id
    assert trace["classification"]["root_cause_category"] == "transient"
    decided = {d["proposed_intervention"] for d in trace["policy_decisions"]}
    assert decided == set(ALL_CANDIDATES)
    stage = trace["optimizer_decisions"][0]
    allowed = set(stage["allowed_candidates"])
    evaluated = {item["intervention"] for item in stage["evaluations"]}
    assert evaluated == allowed
    assert stage["selected_intervention"] in allowed
    assert trace["attempts"][-1]["intervention"] == stage["selected_intervention"]
    assert trace["phase12"]["closed_loop"] is False


def test_a_policy_denied_candidate_is_visible_but_never_economically_evaluated(
    db_conn,
) -> None:
    event_id = "evt_trace_denied"
    _seed(
        db_conn,
        _event(event_id, risk_flag="fraud_suspect"),
        _classification(event_id),
    )
    _run(db_conn, event_id)

    trace = build_event_trace(db_conn, event_id)

    assert all(not d["allowed"] for d in trace["policy_decisions"])
    stage = trace["optimizer_decisions"][0]
    assert stage["allowed_candidates"] == []
    assert stage["evaluations"] == []
    assert stage["selected_intervention"] == "no_action"
    assert stage["selection_reason"] == REASON_NO_ALLOWED_CANDIDATE
    assert trace["executions"] == []


def test_an_event_with_no_economic_decision_reports_the_stage_as_absent(
    db_conn,
) -> None:
    """A stage that did not occur is represented accurately, never fabricated."""
    event_id = "evt_trace_v1_arm"
    _seed(db_conn, _event(event_id), _classification(event_id))
    _run(db_conn, event_id, selection_strategy=SELECTION_V1_FIXED_PRIORITY)

    trace = build_event_trace(db_conn, event_id)

    assert trace["optimizer_decisions"] == []
    assert trace["executions"], "the V1 arm still executed"


def test_an_unprocessed_event_has_an_empty_economic_stage(db_conn) -> None:
    event_id = "evt_trace_untouched"
    insert_payment_event(db_conn, _event(event_id))

    trace = build_event_trace(db_conn, event_id)

    assert trace["optimizer_decisions"] == []
    assert trace["classification"] is None


# ---------------------------------------------------------------------------
# Test 9 — no ground truth on the operator surface
# ---------------------------------------------------------------------------


def test_the_trace_payload_contains_no_benchmark_ground_truth(db_conn) -> None:
    event_id = "evt_trace_no_truth"
    _seed(db_conn, _event(event_id), _classification(event_id))
    _run(db_conn, event_id)

    payload = json.dumps(build_event_trace(db_conn, event_id)).lower()

    for term in GROUND_TRUTH_TERMS:
        assert term not in payload, f"the trace leaks {term}"


def test_the_economic_stage_exposes_only_estimated_fields(db_conn) -> None:
    event_id = "evt_trace_fields"
    _seed(db_conn, _event(event_id), _classification(event_id))
    _run(db_conn, event_id)

    stage = build_event_trace(db_conn, event_id)["optimizer_decisions"][0]

    assert set(stage) == {
        "event_id",
        "decided_at",
        "selected_intervention",
        "selection_reason",
        "candidates_considered",
        "allowed_candidates",
        "evaluations",
    }
    for item in stage["evaluations"]:
        assert set(item) == {
            "intervention",
            "estimated_probability_bps",
            "amount_paise",
            "expected_recovered_value_paise",
            "intervention_cost_paise",
            "friction_cost_paise",
            "expected_value_paise",
        }


# ---------------------------------------------------------------------------
# HTTP boundary
# ---------------------------------------------------------------------------


@pytest.fixture
def client(tmp_path):
    """A TestClient whose routes share one isolated temporary database."""
    conn = connect(str(tmp_path / "trace_api.db"))
    init_db(conn)

    def _override():
        yield conn

    app.dependency_overrides[dashboard_routes.get_db] = _override
    app.dependency_overrides[events_routes.get_db] = _override
    app.dependency_overrides[events_routes.get_now] = lambda: NOW
    app.dependency_overrides[events_routes.get_razorpay_client] = lambda: None
    try:
        yield TestClient(app), conn
    finally:
        app.dependency_overrides.clear()
        conn.close()


def test_the_frontend_stage_renders_backend_values_only() -> None:
    """The dashboard displays the economic decision; it never invents one.

    Asserted against the component source because "no hardcoded economics" and
    "no hidden truth on the operator surface" are architectural properties of
    the file, not of any single rendered state.
    """
    component = (
        pathlib.Path(__file__).resolve().parents[2]
        / "frontend"
        / "src"
        / "components"
        / "EventTrace.jsx"
    ).read_text()

    assert "Economic Optimization" in component
    assert "optimizer_decisions" in component
    for field in (
        "estimated_probability_bps",
        "expected_recovered_value_paise",
        "intervention_cost_paise",
        "friction_cost_paise",
        "expected_value_paise",
        "selection_reason",
    ):
        assert field in component, f"the stage does not read {field}"

    lowered = component.lower()
    for term in GROUND_TRUTH_TERMS:
        assert term not in lowered, f"the dashboard references {term}"
    # Terminology must not overstate a model estimate as a measured result.
    for term in (
        "actual recovery probability",
        "guaranteed recovery",
        "actual expected revenue",
        "realized ev",
    ):
        assert term not in lowered, f"misleading terminology: {term}"
    assert "MODEL ESTIMATE" in component


def test_the_frontend_does_not_reimplement_the_economic_equation() -> None:
    """The equation lives in the backend; the UI only displays its output."""
    component = (
        pathlib.Path(__file__).resolve().parents[2]
        / "frontend"
        / "src"
        / "components"
        / "EventTrace.jsx"
    ).read_text()

    # The one arithmetic-looking construct in the stage is basis-point display
    # formatting, which never touches money.
    economics_lines = [
        line
        for line in component.splitlines()
        if "_paise" in line and any(op in line for op in (" * ", " - ", " + ", " / "))
    ]
    assert economics_lines == [], f"the UI recomputes economics: {economics_lines}"


def test_the_trace_endpoint_returns_the_economic_stage(client) -> None:
    api, conn = client
    event_id = "evt_api_trace"
    _seed(conn, _event(event_id), _classification(event_id))
    api.post(f"/events/{event_id}/execute")

    response = api.get(f"/events/{event_id}/trace")

    assert response.status_code == 200
    body = response.json()
    stage = body["optimizer_decisions"][0]
    assert stage["selection_reason"] == REASON_MAX_EXPECTED_VALUE
    assert stage["selected_intervention"] == body["executions"][-1]["intervention"]


def test_the_trace_endpoint_stays_backward_compatible(client) -> None:
    """Existing consumers keep every key they already relied on."""
    api, conn = client
    event_id = "evt_api_compat"
    _seed(conn, _event(event_id), _classification(event_id))
    api.post(f"/events/{event_id}/execute")

    body = api.get(f"/events/{event_id}/trace").json()

    for key in (
        "event",
        "classification",
        "policy_decisions",
        "executions",
        "attempts",
        "phase12",
        "summary",
    ):
        assert key in body


def test_an_economic_failure_is_reported_explicitly_and_executes_nothing(
    client, monkeypatch
) -> None:
    api, conn = client
    event_id = "evt_api_economic_failure"
    _seed(conn, _event(event_id), _classification(event_id))

    class _BrokenEstimator:
        def estimate(self, event, classification, intervention):
            return 0.5

    monkeypatch.setattr(
        "app.execution_service.RecoveryProbabilityEstimator", _BrokenEstimator
    )

    response = api.post(f"/events/{event_id}/execute")

    assert response.status_code == 500
    assert response.json()["status"] == "economic_selection_failure"
    assert (
        conn.execute(
            "SELECT COUNT(*) FROM execution_outcomes WHERE event_id = ?", (event_id,)
        ).fetchone()[0]
        == 0
    )
    assert build_event_trace(conn, event_id)["optimizer_decisions"] == []

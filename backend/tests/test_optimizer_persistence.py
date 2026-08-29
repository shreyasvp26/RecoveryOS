"""Phase 18 tests: the economic optimizer's decision is durably auditable.

Exercises the real chain through ``execute_event`` against a real SQLite
database, the real policy engine, the real estimator and the real optimizer,
and asserts that the economic decision RecoveryOS actually made is recorded —
before execution, unchanged, without ground truth, and without ever admitting
a policy-denied candidate.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

from app.classification import ClassificationResult
from app.db import (
    get_optimizer_decision,
    get_optimizer_decisions_for_event,
    insert_classification_result,
    insert_intervention_attempt,
    insert_optimizer_decision,
    insert_payment_event,
)
from app.economics import DEFAULT_ECONOMIC_MODEL, CandidateEvaluation
from app.estimator import RecoveryProbabilityEstimator
from app.execution_service import (
    SELECTION_V1_FIXED_PRIORITY,
    SELECTION_V2_ECONOMIC,
    STATUS_EXECUTION_FAILED,
    STATUS_EXECUTION_SUCCESS,
    STATUS_NO_ACTION,
    execute_event,
)
from app.models import CustomerHistory, PaymentEvent
from app.optimizer import (
    REASON_MAX_EXPECTED_VALUE,
    REASON_NO_ALLOWED_CANDIDATE,
    OptimizerError,
)
from app.optimizer_audit import OptimizerAuditError, OptimizerDecisionRecord
from app.policy import InterventionAttempt, PolicyConfig

NOW = datetime(2026, 8, 29, 13, 0, tzinfo=timezone.utc)
DECIDED_AT = NOW.isoformat()
DEFAULT_CONFIG = PolicyConfig()

ALL_CANDIDATES = [
    "retry_immediate",
    "retry_delayed",
    "payment_link",
    "reminder",
    "alternate_method_prompt",
]


def _event(
    event_id: str,
    customer_id: str = "cust_p18",
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


def _classification(
    event_id: str, root: str = "transient", candidates: list[str] | None = None
) -> ClassificationResult:
    return ClassificationResult.from_dict(
        {
            "event_id": event_id,
            "root_cause_category": root,
            "confidence": 0.9,
            "reasoning": "phase 18 audit persistence test classification",
            "candidate_interventions": candidates or list(ALL_CANDIDATES),
        }
    )


def _seed(conn, event: PaymentEvent, classification: ClassificationResult) -> None:
    insert_payment_event(conn, event)
    insert_classification_result(conn, classification)


def _run(conn, event_id: str, config: PolicyConfig = DEFAULT_CONFIG, **kwargs):
    return execute_event(conn, event_id, NOW, config, razorpay_client=None, **kwargs)


def _records(conn, event_id: str) -> list[dict]:
    return get_optimizer_decisions_for_event(conn, event_id)


def _counts(conn, event_id: str) -> tuple[int, int]:
    outcomes = conn.execute(
        "SELECT COUNT(*) FROM execution_outcomes WHERE event_id = ?", (event_id,)
    ).fetchone()[0]
    attempts = conn.execute(
        "SELECT COUNT(*) FROM intervention_attempts WHERE event_id = ?", (event_id,)
    ).fetchone()[0]
    return outcomes, attempts


# ---------------------------------------------------------------------------
# Test 1 / 2 / 4 — the decision, its candidates and its rationale are recorded
# ---------------------------------------------------------------------------


def test_a_normal_v2_decision_is_persisted(db_conn) -> None:
    event_id = "evt_p18_persist"
    _seed(db_conn, _event(event_id), _classification(event_id))

    result = _run(db_conn, event_id)

    assert result.status == STATUS_EXECUTION_SUCCESS
    records = _records(db_conn, event_id)
    assert len(records) == 1
    record = records[0]
    assert record["event_id"] == event_id
    assert record["decided_at"] == DECIDED_AT
    assert record["selected_intervention"] == result.selected_intervention
    assert record["selection_reason"] == REASON_MAX_EXPECTED_VALUE


def test_every_policy_approved_candidate_has_a_persisted_evaluation(db_conn) -> None:
    event_id = "evt_p18_evaluations"
    _seed(db_conn, _event(event_id), _classification(event_id))

    result = _run(db_conn, event_id)

    record = _records(db_conn, event_id)[0]
    assert set(record["allowed_candidates"]) == set(ALL_CANDIDATES)
    evaluated = [item["intervention"] for item in record["evaluations"]]
    assert set(evaluated) == set(record["allowed_candidates"])
    assert len(evaluated) == len(record["allowed_candidates"])
    assert set(record["candidates_considered"]) == set(ALL_CANDIDATES)
    assert result.selected_intervention == evaluated[0]


def test_the_persisted_record_is_the_optimizer_own_output(db_conn) -> None:
    """Persistence records the decision; it never recomputes a different one."""
    event_id = "evt_p18_same_numbers"
    _seed(db_conn, _event(event_id), _classification(event_id))

    result = _run(db_conn, event_id)

    record = _records(db_conn, event_id)[0]
    assert record["evaluations"] == [
        evaluation.to_dict()
        for evaluation in result.optimizer_decision.evaluations
    ]


def test_the_persisted_economics_are_hand_checkable(db_conn) -> None:
    """The stored figures re-derive the frozen equation exactly, in integers."""
    event_id = "evt_p18_hand_check"
    _seed(db_conn, _event(event_id, amount_paise=2_500_00), _classification(event_id))

    _run(db_conn, event_id)

    for item in _records(db_conn, event_id)[0]["evaluations"]:
        economics = DEFAULT_ECONOMIC_MODEL.for_intervention(item["intervention"])
        assert item["amount_paise"] == 2_500_00
        assert item["intervention_cost_paise"] == economics.cost_paise
        assert item["friction_cost_paise"] == (
            item["amount_paise"] * economics.friction_bps // 10_000
        )
        assert item["expected_recovered_value_paise"] == (
            item["amount_paise"] * item["estimated_probability_bps"] // 10_000
        )
        assert item["expected_value_paise"] == (
            item["expected_recovered_value_paise"]
            - item["intervention_cost_paise"]
            - item["friction_cost_paise"]
        )


def test_the_selected_intervention_is_the_persisted_expected_value_maximum(
    db_conn,
) -> None:
    event_id = "evt_p18_max_ev"
    _seed(
        db_conn,
        _event(event_id, failure_reason="expired_card"),
        _classification(event_id, root="customer_action_needed"),
    )

    result = _run(db_conn, event_id)

    record = _records(db_conn, event_id)[0]
    best = max(record["evaluations"], key=lambda item: item["expected_value_paise"])
    assert record["selected_intervention"] == best["intervention"]
    assert record["selected_intervention"] == result.selected_intervention


# ---------------------------------------------------------------------------
# Test 3 / 5 — policy stays authoritative in the audit record
# ---------------------------------------------------------------------------


def test_a_policy_denied_candidate_never_appears_as_an_evaluated_candidate(
    db_conn,
) -> None:
    """The headline invariant, asserted against the durable record."""
    event_id = "evt_p18_denied"
    _seed(
        db_conn,
        _event(event_id, failure_reason="expired_card", amount_paise=2_000_000),
        _classification(event_id, root="customer_action_needed"),
    )
    config = PolicyConfig(
        daily_spend_cap_paise=0,
        intervention_cost_paise={
            "payment_link": 100,
            "alternate_method_prompt": 100,
            "reminder": 100,
            "retry_delayed": 0,
            "retry_immediate": 0,
        },
    )

    result = _run(db_conn, event_id, config=config)

    record = _records(db_conn, event_id)[0]
    evaluated = {item["intervention"] for item in record["evaluations"]}
    assert evaluated == {"retry_delayed", "retry_immediate"}
    assert set(record["allowed_candidates"]) == evaluated
    assert "payment_link" not in evaluated
    assert "alternate_method_prompt" not in evaluated
    # The denial is still visible: the candidate was considered, then denied.
    assert "payment_link" in record["candidates_considered"]
    assert record["selected_intervention"] == result.selected_intervention
    assert record["selected_intervention"] in evaluated


def test_all_candidates_denied_records_no_action_and_executes_nothing(
    db_conn,
) -> None:
    event_id = "evt_p18_all_denied"
    _seed(
        db_conn,
        _event(event_id, risk_flag="fraud_suspect", amount_paise=5_000_00),
        _classification(event_id),
    )

    result = _run(db_conn, event_id)

    assert result.status == STATUS_NO_ACTION
    assert _counts(db_conn, event_id) == (0, 0)
    record = _records(db_conn, event_id)[0]
    assert record["selected_intervention"] == "no_action"
    assert record["selection_reason"] == REASON_NO_ALLOWED_CANDIDATE
    assert record["allowed_candidates"] == []
    assert record["evaluations"] == []


def test_a_terminal_event_records_no_action_and_executes_nothing(db_conn) -> None:
    event_id = "evt_p18_terminal"
    _seed(
        db_conn,
        _event(event_id, amount_paise=5_000_00),
        _classification(event_id, root="terminal"),
    )

    result = _run(db_conn, event_id)

    assert result.status == STATUS_NO_ACTION
    assert _counts(db_conn, event_id) == (0, 0)
    assert _records(db_conn, event_id)[0]["selected_intervention"] == "no_action"


def test_a_duplicate_protected_event_records_no_action_and_executes_nothing(
    db_conn,
) -> None:
    event_id = "evt_p18_duplicate"
    _seed(db_conn, _event(event_id), _classification(event_id))
    insert_intervention_attempt(
        db_conn,
        InterventionAttempt(
            event_id=event_id,
            intervention="retry_delayed",
            customer_id="cust_p18",
            cost_paise=0,
            attempted_at=(NOW - timedelta(hours=1)).isoformat(),
            status="successful",
        ),
    )

    result = _run(db_conn, event_id)

    assert result.status == STATUS_NO_ACTION
    # Only the seeded history row; no new execution and no new attempt.
    assert _counts(db_conn, event_id) == (0, 1)
    assert _records(db_conn, event_id)[0]["selected_intervention"] == "no_action"


# ---------------------------------------------------------------------------
# Audit before action
# ---------------------------------------------------------------------------


def test_the_decision_is_recorded_even_when_execution_fails(db_conn) -> None:
    """A failed execution must still leave evidence of what was decided."""
    event_id = "evt_p18_exec_failed"
    _seed(
        db_conn,
        _event(event_id, failure_reason="insufficient_funds"),
        _classification(
            event_id, root="customer_action_needed", candidates=["payment_link"]
        ),
    )

    result = _run(db_conn, event_id)

    assert result.status == STATUS_EXECUTION_FAILED
    record = _records(db_conn, event_id)[0]
    assert record["selected_intervention"] == "payment_link"
    assert [item["intervention"] for item in record["evaluations"]] == ["payment_link"]


def test_the_decision_is_recorded_before_the_executor_runs(db_conn) -> None:
    """Ordering is asserted, not assumed: the audit row exists at execute time."""
    event_id = "evt_p18_ordering"
    _seed(db_conn, _event(event_id), _classification(event_id))
    observed: list[int] = []

    import app.execution_service as execution_service

    real_executor = execution_service.BoundedExecutor

    class ObservingExecutor(real_executor):  # type: ignore[misc, valid-type]
        def execute(self, *args, **kwargs):
            observed.append(len(_records(db_conn, event_id)))
            return super().execute(*args, **kwargs)

    execution_service.BoundedExecutor = ObservingExecutor
    try:
        _run(db_conn, event_id)
    finally:
        execution_service.BoundedExecutor = real_executor

    assert observed == [1], "the optimizer decision was not persisted before execution"


# ---------------------------------------------------------------------------
# Test 6 / 7 — controlled failure, never unsafe execution
# ---------------------------------------------------------------------------


class _BrokenEstimator:
    """An estimator that returns something the economic model cannot use."""

    def estimate(self, event, classification, intervention):
        return 0.42


class _RaisingEstimator:
    def estimate(self, event, classification, intervention):
        raise RuntimeError("estimator unavailable")


def test_an_estimator_returning_a_bad_type_fails_safe(db_conn, monkeypatch) -> None:
    event_id = "evt_p18_bad_estimator"
    _seed(db_conn, _event(event_id), _classification(event_id))
    monkeypatch.setattr(
        "app.execution_service.RecoveryProbabilityEstimator", _BrokenEstimator
    )

    with pytest.raises(OptimizerError):
        _run(db_conn, event_id)

    assert _counts(db_conn, event_id) == (0, 0)
    assert _records(db_conn, event_id) == []


def test_an_estimator_that_raises_fails_safe(db_conn, monkeypatch) -> None:
    event_id = "evt_p18_raising_estimator"
    _seed(db_conn, _event(event_id), _classification(event_id))
    monkeypatch.setattr(
        "app.execution_service.RecoveryProbabilityEstimator", _RaisingEstimator
    )

    with pytest.raises(RuntimeError):
        _run(db_conn, event_id)

    assert _counts(db_conn, event_id) == (0, 0)
    assert _records(db_conn, event_id) == []


def test_an_optimizer_failure_prevents_execution_and_persistence(
    db_conn, monkeypatch
) -> None:
    event_id = "evt_p18_optimizer_failure"
    _seed(db_conn, _event(event_id), _classification(event_id))

    class _BrokenOptimizer:
        def __init__(self, *args, **kwargs) -> None:
            pass

        def select(self, *args, **kwargs):
            raise OptimizerError("cannot produce a safe decision")

    monkeypatch.setattr(
        "app.execution_service.EconomicInterventionOptimizer", _BrokenOptimizer
    )

    with pytest.raises(OptimizerError):
        _run(db_conn, event_id)

    assert _counts(db_conn, event_id) == (0, 0)
    assert _records(db_conn, event_id) == []


def test_a_persistence_failure_stops_the_flow_before_execution(
    db_conn, monkeypatch
) -> None:
    """An audit write that fails must not be followed by an unaudited action."""
    event_id = "evt_p18_persistence_failure"
    _seed(db_conn, _event(event_id), _classification(event_id))

    def _fail(conn, record):
        raise sqlite3.OperationalError("audit store unavailable")

    monkeypatch.setattr("app.execution_service.insert_optimizer_decision", _fail)

    with pytest.raises(sqlite3.OperationalError):
        _run(db_conn, event_id)

    assert _counts(db_conn, event_id) == (0, 0)


# ---------------------------------------------------------------------------
# Test 8 — determinism
# ---------------------------------------------------------------------------


def test_the_persisted_decision_is_identical_across_identical_runs(tmp_path) -> None:
    from app.db import connect, init_db

    persisted = []
    for index in range(3):
        conn = connect(str(tmp_path / f"p18_determinism_{index}.db"))
        init_db(conn)
        try:
            event_id = "evt_p18_determinism"
            _seed(conn, _event(event_id), _classification(event_id))
            _run(conn, event_id)
            persisted.append(get_optimizer_decisions_for_event(conn, event_id))
        finally:
            conn.close()

    assert persisted[0] == persisted[1] == persisted[2]


def test_re_running_the_same_event_at_the_same_time_reuses_the_record(
    db_conn,
) -> None:
    """Determinism makes the audit record idempotent, never duplicated."""
    event_id = "evt_p18_idempotent"
    _seed(db_conn, _event(event_id, risk_flag="fraud_suspect"), _classification(event_id))

    first = _run(db_conn, event_id)
    second = _run(db_conn, event_id)

    assert first.status == second.status == STATUS_NO_ACTION
    assert len(_records(db_conn, event_id)) == 1


# ---------------------------------------------------------------------------
# The V1 arm has no economics to record
# ---------------------------------------------------------------------------


def test_the_v1_arm_persists_no_optimizer_decision(db_conn) -> None:
    """A stage that genuinely did not happen is represented as absent."""
    event_id = "evt_p18_v1_arm"
    _seed(db_conn, _event(event_id), _classification(event_id))

    _run(db_conn, event_id, selection_strategy=SELECTION_V1_FIXED_PRIORITY)

    assert _records(db_conn, event_id) == []


def test_the_v2_arm_is_the_default_and_does_persist(db_conn) -> None:
    event_id = "evt_p18_v2_default"
    _seed(db_conn, _event(event_id), _classification(event_id))

    _run(db_conn, event_id, selection_strategy=SELECTION_V2_ECONOMIC)

    assert len(_records(db_conn, event_id)) == 1


# ---------------------------------------------------------------------------
# The audit contract itself
# ---------------------------------------------------------------------------


def _evaluation(intervention: str, expected_value_paise: int = 100) -> CandidateEvaluation:
    return CandidateEvaluation(
        intervention=intervention,
        estimated_probability_bps=3000,
        amount_paise=10_000,
        expected_recovered_value_paise=3_000,
        intervention_cost_paise=0,
        friction_cost_paise=0,
        expected_value_paise=expected_value_paise,
    )


def _record(**overrides) -> OptimizerDecisionRecord:
    data = {
        "event_id": "evt_contract",
        "decided_at": DECIDED_AT,
        "selected_intervention": "retry_delayed",
        "selection_reason": REASON_MAX_EXPECTED_VALUE,
        "candidates_considered": ("retry_delayed", "payment_link"),
        "allowed_candidates": ("retry_delayed",),
        "evaluations": (_evaluation("retry_delayed"),),
    }
    data.update(overrides)
    return OptimizerDecisionRecord(**data)


def test_a_record_cannot_evaluate_a_candidate_policy_did_not_allow() -> None:
    with pytest.raises(OptimizerAuditError):
        _record(evaluations=(_evaluation("payment_link"),))


def test_a_record_cannot_allow_a_candidate_that_was_never_considered() -> None:
    with pytest.raises(OptimizerAuditError):
        _record(
            candidates_considered=("retry_delayed",),
            allowed_candidates=("retry_delayed", "reminder"),
            evaluations=(),
        )


def test_a_record_rejects_a_missing_identifier() -> None:
    with pytest.raises(OptimizerAuditError):
        _record(event_id="")
    with pytest.raises(OptimizerAuditError):
        _record(decided_at="")


def test_a_record_rejects_a_non_evaluation_entry() -> None:
    with pytest.raises(OptimizerAuditError):
        _record(evaluations=({"intervention": "retry_delayed"},))


def test_from_decision_rejects_anything_that_is_not_an_optimizer_decision() -> None:
    with pytest.raises(OptimizerAuditError):
        OptimizerDecisionRecord.from_decision("evt", DECIDED_AT, object())


def test_a_record_round_trips_through_its_serialized_form() -> None:
    record = _record()
    assert OptimizerDecisionRecord.from_dict(record.to_dict()) == record


def test_from_dict_rejects_a_truncated_record() -> None:
    data = _record().to_dict()
    del data["selection_reason"]
    with pytest.raises(OptimizerAuditError):
        OptimizerDecisionRecord.from_dict(data)


def test_from_dict_rejects_a_truncated_evaluation() -> None:
    data = _record().to_dict()
    del data["evaluations"][0]["expected_value_paise"]
    with pytest.raises(OptimizerAuditError):
        OptimizerDecisionRecord.from_dict(data)


def test_the_store_is_append_only(db_conn) -> None:
    record = _record()
    insert_optimizer_decision(db_conn, record)
    with pytest.raises(sqlite3.IntegrityError):
        insert_optimizer_decision(db_conn, record)
    assert get_optimizer_decision(db_conn, record.event_id, record.decided_at) == record


def test_the_store_rejects_a_foreign_object(db_conn) -> None:
    with pytest.raises(TypeError):
        insert_optimizer_decision(db_conn, {"event_id": "evt"})


def test_an_absent_record_reads_back_as_none(db_conn) -> None:
    assert get_optimizer_decision(db_conn, "evt_missing", DECIDED_AT) is None
    assert get_optimizer_decisions_for_event(db_conn, "evt_missing") == []


def test_the_real_estimator_remains_the_authoritative_probability_source(
    db_conn,
) -> None:
    """The persisted probability is the estimator's, not a persistence artefact."""
    event_id = "evt_p18_estimator_authority"
    event = _event(event_id)
    classification = _classification(event_id)
    _seed(db_conn, event, classification)

    _run(db_conn, event_id)

    estimator = RecoveryProbabilityEstimator()
    for item in _records(db_conn, event_id)[0]["evaluations"]:
        expected = estimator.estimate(event, classification, item["intervention"])
        assert item["estimated_probability_bps"] == expected.basis_points

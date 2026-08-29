"""Phase 21 unit tests for the Recovery Operations projection.

These tests exercise the projection as a pure function of persisted records
plus the database-backed queue builder. The invariants under test are the ones
the operator relies on: execution is not recovery, simulated is not real, a
blocked row always explains itself, and the order is deterministic.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

from app.db import (
    insert_classification_result,
    insert_execution_outcome,
    insert_optimizer_decision,
    insert_payment_event,
    insert_policy_decision,
    insert_webhook_recovery_outcome,
)
from app.classification import ClassificationResult
from app.economics import CandidateEvaluation
from app.executor import ExecutionOutcome
from app.models import PaymentEvent
from app.optimizer_audit import OptimizerDecisionRecord
from app.policy import PolicyDecision
from app.recovery_operations import (
    POLICY_ALLOWED,
    POLICY_BLOCKED,
    POLICY_NOT_EVALUATED,
    SORT_AMOUNT_DESC,
    SORT_EXPECTED_RECOVERY_DESC,
    SORT_OLDEST_PENDING_OUTCOME,
    STATE_BLOCKED,
    STATE_EXECUTED,
    STATE_FAILED,
    STATE_NOT_CLASSIFIED,
    STATE_PENDING_OUTCOME,
    STATE_POLICY_ALLOWED,
    STATE_RECOMMENDED,
    STATE_RECOVERED,
    STATE_SELECTED,
    RecoveryQueueError,
    build_queue_row,
    build_queue_row_for_event,
    build_recovery_queue,
)

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


def _classification(event_id: str, candidates=("retry_delayed", "payment_link")):
    return ClassificationResult(
        event_id=event_id,
        root_cause_category="transient",
        confidence=0.88,
        reasoning="transient bank timeout",
        candidate_interventions=tuple(candidates),
    )


def _decision(event_id: str, intervention: str, allowed: bool, at=NOW, reason=None):
    return PolicyDecision(
        event_id=event_id,
        proposed_intervention=intervention,
        allowed=allowed,
        denial_reason=None if allowed else (reason or "fraud_protection"),
        policy_rules_applied=("fraud_check_passed",) if allowed else (reason or "fraud_protection",),
        evaluated_at=at.isoformat(),
    )


def _optimizer_record(event_id: str, selected: str, expected_value_paise: int, at=NOW):
    evaluation = CandidateEvaluation(
        intervention=selected,
        estimated_probability_bps=3000,
        amount_paise=50_000,
        expected_recovered_value_paise=15_000,
        intervention_cost_paise=500,
        friction_cost_paise=0,
        expected_value_paise=expected_value_paise,
    )
    return OptimizerDecisionRecord(
        event_id=event_id,
        decided_at=at.isoformat(),
        selected_intervention=selected,
        selection_reason="highest expected value",
        candidates_considered=(selected,),
        allowed_candidates=(selected,),
        evaluations=(evaluation,),
    )


def _execution(
    event_id: str,
    intervention: str = "retry_delayed",
    mode: str = "SIMULATED",
    status: str = "SUCCESS",
    payment_link_id: str | None = None,
    at=NOW,
):
    return ExecutionOutcome(
        event_id=event_id,
        intervention=intervention,
        execution_mode=mode,
        status=status,
        external_reference="https://rzp.io/l/x" if payment_link_id else None,
        detail=None if status == "SUCCESS" else "razorpay_api_error",
        reported_at=at.isoformat(),
        payment_link_id=payment_link_id,
    )


def _row(conn, event_id: str) -> dict:
    return build_queue_row_for_event(conn, event_id)


# ---------------------------------------------------------------------------
# State derivation
# ---------------------------------------------------------------------------


def test_an_unclassified_event_is_not_classified(db_conn) -> None:
    insert_payment_event(db_conn, _event("evt_raw"))
    row = _row(db_conn, "evt_raw")
    assert row["lifecycle_state"] == STATE_NOT_CLASSIFIED
    assert row["diagnosis"] is None
    assert row["policy"]["status"] == POLICY_NOT_EVALUATED
    assert row["outcome"]["state"] == "NOT_EXECUTED"


def test_a_classified_event_without_policy_is_recommended(db_conn) -> None:
    insert_payment_event(db_conn, _event("evt_rec"))
    insert_classification_result(db_conn, _classification("evt_rec"))
    row = _row(db_conn, "evt_rec")
    assert row["lifecycle_state"] == STATE_RECOMMENDED
    assert row["diagnosis"]["root_cause_category"] == "transient"
    assert row["diagnosis"]["candidate_interventions"] == ["retry_delayed", "payment_link"]
    assert row["actionable"] is True


def test_an_allowed_event_without_selection_is_policy_allowed(db_conn) -> None:
    insert_payment_event(db_conn, _event("evt_allow"))
    insert_classification_result(db_conn, _classification("evt_allow"))
    insert_policy_decision(db_conn, _decision("evt_allow", "retry_delayed", True))
    row = _row(db_conn, "evt_allow")
    assert row["lifecycle_state"] == STATE_POLICY_ALLOWED
    assert row["policy"]["status"] == POLICY_ALLOWED
    assert row["policy"]["allowed_interventions"] == ["retry_delayed"]
    assert row["policy"]["denial_reason"] is None


def test_a_selected_event_shows_the_optimizer_estimate(db_conn) -> None:
    insert_payment_event(db_conn, _event("evt_sel"))
    insert_classification_result(db_conn, _classification("evt_sel"))
    insert_policy_decision(db_conn, _decision("evt_sel", "retry_delayed", True))
    insert_optimizer_decision(
        db_conn, _optimizer_record("evt_sel", "retry_delayed", 14_500)
    )
    row = _row(db_conn, "evt_sel")
    assert row["lifecycle_state"] == STATE_SELECTED
    assert row["selection"]["selected_intervention"] == "retry_delayed"
    assert row["selection"]["expected_value_paise"] == 14_500
    assert row["selection"]["selection_reason"] == "highest expected value"


def test_a_fully_denied_event_is_blocked_and_explains_why(db_conn) -> None:
    insert_payment_event(db_conn, _event("evt_block", risk_flag="fraud_suspect"))
    insert_classification_result(db_conn, _classification("evt_block"))
    insert_policy_decision(
        db_conn, _decision("evt_block", "retry_delayed", False, reason="fraud_protection")
    )
    insert_policy_decision(
        db_conn, _decision("evt_block", "payment_link", False, reason="fraud_protection")
    )
    row = _row(db_conn, "evt_block")
    assert row["lifecycle_state"] == STATE_BLOCKED
    assert row["policy"]["status"] == POLICY_BLOCKED
    assert row["policy"]["denial_reason"] == "fraud_protection"
    assert row["policy"]["denial_rule_label"] == "Fraud protection"
    assert row["policy"]["denied_interventions"] == ["payment_link", "retry_delayed"]
    assert row["actionable"] is False


def test_a_partly_denied_event_is_still_allowed(db_conn) -> None:
    """One ALLOW is enough for the gate to have authorized action."""
    insert_payment_event(db_conn, _event("evt_mixed"))
    insert_classification_result(db_conn, _classification("evt_mixed"))
    insert_policy_decision(db_conn, _decision("evt_mixed", "retry_delayed", True))
    insert_policy_decision(
        db_conn, _decision("evt_mixed", "payment_link", False, reason="spend_cap_exceeded")
    )
    row = _row(db_conn, "evt_mixed")
    assert row["policy"]["status"] == POLICY_ALLOWED
    assert row["policy"]["allowed_interventions"] == ["retry_delayed"]
    assert row["policy"]["denied_interventions"] == ["payment_link"]
    assert row["lifecycle_state"] == STATE_POLICY_ALLOWED


def test_only_the_most_recent_policy_evaluation_drives_the_row(db_conn) -> None:
    insert_payment_event(db_conn, _event("evt_reeval"))
    insert_classification_result(db_conn, _classification("evt_reeval"))
    insert_policy_decision(
        db_conn,
        _decision("evt_reeval", "retry_delayed", True, at=NOW - timedelta(hours=2)),
    )
    insert_policy_decision(
        db_conn,
        _decision("evt_reeval", "retry_delayed", False, reason="event_cooldown_active"),
    )
    row = _row(db_conn, "evt_reeval")
    assert row["policy"]["status"] == POLICY_BLOCKED
    assert row["policy"]["denial_reason"] == "event_cooldown_active"
    assert row["policy"]["evaluated_at"] == NOW.isoformat()


# ---------------------------------------------------------------------------
# Execution is not recovery; simulated is not real
# ---------------------------------------------------------------------------


def test_a_simulated_execution_is_executed_and_never_recovered(db_conn) -> None:
    insert_payment_event(db_conn, _event("evt_sim"))
    insert_classification_result(db_conn, _classification("evt_sim"))
    insert_policy_decision(db_conn, _decision("evt_sim", "retry_delayed", True))
    insert_execution_outcome(db_conn, _execution("evt_sim"))
    row = _row(db_conn, "evt_sim")
    assert row["lifecycle_state"] == STATE_EXECUTED
    assert row["execution"]["execution_mode"] == "SIMULATED"
    assert row["outcome"]["state"] == STATE_EXECUTED
    assert row["outcome"]["recovered_amount_paise"] is None
    assert "no revenue is claimed" in row["outcome"]["note"]


def test_a_real_payment_link_is_pending_until_a_verified_webhook(db_conn) -> None:
    insert_payment_event(db_conn, _event("evt_link"))
    insert_classification_result(db_conn, _classification("evt_link"))
    insert_policy_decision(db_conn, _decision("evt_link", "payment_link", True))
    insert_execution_outcome(
        db_conn,
        _execution(
            "evt_link",
            intervention="payment_link",
            mode="REAL_RAZORPAY",
            payment_link_id="plink_1",
        ),
    )
    row = _row(db_conn, "evt_link")
    assert row["lifecycle_state"] == STATE_PENDING_OUTCOME
    assert row["execution"]["execution_mode"] == "REAL_RAZORPAY"
    assert row["execution"]["payment_link_id"] == "plink_1"
    assert row["outcome"]["state"] == STATE_PENDING_OUTCOME
    assert row["outcome"]["recovered_amount_paise"] is None


def test_a_verified_webhook_recovery_makes_the_row_recovered(db_conn) -> None:
    insert_payment_event(db_conn, _event("evt_paid"))
    insert_classification_result(db_conn, _classification("evt_paid"))
    insert_policy_decision(db_conn, _decision("evt_paid", "payment_link", True))
    insert_execution_outcome(
        db_conn,
        _execution(
            "evt_paid",
            intervention="payment_link",
            mode="REAL_RAZORPAY",
            payment_link_id="plink_paid",
        ),
    )
    insert_webhook_recovery_outcome(
        db_conn,
        delivery_id="delivery_1",
        payment_link_id="plink_paid",
        referenced_event_id="evt_paid",
        amount_paid_paise=50_000,
        currency="INR",
        payment_id="pay_verified",
        recovered_at=(NOW + timedelta(minutes=5)).isoformat(),
    )
    row = _row(db_conn, "evt_paid")
    assert row["lifecycle_state"] == STATE_RECOVERED
    # The recovered amount is the provider-reported amount_paid, not the event.
    assert row["outcome"]["recovered_amount_paise"] == 50_000
    assert row["outcome"]["payment_id"] == "pay_verified"


def test_a_recovery_for_a_different_link_never_marks_this_row_recovered(db_conn) -> None:
    insert_payment_event(db_conn, _event("evt_other"))
    insert_classification_result(db_conn, _classification("evt_other"))
    insert_execution_outcome(
        db_conn,
        _execution(
            "evt_other",
            intervention="payment_link",
            mode="REAL_RAZORPAY",
            payment_link_id="plink_mine",
        ),
    )
    insert_webhook_recovery_outcome(
        db_conn,
        delivery_id="delivery_other",
        payment_link_id="plink_someone_else",
        referenced_event_id="evt_elsewhere",
        amount_paid_paise=99_999,
        currency="INR",
        payment_id="pay_elsewhere",
        recovered_at=NOW.isoformat(),
    )
    row = _row(db_conn, "evt_other")
    assert row["lifecycle_state"] == STATE_PENDING_OUTCOME
    assert row["outcome"]["recovered_amount_paise"] is None


def test_a_failed_execution_is_failed_and_claims_nothing(db_conn) -> None:
    insert_payment_event(db_conn, _event("evt_fail"))
    insert_classification_result(db_conn, _classification("evt_fail"))
    insert_execution_outcome(
        db_conn,
        _execution(
            "evt_fail",
            intervention="payment_link",
            mode="REAL_RAZORPAY",
            status="FAILED",
        ),
    )
    row = _row(db_conn, "evt_fail")
    assert row["lifecycle_state"] == STATE_FAILED
    assert row["outcome"]["state"] == STATE_FAILED
    assert row["outcome"]["recovered_amount_paise"] is None
    assert row["execution"]["detail"] == "razorpay_api_error"


# ---------------------------------------------------------------------------
# Missing / malformed state
# ---------------------------------------------------------------------------


def test_the_projection_is_pure_and_tolerates_absent_records() -> None:
    row = build_queue_row(
        {"event_id": "evt_bare", "amount_paise": 100, "timestamp": NOW.isoformat()},
        None,
        [],
        [],
        [],
        {},
    )
    assert row["lifecycle_state"] == STATE_NOT_CLASSIFIED
    assert row["selection"] is None
    assert row["execution"] is None
    assert row["customer_id"] is None
    assert row["diagnosis"] is None


def test_a_real_link_execution_without_a_link_id_is_never_recovered() -> None:
    """A malformed REAL_RAZORPAY success carrying no link id cannot correlate."""
    row = build_queue_row(
        {"event_id": "evt_nolink", "amount_paise": 100, "timestamp": NOW.isoformat()},
        None,
        [],
        [],
        [
            {
                "event_id": "evt_nolink",
                "intervention": "payment_link",
                "execution_mode": "REAL_RAZORPAY",
                "status": "SUCCESS",
                "payment_link_id": None,
                "external_reference": None,
                "detail": None,
                "reported_at": NOW.isoformat(),
            }
        ],
        {"plink_x": {"amount_paid_paise": 1, "payment_link_id": "plink_x"}},
    )
    assert row["lifecycle_state"] == STATE_PENDING_OUTCOME
    assert row["outcome"]["recovered_amount_paise"] is None


def test_a_no_action_selection_does_not_count_as_selected(db_conn) -> None:
    insert_payment_event(db_conn, _event("evt_noact"))
    insert_classification_result(db_conn, _classification("evt_noact"))
    insert_policy_decision(db_conn, _decision("evt_noact", "retry_delayed", True))
    insert_optimizer_decision(
        db_conn,
        OptimizerDecisionRecord(
            event_id="evt_noact",
            decided_at=NOW.isoformat(),
            selected_intervention="no_action",
            selection_reason="no candidate had positive expected value",
            candidates_considered=("retry_delayed",),
            allowed_candidates=("retry_delayed",),
            evaluations=(),
        ),
    )
    row = _row(db_conn, "evt_noact")
    assert row["lifecycle_state"] == STATE_POLICY_ALLOWED
    assert row["selection"]["selected_intervention"] == "no_action"
    assert row["selection"]["expected_value_paise"] is None


def test_a_missing_event_projects_to_none(db_conn) -> None:
    assert build_queue_row_for_event(db_conn, "evt_ghost") is None


# ---------------------------------------------------------------------------
# Queue: filtering, sorting, counts
# ---------------------------------------------------------------------------


def _seed_mixed_workload(conn) -> None:
    insert_payment_event(
        conn, _event("evt_a", amount_paise=10_000, timestamp=(NOW - timedelta(hours=3)).isoformat())
    )
    insert_payment_event(
        conn,
        _event(
            "evt_b",
            amount_paise=90_000,
            failure_reason="insufficient_funds",
            timestamp=(NOW - timedelta(hours=2)).isoformat(),
        ),
    )
    insert_payment_event(
        conn,
        _event(
            "evt_c",
            amount_paise=30_000,
            risk_flag="fraud_suspect",
            timestamp=(NOW - timedelta(hours=1)).isoformat(),
        ),
    )
    for event_id in ("evt_a", "evt_b", "evt_c"):
        insert_classification_result(conn, _classification(event_id))
    insert_policy_decision(conn, _decision("evt_a", "retry_delayed", True))
    insert_optimizer_decision(conn, _optimizer_record("evt_a", "retry_delayed", 8_000))
    insert_execution_outcome(conn, _execution("evt_a"))
    insert_policy_decision(conn, _decision("evt_b", "payment_link", True))
    insert_optimizer_decision(conn, _optimizer_record("evt_b", "payment_link", 25_000))
    insert_execution_outcome(
        conn,
        _execution(
            "evt_b",
            intervention="payment_link",
            mode="REAL_RAZORPAY",
            payment_link_id="plink_b",
        ),
    )
    insert_policy_decision(
        conn, _decision("evt_c", "retry_delayed", False, reason="fraud_protection")
    )


def test_the_queue_reports_every_lifecycle_state_count(db_conn) -> None:
    _seed_mixed_workload(db_conn)
    payload = build_recovery_queue(db_conn)
    assert payload["count"] == 3
    assert payload["state_counts"][STATE_EXECUTED] == 1
    assert payload["state_counts"][STATE_PENDING_OUTCOME] == 1
    assert payload["state_counts"][STATE_BLOCKED] == 1
    assert payload["state_counts"][STATE_RECOVERED] == 0


def test_the_queue_filters_by_lifecycle_state(db_conn) -> None:
    _seed_mixed_workload(db_conn)
    payload = build_recovery_queue(db_conn, lifecycle_state=STATE_BLOCKED)
    assert [row["event_id"] for row in payload["rows"]] == ["evt_c"]
    assert payload["filters"]["lifecycle_state"] == STATE_BLOCKED


def test_the_queue_filters_by_execution_mode(db_conn) -> None:
    _seed_mixed_workload(db_conn)
    payload = build_recovery_queue(db_conn, execution_mode="REAL_RAZORPAY")
    assert [row["event_id"] for row in payload["rows"]] == ["evt_b"]


def test_the_queue_filters_by_risk_flag_and_failure_reason(db_conn) -> None:
    _seed_mixed_workload(db_conn)
    assert [
        row["event_id"] for row in build_recovery_queue(db_conn, risk_flag="fraud_suspect")["rows"]
    ] == ["evt_c"]
    assert [
        row["event_id"]
        for row in build_recovery_queue(db_conn, failure_reason="insufficient_funds")["rows"]
    ] == ["evt_b"]


def test_the_queue_filters_by_intervention_and_policy_status(db_conn) -> None:
    _seed_mixed_workload(db_conn)
    assert [
        row["event_id"] for row in build_recovery_queue(db_conn, intervention="payment_link")["rows"]
    ] == ["evt_b"]
    assert [
        row["event_id"] for row in build_recovery_queue(db_conn, policy_status=POLICY_BLOCKED)["rows"]
    ] == ["evt_c"]


def test_the_queue_sorts_deterministically(db_conn) -> None:
    _seed_mixed_workload(db_conn)
    newest = [row["event_id"] for row in build_recovery_queue(db_conn)["rows"]]
    assert newest == ["evt_c", "evt_b", "evt_a"]
    by_amount = [
        row["event_id"] for row in build_recovery_queue(db_conn, sort=SORT_AMOUNT_DESC)["rows"]
    ]
    assert by_amount == ["evt_b", "evt_c", "evt_a"]
    by_value = [
        row["event_id"]
        for row in build_recovery_queue(db_conn, sort=SORT_EXPECTED_RECOVERY_DESC)["rows"]
    ]
    assert by_value == ["evt_b", "evt_a", "evt_c"]
    pending_first = [
        row["event_id"]
        for row in build_recovery_queue(db_conn, sort=SORT_OLDEST_PENDING_OUTCOME)["rows"]
    ]
    assert pending_first[0] == "evt_b"


def test_repeated_queue_reads_are_identical(db_conn) -> None:
    _seed_mixed_workload(db_conn)
    first = build_recovery_queue(db_conn, sort=SORT_AMOUNT_DESC)["rows"]
    second = build_recovery_queue(db_conn, sort=SORT_AMOUNT_DESC)["rows"]
    assert json.dumps(first, sort_keys=True) == json.dumps(second, sort_keys=True)


def test_the_queue_respects_the_limit(db_conn) -> None:
    _seed_mixed_workload(db_conn)
    payload = build_recovery_queue(db_conn, limit=1)
    assert payload["count"] == 1
    assert payload["total_matched"] == 3


@pytest.mark.parametrize(
    "kwargs", [{"lifecycle_state": "SOMETHING_ELSE"}, {"sort": "by_vibes"}]
)
def test_an_unknown_filter_or_sort_is_rejected(db_conn, kwargs) -> None:
    with pytest.raises(RecoveryQueueError):
        build_recovery_queue(db_conn, **kwargs)


def test_the_queue_never_exposes_hidden_world_or_benchmark_state(db_conn) -> None:
    _seed_mixed_workload(db_conn)
    payload = json.dumps(build_recovery_queue(db_conn))
    for forbidden in ("hidden", "oracle", "true_probability", "regret", "recovery_draw"):
        assert forbidden not in payload.lower()

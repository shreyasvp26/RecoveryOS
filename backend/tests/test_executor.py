"""Phase 7 executor tests: outcomes, simulated execution, authorization."""

from __future__ import annotations

import pytest

from app.executor import (
    ExecutionAuthorizationError,
    ExecutionOutcome,
    ExecutionRejectedError,
    SIMULATED_INTERVENTIONS,
    BoundedExecutor,
)
from app.models import CustomerHistory, PaymentEvent
from app.policy import PolicyDecision
from app.selector import NO_ACTION

VALID_EVALUATED_AT = "2026-08-27T13:00:00+00:00"


def _event(event_id: str = "evt_exec") -> PaymentEvent:
    return PaymentEvent.from_dict(
        {
            "event_id": event_id,
            "order_id": "order_exec",
            "payment_id": "pay_exec",
            "customer_id": "cust_exec",
            "amount_paise": 75000,
            "currency": "INR",
            "payment_method": "card",
            "failure_reason": "bank_timeout",
            "bank": "HDFC",
            "risk_flag": "normal",
            "customer_history": CustomerHistory(4, 1, True).to_dict(),
            "timestamp": "2026-08-27T12:00:00+00:00",
        }
    )


def _decision(
    intervention: str,
    event_id: str = "evt_exec",
    allowed: bool = True,
) -> PolicyDecision:
    return PolicyDecision.from_dict(
        {
            "event_id": event_id,
            "proposed_intervention": intervention,
            "allowed": allowed,
            "denial_reason": None,
            "policy_rules_applied": [
                "fraud_check_passed",
                "terminal_check_passed",
                "duplicate_check_passed",
                "retry_limit_passed",
                "cooldown_check_passed",
                "spend_cap_passed",
            ],
            "evaluated_at": VALID_EVALUATED_AT,
        }
    )


@pytest.mark.parametrize(
    "intervention",
    ["retry_immediate", "retry_delayed", "reminder", "alternate_method_prompt"],
)
def test_simulated_execution_reports_operation_success(intervention: str) -> None:
    event = _event()
    outcome = BoundedExecutor().execute(event, intervention, _decision(intervention))
    assert outcome.event_id == event.event_id
    assert outcome.intervention == intervention
    assert outcome.execution_mode == "SIMULATED"
    assert outcome.status == "SUCCESS"
    assert outcome.external_reference is None
    assert outcome.reported_at == VALID_EVALUATED_AT


def test_simulated_interventions_are_explicit() -> None:
    assert SIMULATED_INTERVENTIONS == frozenset(
        {"retry_immediate", "retry_delayed", "reminder", "alternate_method_prompt"}
    )


def test_execution_outcome_mode_coupling_is_structural() -> None:
    with pytest.raises(ExecutionRejectedError):
        ExecutionOutcome(
            event_id="evt_exec",
            intervention="retry_delayed",
            execution_mode="REAL_RAZORPAY",
            status="SUCCESS",
            reported_at=VALID_EVALUATED_AT,
        )
    with pytest.raises(ExecutionRejectedError):
        ExecutionOutcome(
            event_id="evt_exec",
            intervention="payment_link",
            execution_mode="SIMULATED",
            status="SUCCESS",
            reported_at=VALID_EVALUATED_AT,
        )


def test_mandatory_authorization_denied_decision_never_executes() -> None:
    event = _event()
    denied = PolicyDecision.from_dict(
        {
            "event_id": "evt_exec",
            "proposed_intervention": "retry_delayed",
            "allowed": False,
            "denial_reason": "fraud_protection",
            "policy_rules_applied": ["fraud_protection"],
            "evaluated_at": VALID_EVALUATED_AT,
        }
    )
    with pytest.raises(ExecutionAuthorizationError):
        BoundedExecutor().execute(event, "retry_delayed", denied)


def test_execution_requires_a_policy_decision_object() -> None:
    with pytest.raises(ExecutionAuthorizationError):
        BoundedExecutor().execute(_event(), "retry_delayed", {"allowed": True})


def test_execution_requires_event_id_match() -> None:
    event = _event()
    wrong = _decision("retry_delayed", event_id="evt_other")
    with pytest.raises(ExecutionAuthorizationError):
        BoundedExecutor().execute(event, "retry_delayed", wrong)


def test_execution_requires_intervention_match() -> None:
    event = _event()
    decision = _decision("payment_link")
    with pytest.raises(ExecutionAuthorizationError):
        BoundedExecutor().execute(event, "retry_delayed", decision)


def test_no_action_is_never_executable() -> None:
    event = _event()
    decision = _decision(NO_ACTION)
    with pytest.raises(ExecutionRejectedError):
        BoundedExecutor().execute(event, NO_ACTION, decision)


def test_unknown_intervention_is_rejected() -> None:
    event = _event()
    decision = _decision("retry_delayed")
    with pytest.raises(ExecutionRejectedError):
        BoundedExecutor().execute(event, "wire_transfer", decision)


def test_payment_link_requires_configured_client() -> None:
    event = _event()
    decision = _decision("payment_link")
    with pytest.raises(ExecutionRejectedError):
        BoundedExecutor().execute(event, "payment_link", decision)


def test_outcome_rejects_naive_timestamp() -> None:
    with pytest.raises(ExecutionRejectedError):
        ExecutionOutcome(
            event_id="evt_exec",
            intervention="retry_delayed",
            execution_mode="SIMULATED",
            status="SUCCESS",
            reported_at="2026-08-27T13:00:00",
        )

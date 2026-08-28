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
from app.razorpay_client import (
    PaymentLinkResult,
    RazorpayExecutionError,
)
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


def test_payment_link_requires_configured_client() -> None:
    event = _event()
    decision = _decision("payment_link")
    outcome = BoundedExecutor().execute(event, "payment_link", decision, razorpay_client=None)
    assert outcome.execution_mode == "REAL_RAZORPAY"
    assert outcome.status == "FAILED"
    assert "configuration_missing" in outcome.detail


def test_payment_link_execution_through_real_razorpay_mode() -> None:
    event = _event()
    decision = _decision("payment_link")
    client = StubPaymentLinkClient(
        result=PaymentLinkResult(id="plink_xyz", short_url="https://rzp.io/l/real123")
    )
    outcome = BoundedExecutor().execute(
        event, "payment_link", decision, razorpay_client=client
    )
    assert outcome.execution_mode == "REAL_RAZORPAY"
    assert outcome.status == "SUCCESS"
    assert outcome.external_reference == "https://rzp.io/l/real123"
    call = client.calls[0]
    assert call["amount_paise"] == event.amount_paise
    assert call["currency"] == event.currency
    assert call["reference_id"] == "evtexec"
    assert "order_exec" in call["description"]


def test_payment_link_provider_failure_is_failed_not_success() -> None:
    event = _event()
    decision = _decision("payment_link")
    client = StubPaymentLinkClient(error=RazorpayExecutionError("razorpay_api_error: rejected"))
    outcome = BoundedExecutor().execute(
        event, "payment_link", decision, razorpay_client=client
    )
    assert outcome.execution_mode == "REAL_RAZORPAY"
    assert outcome.status == "FAILED"
    assert "razorpay_api_error" in outcome.detail
    assert outcome.external_reference is None


def test_payment_link_unexpected_response_is_failed_not_success() -> None:
    from app.razorpay_client import RazorpayUnexpectedResponseError

    event = _event()
    decision = _decision("payment_link")
    client = StubPaymentLinkClient(
        error=RazorpayUnexpectedResponseError("razorpay_api_unexpected_response: no url")
    )
    outcome = BoundedExecutor().execute(
        event, "payment_link", decision, razorpay_client=client
    )
    assert outcome.status == "FAILED"
    assert "unexpected_response" in outcome.detail
    assert outcome.external_reference is None


def test_payment_link_unexpected_client_exception_is_explicit_failure() -> None:
    event = _event()
    decision = _decision("payment_link")
    client = StubPaymentLinkClient(error=RuntimeError("boundary leaked"))
    outcome = BoundedExecutor().execute(
        event, "payment_link", decision, razorpay_client=client
    )
    assert outcome.status == "FAILED"
    assert "razorpay_api_error" in outcome.detail
    # The raw provider exception text must not become user/audit-facing detail.
    assert "boundary leaked" not in outcome.detail
    assert outcome.external_reference is None


def test_payment_link_provider_exception_text_never_escapes_detail() -> None:
    event = _event()
    decision = _decision("payment_link")
    marker = "SECRET_SHOULD_NOT_ESCAPE"
    client = StubPaymentLinkClient(error=RuntimeError(marker))
    outcome = BoundedExecutor().execute(
        event, "payment_link", decision, razorpay_client=client
    )
    assert outcome.status == "FAILED"
    assert outcome.detail == "razorpay_api_error"
    assert marker not in (outcome.detail or "")
    assert outcome.external_reference is None


def test_payment_link_failure_never_fabricates_url() -> None:
    event = _event()
    decision = _decision("payment_link")
    client = StubPaymentLinkClient(error=RazorpayExecutionError("razorpay_api_error: down"))
    outcome = BoundedExecutor().execute(
        event, "payment_link", decision, razorpay_client=client
    )
    assert outcome.external_reference is None
    assert outcome.status == "FAILED"


def test_outcome_rejects_naive_timestamp() -> None:
    with pytest.raises(ExecutionRejectedError):
        ExecutionOutcome(
            event_id="evt_exec",
            intervention="retry_delayed",
            execution_mode="SIMULATED",
            status="SUCCESS",
            reported_at="2026-08-27T13:00:00",
        )

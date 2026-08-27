"""Phase 6 tests for the deterministic policy contract."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from app.models import CustomerHistory, PaymentEvent
from app.policy import (
    DETERMINISTIC_RULE_ORDER,
    INTERVENTION_ATTEMPT_STATUSES,
    PolicyDecision,
    PolicyHistory,
    PolicyInput,
    PolicyValidationError,
    InterventionAttempt,
    parse_aware_datetime,
    PolicyConfig,
)

T = timezone.utc


def make_event(event_id: str = "evt_1", risk_flag: str = "normal") -> PaymentEvent:
    return PaymentEvent(
        event_id=event_id,
        order_id="order_1",
        payment_id="pay_1",
        customer_id="cust_1",
        amount_paise=75000,
        currency="INR",
        payment_method="card",
        failure_reason="bank_timeout",
        bank="HDFC",
        risk_flag=risk_flag,
        customer_history=CustomerHistory(
            prior_successful_payments=4,
            prior_failed_payments=1,
            has_active_subscription=True,
        ),
        timestamp="2026-08-27T12:00:00+00:00",
    )


def decision_dict(**overrides) -> dict:
    base = {
        "event_id": "evt_1",
        "proposed_intervention": "retry_delayed",
        "allowed": True,
        "denial_reason": None,
        "policy_rules_applied": [
            "fraud_check_passed",
            "terminal_check_passed",
            "duplicate_check_passed",
            "retry_limit_passed",
            "cooldown_check_passed",
            "spend_cap_passed",
        ],
        "evaluated_at": "2026-08-27T13:00:00+00:00",
    }
    base.update(overrides)
    return base


def test_rule_order_is_deterministic_and_documented() -> None:
    assert DETERMINISTIC_RULE_ORDER == (
        "fraud_protection",
        "terminal_failure",
        "duplicate_intervention",
        "customer_intervention_limit_exceeded",
        "event_cooldown_active",
        "spend_cap_exceeded",
    )


def test_attempt_statuses_are_locked() -> None:
    assert INTERVENTION_ATTEMPT_STATUSES == {"attempted", "failed", "successful"}


def test_policy_decision_round_trip_preserves_contract() -> None:
    data = decision_dict()
    decision = PolicyDecision.from_dict(data)
    assert decision.to_dict() == data
    assert decision.allowed is True
    assert decision.denial_reason is None


def test_denied_decision_round_trip_preserves_reason() -> None:
    data = decision_dict(
        allowed=False,
        denial_reason="customer_intervention_limit_exceeded",
        policy_rules_applied=["max_2_interventions_24h"],
    )
    decision = PolicyDecision.from_dict(data)
    assert decision.to_dict() == data
    assert decision.allowed is False
    assert decision.denial_reason == "customer_intervention_limit_exceeded"


def test_allowed_decision_must_not_carry_denial_reason() -> None:
    with pytest.raises(PolicyValidationError):
        PolicyDecision.from_dict(decision_dict(denial_reason="fraud_protection"))


def test_denied_decision_requires_explicit_reason() -> None:
    with pytest.raises(PolicyValidationError):
        PolicyDecision.from_dict(
            decision_dict(allowed=False, denial_reason=None)
        )


def test_denied_decision_with_empty_reason_is_invalid() -> None:
    with pytest.raises(PolicyValidationError):
        PolicyDecision.from_dict(
            decision_dict(allowed=False, denial_reason="   ")
        )


def test_decision_rejects_unexpected_fields() -> None:
    with pytest.raises(PolicyValidationError):
        PolicyDecision.from_dict(decision_dict(extra_field=True))


def test_decision_rejects_missing_fields() -> None:
    data = decision_dict()
    del data["event_id"]
    with pytest.raises(PolicyValidationError):
        PolicyDecision.from_dict(data)


def test_decision_rejects_invalid_intervention() -> None:
    with pytest.raises(PolicyValidationError):
        PolicyDecision.from_dict(
            decision_dict(proposed_intervention="wire_transfer")
        )


def test_decision_rejects_empty_policy_rules() -> None:
    data = decision_dict(
        allowed=False,
        denial_reason="fraud_protection",
        policy_rules_applied=[],
    )
    with pytest.raises(PolicyValidationError):
        PolicyDecision.from_dict(data)


def test_decision_rejects_all_blank_policy_rules() -> None:
    data = decision_dict(policy_rules_applied=["   "])
    with pytest.raises(PolicyValidationError):
        PolicyDecision.from_dict(data)


def test_decision_rejects_naive_evaluated_at() -> None:
    with pytest.raises(PolicyValidationError):
        PolicyDecision.from_dict(
            decision_dict(evaluated_at="2026-08-27T13:00:00")
        )


def test_intervention_attempt_round_trip() -> None:
    data = {
        "event_id": "evt_1",
        "intervention": "retry_delayed",
        "customer_id": "cust_1",
        "cost_paise": 0,
        "attempted_at": "2026-08-27T12:30:00+00:00",
        "status": "attempted",
    }
    attempt = InterventionAttempt.from_dict(data)
    assert attempt.to_dict() == data


def test_intervention_attempt_validates_status_and_intervention() -> None:
    with pytest.raises(PolicyValidationError):
        InterventionAttempt.from_dict(
            {
                "event_id": "evt_1",
                "intervention": "retry_delayed",
                "customer_id": "cust_1",
                "cost_paise": 0,
                "attempted_at": "2026-08-27T12:30:00+00:00",
                "status": "executed_twice",
            }
        )
    with pytest.raises(PolicyValidationError):
        InterventionAttempt.from_dict(
            {
                "event_id": "evt_1",
                "intervention": "wire_transfer",
                "customer_id": "cust_1",
                "cost_paise": 0,
                "attempted_at": "2026-08-27T12:30:00+00:00",
                "status": "attempted",
            }
        )


def test_parse_aware_datetime_requires_aware() -> None:
    with pytest.raises(PolicyValidationError):
        parse_aware_datetime("2026-08-27T12:30:00")
    parsed = parse_aware_datetime("2026-08-27T12:30:00+05:30")
    assert parsed.tzinfo is not None


def test_policy_history_validates_facts() -> None:
    with pytest.raises(PolicyValidationError):
        PolicyHistory(
            customer_intervention_count_24h=-1,
            most_recent_event_intervention_time=None,
            has_successful_intervention=False,
            existing_daily_spend_paise=0,
        )


def test_policy_input_requires_matching_event_ids() -> None:
    event = make_event()
    from app.classification import ClassificationResult

    classification = ClassificationResult.from_dict(
        {
            "event_id": "evt_999",
            "root_cause_category": "transient",
            "confidence": 0.9,
            "reasoning": "r",
            "candidate_interventions": ["retry_delayed"],
        }
    )
    with pytest.raises(PolicyValidationError):
        PolicyInput(
            event=event,
            classification=classification,
            proposed_intervention="retry_delayed",
            history=PolicyHistory(
                customer_intervention_count_24h=0,
                most_recent_event_intervention_time=None,
                has_successful_intervention=False,
                existing_daily_spend_paise=0,
            ),
            evaluation_time=datetime(2026, 8, 27, 13, 0, tzinfo=T),
        )


def test_policy_input_requires_aware_evaluation_time() -> None:
    event = make_event()
    from app.classification import ClassificationResult

    classification = ClassificationResult.from_dict(
        {
            "event_id": "evt_1",
            "root_cause_category": "transient",
            "confidence": 0.9,
            "reasoning": "r",
            "candidate_interventions": ["retry_delayed"],
        }
    )
    with pytest.raises(PolicyValidationError):
        PolicyInput(
            event=event,
            classification=classification,
            proposed_intervention="retry_delayed",
            history=PolicyHistory(
                customer_intervention_count_24h=0,
                most_recent_event_intervention_time=None,
                has_successful_intervention=False,
                existing_daily_spend_paise=0,
            ),
            evaluation_time=datetime(2026, 8, 27, 13, 0),
        )


def test_policy_config_validates_values() -> None:
    with pytest.raises(PolicyValidationError):
        PolicyConfig(event_cooldown_minutes=0)
    with pytest.raises(PolicyValidationError):
        PolicyConfig(daily_spend_cap_paise=-1)
    with pytest.raises(PolicyValidationError):
        PolicyConfig(max_interventions_per_customer_24h=0)
    assert PolicyConfig().daily_spend_cap_paise == 5_000_000


def test_policy_config_defaults_match_required_configuration() -> None:
    config = PolicyConfig()
    assert config.max_interventions_per_customer_24h == 2
    assert config.event_cooldown_minutes == 30
    assert config.intervention_cost("retry_delayed") == 0
    assert config.intervention_cost("payment_link") == 0

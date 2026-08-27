"""Phase 6 tests for the deterministic policy rules.

Covers every locked rule, boundary conditions, rule ordering, determinism,
and the adversarial requirement that no unauthorized path returns allowed.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from app.classification import ClassificationResult
from app.models import CustomerHistory, PaymentEvent
from app.policy import (
    PolicyConfig,
    PolicyEngine,
    PolicyHistory,
    PolicyInput,
    PolicyValidationError,
)

T = timezone.utc
ENGINE = PolicyEngine()

BASE_TS = datetime(2026, 8, 27, 12, 0, tzinfo=T)


def make_event(risk_flag: str = "normal", event_id: str = "evt_1") -> PaymentEvent:
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


def make_classification(
    root_cause: str = "transient",
    interventions: list[str] | None = None,
    event_id: str = "evt_1",
) -> ClassificationResult:
    return ClassificationResult.from_dict(
        {
            "event_id": event_id,
            "root_cause_category": root_cause,
            "confidence": 0.9,
            "reasoning": "reasoning",
            "candidate_interventions": interventions or ["retry_delayed"],
        }
    )


def make_input(
    *,
    risk_flag: str = "normal",
    root_cause: str = "transient",
    proposed: str = "retry_delayed",
    count_24h: int = 0,
    recent: datetime | None = None,
    successful: bool = False,
    spend_paise: int = 0,
    evaluation_time: datetime = BASE_TS + timedelta(hours=1),
) -> PolicyInput:
    return PolicyInput(
        event=make_event(risk_flag=risk_flag),
        classification=make_classification(root_cause=root_cause),
        proposed_intervention=proposed,
        history=PolicyHistory(
            customer_intervention_count_24h=count_24h,
            most_recent_event_intervention_time=recent,
            has_successful_intervention=successful,
            existing_daily_spend_paise=spend_paise,
        ),
        evaluation_time=evaluation_time,
    )


def test_normal_event_is_allowed_with_all_checks_passed() -> None:
    decision = ENGINE.evaluate(make_input(), PolicyConfig())
    assert decision.allowed is True
    assert decision.denial_reason is None
    assert decision.policy_rules_applied == (
        "fraud_check_passed",
        "terminal_check_passed",
        "duplicate_check_passed",
        "retry_limit_passed",
        "cooldown_check_passed",
        "spend_cap_passed",
    )


def test_fraud_suspect_is_denied() -> None:
    decision = ENGINE.evaluate(
        make_input(risk_flag="fraud_suspect"), PolicyConfig()
    )
    assert decision.allowed is False
    assert decision.denial_reason == "fraud_protection"
    assert decision.policy_rules_applied == ("fraud_protection",)


def test_terminal_failure_is_denied() -> None:
    decision = ENGINE.evaluate(
        make_input(root_cause="terminal"), PolicyConfig()
    )
    assert decision.allowed is False
    assert decision.denial_reason == "terminal_failure"


def test_invalid_intervention_is_fail_closed_controlled_error() -> None:
    with pytest.raises(PolicyValidationError):
        ENGINE.evaluate(make_input(proposed="wire_transfer"), PolicyConfig())


def test_customer_zero_previous_allows() -> None:
    assert ENGINE.evaluate(
        make_input(count_24h=0), PolicyConfig()
    ).allowed is True


def test_customer_one_previous_allows() -> None:
    assert ENGINE.evaluate(
        make_input(count_24h=1), PolicyConfig()
    ).allowed is True


def test_customer_two_previous_denies() -> None:
    decision = ENGINE.evaluate(make_input(count_24h=2), PolicyConfig())
    assert decision.allowed is False
    assert decision.denial_reason == "customer_intervention_limit_exceeded"


def test_customer_third_intervention_denies() -> None:
    decision = ENGINE.evaluate(
        make_input(
            count_24h=2,
            proposed="payment_link",
        ),
        PolicyConfig(),
    )
    assert decision.allowed is False
    assert decision.denial_reason == "customer_intervention_limit_exceeded"


def test_customer_limit_is_configurable() -> None:
    config = PolicyConfig(max_interventions_per_customer_24h=5)
    assert ENGINE.evaluate(
        make_input(count_24h=5), config
    ).denial_reason == "customer_intervention_limit_exceeded"
    assert ENGINE.evaluate(
        make_input(count_24h=4), config
    ).allowed is True


def test_cooldown_ten_minutes_denies() -> None:
    recent = make_input().evaluation_time - timedelta(minutes=10)
    decision = ENGINE.evaluate(
        make_input(recent=recent), PolicyConfig()
    )
    assert decision.allowed is False
    assert decision.denial_reason == "event_cooldown_active"


def test_cooldown_twenty_nine_minutes_denies() -> None:
    recent = make_input().evaluation_time - timedelta(minutes=29)
    assert ENGINE.evaluate(
        make_input(recent=recent), PolicyConfig()
    ).denial_reason == "event_cooldown_active"


def test_cooldown_exactly_thirty_minutes_allows() -> None:
    recent = make_input().evaluation_time - timedelta(minutes=30)
    decision = ENGINE.evaluate(make_input(recent=recent), PolicyConfig())
    assert decision.allowed is True
    assert "cooldown_check_passed" in decision.policy_rules_applied


def test_cooldown_thirty_plus_minutes_allows() -> None:
    recent = make_input().evaluation_time - timedelta(minutes=31)
    assert ENGINE.evaluate(
        make_input(recent=recent), PolicyConfig()
    ).allowed is True


def test_cooldown_is_configurable() -> None:
    config = PolicyConfig(event_cooldown_minutes=45)
    recent = make_input().evaluation_time - timedelta(minutes=40)
    assert ENGINE.evaluate(
        make_input(recent=recent), config
    ).denial_reason == "event_cooldown_active"


def test_duplicate_successful_intervention_denies() -> None:
    decision = ENGINE.evaluate(
        make_input(successful=True), PolicyConfig()
    )
    assert decision.allowed is False
    assert decision.denial_reason == "duplicate_intervention"


def test_failed_prior_intervention_is_not_a_duplicate() -> None:
    decision = ENGINE.evaluate(
        make_input(successful=False, count_24h=1), PolicyConfig()
    )
    assert decision.allowed is True
    assert "duplicate_check_passed" in decision.policy_rules_applied


def test_attempted_prior_intervention_is_not_a_duplicate() -> None:
    decision = ENGINE.evaluate(
        make_input(successful=False), PolicyConfig()
    )
    assert decision.allowed is True


def test_spend_below_cap_allows() -> None:
    config = PolicyConfig(daily_spend_cap_paise=1000)
    decision = ENGINE.evaluate(
        make_input(spend_paise=500), config
    )
    assert decision.allowed is True
    assert "spend_cap_passed" in decision.policy_rules_applied


def test_spend_exactly_at_cap_allows() -> None:
    config = PolicyConfig(daily_spend_cap_paise=1000)
    assert ENGINE.evaluate(
        make_input(spend_paise=1000), config
    ).allowed is True


def test_spend_exceeding_cap_denies() -> None:
    config = PolicyConfig(daily_spend_cap_paise=1000)
    decision = ENGINE.evaluate(
        make_input(spend_paise=1001), config
    )
    assert decision.allowed is False
    assert decision.denial_reason == "spend_cap_exceeded"


def test_spend_cap_custom_config_changes_behavior() -> None:
    strict = PolicyConfig(daily_spend_cap_paise=10)
    relaxed = PolicyConfig(daily_spend_cap_paise=100000)
    assert ENGINE.evaluate(
        make_input(spend_paise=20), strict
    ).denial_reason == "spend_cap_exceeded"
    assert ENGINE.evaluate(
        make_input(spend_paise=20), relaxed
    ).allowed is True


def test_zero_cost_intervention_still_passes_through_spend_evaluation() -> None:
    config = PolicyConfig(
        daily_spend_cap_paise=1000,
        intervention_cost_paise={"retry_delayed": 0, "payment_link": 0},
    )
    denied = ENGINE.evaluate(make_input(spend_paise=1500), config)
    assert denied.denial_reason == "spend_cap_exceeded"
    allowed = ENGINE.evaluate(make_input(spend_paise=0), config)
    assert allowed.allowed is True
    assert "spend_cap_passed" in allowed.policy_rules_applied


def test_proposed_intervention_cost_counts_toward_cap() -> None:
    config = PolicyConfig(
        daily_spend_cap_paise=1000,
        intervention_cost_paise={"payment_link": 400},
    )
    assert ENGINE.evaluate(
        make_input(proposed="payment_link", spend_paise=599), config
    ).allowed is True
    assert ENGINE.evaluate(
        make_input(proposed="payment_link", spend_paise=600), config
    ).allowed is True
    assert ENGINE.evaluate(
        make_input(proposed="payment_link", spend_paise=601), config
    ).denial_reason == "spend_cap_exceeded"


def test_fraud_is_evaluated_before_spend() -> None:
    config = PolicyConfig(daily_spend_cap_paise=0)
    decision = ENGINE.evaluate(
        make_input(risk_flag="fraud_suspect", spend_paise=5000), config
    )
    assert decision.denial_reason == "fraud_protection"


def test_fraud_is_evaluated_before_customer_limit() -> None:
    decision = ENGINE.evaluate(
        make_input(risk_flag="fraud_suspect", count_24h=2), PolicyConfig()
    )
    assert decision.denial_reason == "fraud_protection"


def test_terminal_is_evaluated_before_cooldown() -> None:
    recent = make_input().evaluation_time - timedelta(minutes=5)
    decision = ENGINE.evaluate(
        make_input(root_cause="terminal", recent=recent), PolicyConfig()
    )
    assert decision.denial_reason == "terminal_failure"


def test_terminal_is_evaluated_before_duplicate() -> None:
    decision = ENGINE.evaluate(
        make_input(root_cause="terminal", successful=True), PolicyConfig()
    )
    assert decision.denial_reason == "terminal_failure"


def test_fraud_is_evaluated_before_terminal() -> None:
    decision = ENGINE.evaluate(
        make_input(risk_flag="fraud_suspect", root_cause="terminal"),
        PolicyConfig(),
    )
    assert decision.denial_reason == "fraud_protection"


def test_duplicate_is_evaluated_before_customer_limit() -> None:
    decision = ENGINE.evaluate(
        make_input(successful=True, count_24h=2), PolicyConfig()
    )
    assert decision.denial_reason == "duplicate_intervention"


def test_cooldown_is_evaluated_before_spend() -> None:
    config = PolicyConfig(daily_spend_cap_paise=0)
    recent = make_input().evaluation_time - timedelta(minutes=5)
    decision = ENGINE.evaluate(
        make_input(recent=recent, spend_paise=9000), config
    )
    assert decision.denial_reason == "event_cooldown_active"


def test_multiple_candidates_are_evaluated_independently() -> None:
    config = PolicyConfig(
        daily_spend_cap_paise=1000,
        intervention_cost_paise={"payment_link": 2000},
    )
    first = ENGINE.evaluate(make_input(proposed="retry_delayed"), config)
    second = ENGINE.evaluate(make_input(proposed="payment_link"), config)
    assert first.allowed is True
    assert second.allowed is False
    assert second.denial_reason == "spend_cap_exceeded"


def test_evaluation_is_deterministic() -> None:
    config = PolicyConfig(daily_spend_cap_paise=1000)
    first = make_input(spend_paise=300)
    a = ENGINE.evaluate(first, config)
    b = ENGINE.evaluate(first, config)
    assert a == b
    assert a.to_dict() == b.to_dict()


def test_identical_inputs_across_time_axes_are_stable() -> None:
    config = PolicyConfig()
    a = ENGINE.evaluate(make_input(evaluation_time=BASE_TS), config)
    b = ENGINE.evaluate(make_input(evaluation_time=BASE_TS), config)
    assert a.to_dict() == b.to_dict()
    assert a == b


def test_no_unauthorized_path_returns_allowed() -> None:
    scenarios = [
        make_input(risk_flag="fraud_suspect"),
        make_input(root_cause="terminal"),
        make_input(successful=True),
        make_input(count_24h=2),
        make_input(
            recent=make_input().evaluation_time - timedelta(minutes=10)
        ),
        make_input(spend_paise=10 ** 9),
    ]
    config = PolicyConfig(daily_spend_cap_paise=1_00_000)
    for input in scenarios:
        decision = ENGINE.evaluate(input, config)
        assert decision.allowed is False, (
            f"expected denial for {input.proposed_intervention!r} with "
            f"history {input.history}"
        )


def test_invalid_intervention_never_returns_allowed() -> None:
    with pytest.raises(PolicyValidationError):
        ENGINE.evaluate(make_input(proposed="wire_transfer"), PolicyConfig())

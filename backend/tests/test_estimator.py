"""Phase 16 tests: the deterministic recovery probability estimator."""

from __future__ import annotations

import pytest

from app.classification import ClassificationResult
from app.economics import (
    EXECUTABLE_INTERVENTIONS,
    PROBABILITY_SCALE,
    RecoveryProbability,
    UnsupportedInterventionError,
)
from app.estimator import (
    BASE_RECOVERY_BPS,
    FAILURE_REASON_ADJUSTMENT_BPS,
    PAYMENT_METHOD_ADJUSTMENT_BPS,
    ROOT_CAUSE_ADJUSTMENT_BPS,
    SUBSCRIPTION_ADJUSTMENT_BPS,
    EstimationError,
    RecoveryProbabilityEstimator,
    _saturate,
)
from app.models import CustomerHistory, PaymentEvent

EVENT_ID = "evt_estimator"


def _event(
    *,
    failure_reason: str = "bank_timeout",
    payment_method: str = "card",
    amount_paise: int = 75_000,
    bank: str = "HDFC",
    risk_flag: str = "normal",
    prior_successful: int = 4,
    prior_failed: int = 1,
    subscription: bool = True,
) -> PaymentEvent:
    return PaymentEvent(
        event_id=EVENT_ID,
        order_id="order_estimator",
        payment_id="pay_estimator",
        customer_id="cust_estimator",
        amount_paise=amount_paise,
        currency="INR",
        payment_method=payment_method,
        failure_reason=failure_reason,
        bank=bank,
        risk_flag=risk_flag,
        customer_history=CustomerHistory(
            prior_successful_payments=prior_successful,
            prior_failed_payments=prior_failed,
            has_active_subscription=subscription,
        ),
        timestamp="2026-08-27T12:00:00+00:00",
    )


def _classification(root: str = "transient") -> ClassificationResult:
    return ClassificationResult(
        event_id=EVENT_ID,
        root_cause_category=root,
        confidence=0.9,
        reasoning="advisory diagnosis for estimator tests",
        candidate_interventions=("retry_delayed", "payment_link"),
    )


ESTIMATOR = RecoveryProbabilityEstimator()


def _estimate(event: PaymentEvent, classification, intervention: str) -> int:
    return ESTIMATOR.estimate(event, classification, intervention).basis_points


# ---------------------------------------------------------------------------
# Determinism
# ---------------------------------------------------------------------------


def test_repeated_evaluation_returns_the_identical_probability() -> None:
    event, classification = _event(), _classification()
    for intervention in sorted(EXECUTABLE_INTERVENTIONS):
        results = {
            ESTIMATOR.estimate(event, classification, intervention)
            for _ in range(50)
        }
        assert len(results) == 1


def test_separate_estimator_instances_agree() -> None:
    event, classification = _event(), _classification()
    for intervention in sorted(EXECUTABLE_INTERVENTIONS):
        assert RecoveryProbabilityEstimator().estimate(
            event, classification, intervention
        ) == RecoveryProbabilityEstimator().estimate(
            event, classification, intervention
        )


def test_estimate_does_not_depend_on_event_identity() -> None:
    """Identifiers carry no signal and are the route ground truth could leak in."""
    base = _event()
    renamed = PaymentEvent.from_dict(
        dict(
            base.to_dict(),
            event_id="evt_completely_different",
            order_id="order_other",
            payment_id="pay_other",
            customer_id="cust_other",
        )
    )
    classification = _classification()
    renamed_classification = ClassificationResult.from_dict(
        dict(classification.to_dict(), event_id="evt_completely_different")
    )
    for intervention in sorted(EXECUTABLE_INTERVENTIONS):
        assert _estimate(base, classification, intervention) == _estimate(
            renamed, renamed_classification, intervention
        )


def test_estimate_does_not_depend_on_amount() -> None:
    """Amount enters the decision through EV, never twice through probability."""
    classification = _classification()
    for intervention in sorted(EXECUTABLE_INTERVENTIONS):
        assert _estimate(
            _event(amount_paise=500), classification, intervention
        ) == _estimate(
            _event(amount_paise=50_000_000), classification, intervention
        )


def test_estimate_does_not_depend_on_bank() -> None:
    """No bank-reliability data exists, so no bank coefficient may exist."""
    classification = _classification()
    for intervention in sorted(EXECUTABLE_INTERVENTIONS):
        assert _estimate(
            _event(bank="HDFC"), classification, intervention
        ) == _estimate(_event(bank="Yes Bank"), classification, intervention)


def test_estimate_does_not_depend_on_llm_confidence() -> None:
    """Confidence is a non-deterministic LLM output and must not be consumed."""
    event = _event()
    low = ClassificationResult.from_dict(
        dict(_classification().to_dict(), confidence=0.01)
    )
    high = ClassificationResult.from_dict(
        dict(_classification().to_dict(), confidence=1.0)
    )
    for intervention in sorted(EXECUTABLE_INTERVENTIONS):
        assert _estimate(event, low, intervention) == _estimate(
            event, high, intervention
        )


# ---------------------------------------------------------------------------
# Valid probability domain
# ---------------------------------------------------------------------------


def test_every_estimate_is_a_valid_probability() -> None:
    combinations = [
        (failure_reason, method, root, successes, failures, subscription)
        for failure_reason in (
            "bank_timeout",
            "insufficient_funds",
            "authentication_failed",
            "declined_by_bank",
            "expired_card",
            "transaction_declined",
            "payment_failed",
            "network_issue",
            "some_unmapped_reason",
        )
        for method in ("upi", "card", "netbanking", "wallet")
        for root in (
            "transient",
            "customer_action_needed",
            "fraud_suspect",
            "terminal",
        )
        for successes in (0, 4, 40)
        for failures in (0, 3, 6)
        for subscription in (True, False)
    ]
    for reason, method, root, successes, failures, subscription in combinations:
        event = _event(
            failure_reason=reason,
            payment_method=method,
            prior_successful=successes,
            prior_failed=failures,
            subscription=subscription,
        )
        classification = _classification(root)
        for intervention in sorted(EXECUTABLE_INTERVENTIONS):
            probability = ESTIMATOR.estimate(event, classification, intervention)
            assert isinstance(probability, RecoveryProbability)
            assert 0 <= probability.basis_points <= PROBABILITY_SCALE


def test_saturation_bounds_the_additive_score_at_both_endpoints() -> None:
    assert _saturate(-50_000) == 0
    assert _saturate(PROBABILITY_SCALE + 50_000) == PROBABILITY_SCALE
    assert _saturate(4_242) == 4_242


# ---------------------------------------------------------------------------
# Intervention-specific behaviour
# ---------------------------------------------------------------------------


def test_estimates_differ_between_interventions_for_the_same_event() -> None:
    event, classification = _event(), _classification()
    estimates = {
        intervention: _estimate(event, classification, intervention)
        for intervention in sorted(EXECUTABLE_INTERVENTIONS)
    }
    assert len(set(estimates.values())) > 1


def test_every_executable_intervention_has_a_base_rate() -> None:
    assert set(BASE_RECOVERY_BPS) == EXECUTABLE_INTERVENTIONS


# ---------------------------------------------------------------------------
# Representative known feature cases (the interpretable causal stories)
# ---------------------------------------------------------------------------


def test_transient_bank_timeout_prefers_a_delayed_retry_over_an_immediate_one() -> None:
    """Retrying instantly hits the same outage; retrying later does not."""
    event = _event(failure_reason="bank_timeout")
    classification = _classification("transient")
    assert _estimate(event, classification, "retry_delayed") > _estimate(
        event, classification, "retry_immediate"
    )


def test_expired_card_suppresses_retries_and_favours_a_different_instrument() -> None:
    """A dead instrument cannot be retried into life."""
    event = _event(failure_reason="expired_card", payment_method="card")
    classification = _classification("customer_action_needed")
    assert _estimate(event, classification, "alternate_method_prompt") > _estimate(
        event, classification, "retry_delayed"
    )
    assert _estimate(event, classification, "retry_immediate") < _estimate(
        event, classification, "payment_link"
    )


def test_customer_action_needed_favours_reaching_the_customer() -> None:
    event = _event(failure_reason="insufficient_funds")
    classification = _classification("customer_action_needed")
    assert _estimate(event, classification, "payment_link") > _estimate(
        event, classification, "retry_immediate"
    )
    assert _estimate(event, classification, "reminder") > _estimate(
        event, classification, "retry_immediate"
    )


def test_terminal_root_cause_suppresses_every_intervention() -> None:
    event = _event()
    for intervention in sorted(EXECUTABLE_INTERVENTIONS):
        assert _estimate(event, _classification("terminal"), intervention) < _estimate(
            event, _classification("transient"), intervention
        )


def test_a_reliable_payer_scores_higher_than_a_struggling_one() -> None:
    classification = _classification()
    reliable = _event(prior_successful=40, prior_failed=0)
    struggling = _event(prior_successful=0, prior_failed=6)
    for intervention in sorted(EXECUTABLE_INTERVENTIONS):
        assert _estimate(reliable, classification, intervention) > _estimate(
            struggling, classification, intervention
        )


def test_an_active_subscription_helps_automated_reattempts() -> None:
    """A stored mandate lets a retry succeed with no customer involvement."""
    classification = _classification()
    with_mandate = _event(subscription=True)
    without_mandate = _event(subscription=False)
    assert _estimate(with_mandate, classification, "retry_delayed") > _estimate(
        without_mandate, classification, "retry_delayed"
    )


def test_the_estimate_is_hand_calculable_from_the_documented_coefficients() -> None:
    """One fully worked case, so the score model is verified against arithmetic.

    transient + bank_timeout + card + 4 prior successes + 1 prior failure
    + active subscription, for retry_delayed:

        base                     3200
        root_cause transient    +1600
        failure bank_timeout    +1200
        method card (no entry)     +0
        established customer     +200
        subscription             +400
                                -----
                                 6600
    """
    assert _estimate(_event(), _classification("transient"), "retry_delayed") == 6_600


# ---------------------------------------------------------------------------
# Missing / unmapped features
# ---------------------------------------------------------------------------


def test_an_unmapped_failure_reason_contributes_nothing_rather_than_guessing() -> None:
    """failure_reason has no finite taxonomy, so unknown values are neutral."""
    classification = _classification()
    unmapped = _event(failure_reason="a_reason_the_model_has_never_seen")
    neutral = _event(failure_reason="payment_failed")
    for intervention in sorted(EXECUTABLE_INTERVENTIONS):
        assert _estimate(unmapped, classification, intervention) == _estimate(
            neutral, classification, intervention
        )


def test_a_customer_with_no_history_is_scored_without_error() -> None:
    event = _event(prior_successful=0, prior_failed=0, subscription=False)
    classification = _classification()
    for intervention in sorted(EXECUTABLE_INTERVENTIONS):
        probability = ESTIMATOR.estimate(event, classification, intervention)
        assert 0 <= probability.basis_points <= PROBABILITY_SCALE


# ---------------------------------------------------------------------------
# Invalid input
# ---------------------------------------------------------------------------


def test_unsupported_intervention_is_rejected() -> None:
    with pytest.raises(UnsupportedInterventionError):
        ESTIMATOR.estimate(_event(), _classification(), "wire_transfer")


def test_no_action_is_never_estimated() -> None:
    with pytest.raises(UnsupportedInterventionError):
        ESTIMATOR.estimate(_event(), _classification(), "no_action")


def test_non_event_input_is_rejected() -> None:
    with pytest.raises(EstimationError):
        ESTIMATOR.estimate({"event_id": EVENT_ID}, _classification(), "reminder")  # type: ignore[arg-type]


def test_non_classification_input_is_rejected() -> None:
    with pytest.raises(EstimationError):
        ESTIMATOR.estimate(_event(), {"root_cause_category": "transient"}, "reminder")  # type: ignore[arg-type]


def test_mismatched_event_and_classification_are_rejected() -> None:
    other = ClassificationResult.from_dict(
        dict(_classification().to_dict(), event_id="evt_someone_else")
    )
    with pytest.raises(EstimationError, match="do not match"):
        ESTIMATOR.estimate(_event(), other, "reminder")


# ---------------------------------------------------------------------------
# The coefficient tables are constants, not runtime configuration
#
# Regression coverage for the Phase 16 repair: the estimator's coefficient
# tables were plain module-level dicts, so any importer could retune the model
# in place and break the determinism the estimator guarantees.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "table, key",
    [
        (BASE_RECOVERY_BPS, "retry_delayed"),
        (SUBSCRIPTION_ADJUSTMENT_BPS, "retry_delayed"),
        (ROOT_CAUSE_ADJUSTMENT_BPS, "transient"),
        (FAILURE_REASON_ADJUSTMENT_BPS, "bank_timeout"),
        (PAYMENT_METHOD_ADJUSTMENT_BPS, "upi"),
    ],
)
def test_a_coefficient_table_cannot_be_retuned_at_runtime(table, key) -> None:
    with pytest.raises(TypeError):
        table[key] = 9_999


@pytest.mark.parametrize(
    "table",
    [
        ROOT_CAUSE_ADJUSTMENT_BPS,
        FAILURE_REASON_ADJUSTMENT_BPS,
        PAYMENT_METHOD_ADJUSTMENT_BPS,
    ],
)
def test_nested_coefficient_tables_are_also_read_only(table) -> None:
    for feature, adjustments in table.items():
        with pytest.raises(TypeError):
            adjustments["retry_delayed"] = 9_999


def test_the_estimate_is_unchanged_by_an_attempted_retune() -> None:
    event, classification = _event(), _classification()
    before = ESTIMATOR.estimate(event, classification, "retry_delayed")
    with pytest.raises(TypeError):
        BASE_RECOVERY_BPS["retry_delayed"] = 9_999
    assert ESTIMATOR.estimate(event, classification, "retry_delayed") == before

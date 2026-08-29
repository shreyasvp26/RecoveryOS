"""Phase 17 tests: the hidden world is signal-bearing, frozen, and honest.

These tests are about the WORLD, not about who wins. They assert that ground
truth carries real causal structure, that it is a function of features rather
than of identity, that no strategy can influence it, and that its randomness
contract is order-independent.
"""

from __future__ import annotations

import pytest

from app.economics import DEFAULT_ECONOMIC_MODEL, PROBABILITY_SCALE
from app.estimator import RecoveryProbabilityEstimator
from app.classification import ClassificationResult
from app.generator import generate_events
from app.hidden_world import (
    HiddenWorld,
    HiddenWorldError,
    deterministic_draw_bps,
    true_expected_value_paise,
    true_probability_bps,
)
from app.models import CustomerHistory, PaymentEvent
from app.selector import NO_ACTION

INTERVENTIONS = (
    "retry_immediate",
    "retry_delayed",
    "payment_link",
    "reminder",
    "alternate_method_prompt",
)


def make_event(**overrides) -> PaymentEvent:
    """Build a valid PaymentEvent, overriding only the feature under test."""
    fields = {
        "event_id": "evt_test_0001",
        "order_id": "order_test_0001",
        "payment_id": "pay_test_0001",
        "customer_id": "cust_0001",
        "amount_paise": 500_000,
        "currency": "INR",
        "payment_method": "card",
        "failure_reason": "bank_timeout",
        "bank": "HDFC",
        "risk_flag": "normal",
        "customer_history": CustomerHistory(
            prior_successful_payments=6,
            prior_failed_payments=1,
            has_active_subscription=False,
        ),
        "timestamp": "2026-08-01T10:00:00+00:00",
    }
    fields.update(overrides)
    return PaymentEvent(**fields)


def world() -> HiddenWorld:
    return HiddenWorld(outcome_seed=42, model=DEFAULT_ECONOMIC_MODEL)


def best_intervention(event: PaymentEvent) -> str:
    """The world's highest-probability executable action for an event."""
    return max(
        INTERVENTIONS, key=lambda name: (true_probability_bps(event, name), name)
    )


# ---------------------------------------------------------------------------
# Determinism and domain
# ---------------------------------------------------------------------------


def test_probabilities_are_valid_and_bounded() -> None:
    for event in generate_events(seed=42, count=120):
        for intervention in (NO_ACTION, *INTERVENTIONS):
            probability = true_probability_bps(event, intervention)
            assert isinstance(probability, int)
            assert 0 <= probability <= PROBABILITY_SCALE


def test_the_same_event_always_has_the_same_probability() -> None:
    event = make_event()
    for intervention in INTERVENTIONS:
        first = true_probability_bps(event, intervention)
        for _ in range(5):
            assert true_probability_bps(event, intervention) == first


def test_event_order_cannot_change_a_probability() -> None:
    events = generate_events(seed=42, count=60)
    forward = {
        (event.event_id, i): true_probability_bps(event, i)
        for event in events
        for i in INTERVENTIONS
    }
    backward = {
        (event.event_id, i): true_probability_bps(event, i)
        for event in reversed(events)
        for i in INTERVENTIONS
    }
    assert forward == backward


def test_an_unknown_intervention_is_rejected_not_guessed() -> None:
    with pytest.raises(HiddenWorldError):
        true_probability_bps(make_event(), "send_carrier_pigeon")


# ---------------------------------------------------------------------------
# The probability comes from features, never from identity
# ---------------------------------------------------------------------------


def test_identity_fields_do_not_change_the_probability() -> None:
    """Two events differing ONLY in identity must be indistinguishable."""
    base = make_event()
    twin = make_event(
        event_id="evt_completely_different",
        order_id="order_zzz",
        payment_id="pay_zzz",
        customer_id="cust_9999",
        timestamp="2026-07-02T03:04:05+00:00",
        bank="Kotak",
    )
    for intervention in (NO_ACTION, *INTERVENTIONS):
        assert true_probability_bps(base, intervention) == true_probability_bps(
            twin, intervention
        )


def test_no_probability_is_ever_looked_up_by_event_identity() -> None:
    """The exact shape of the Phase 8 design Phase 17 moves away from.

    Asserted against executable code only: prose explaining that the module
    does NOT key probabilities by event id must not be mistaken for it doing so.
    """
    import ast
    import pathlib

    tree = ast.parse(
        (
            pathlib.Path(__file__).resolve().parent.parent / "app" / "hidden_world.py"
        ).read_text()
    )
    for node in ast.walk(tree):
        if isinstance(node, ast.Subscript):
            assert "event_id" not in ast.unparse(node.slice), (
                f"hidden_world subscripts by event identity: {ast.unparse(node)}"
            )


def test_changing_an_observable_feature_changes_the_probability() -> None:
    base = make_event(failure_reason="bank_timeout")
    assert true_probability_bps(base, "retry_delayed") != true_probability_bps(
        make_event(failure_reason="expired_card"), "retry_delayed"
    )
    assert true_probability_bps(base, "retry_immediate") != true_probability_bps(
        make_event(payment_method="upi"), "retry_immediate"
    )


# ---------------------------------------------------------------------------
# The world carries the intended signal
# ---------------------------------------------------------------------------


def test_a_bank_outage_rewards_waiting_over_retrying_immediately() -> None:
    event = make_event(failure_reason="bank_timeout")
    assert true_probability_bps(event, "retry_delayed") > true_probability_bps(
        event, "retry_immediate"
    )


def test_a_dead_card_rewards_a_new_instrument_over_any_retry() -> None:
    event = make_event(failure_reason="expired_card")
    for retry in ("retry_immediate", "retry_delayed"):
        assert true_probability_bps(event, "alternate_method_prompt") > (
            true_probability_bps(event, retry)
        )
        assert true_probability_bps(event, "payment_link") > (
            true_probability_bps(event, retry)
        )


def test_an_empty_account_rewards_a_nudge_over_an_immediate_retry() -> None:
    event = make_event(failure_reason="insufficient_funds")
    assert true_probability_bps(event, "reminder") > true_probability_bps(
        event, "retry_immediate"
    )
    assert true_probability_bps(event, "retry_delayed") > true_probability_bps(
        event, "retry_immediate"
    )


def test_a_terminal_refusal_is_close_to_unrecoverable() -> None:
    for reason in ("transaction_declined", "payment_failed"):
        event = make_event(failure_reason=reason)
        for intervention in INTERVENTIONS:
            assert true_probability_bps(event, intervention) < 1000


def test_customer_history_moves_the_world() -> None:
    reliable = make_event(
        customer_history=CustomerHistory(
            prior_successful_payments=30,
            prior_failed_payments=0,
            has_active_subscription=False,
        )
    )
    struggling = make_event(
        customer_history=CustomerHistory(
            prior_successful_payments=0,
            prior_failed_payments=6,
            has_active_subscription=False,
        )
    )
    assert true_probability_bps(reliable, "retry_delayed") > true_probability_bps(
        struggling, "retry_delayed"
    )


def test_a_live_mandate_favours_automated_retries_over_asking_the_customer() -> None:
    with_mandate = make_event(
        customer_history=CustomerHistory(
            prior_successful_payments=6,
            prior_failed_payments=1,
            has_active_subscription=True,
        )
    )
    without = make_event()
    assert true_probability_bps(with_mandate, "retry_delayed") > (
        true_probability_bps(without, "retry_delayed")
    )
    assert true_probability_bps(with_mandate, "payment_link") < (
        true_probability_bps(without, "payment_link")
    )


def test_payment_method_moves_the_world() -> None:
    upi = make_event(payment_method="upi", failure_reason="declined_by_bank")
    card = make_event(payment_method="card", failure_reason="declined_by_bank")
    assert true_probability_bps(upi, "retry_immediate") != true_probability_bps(
        card, "retry_immediate"
    )


def test_no_single_intervention_is_globally_best() -> None:
    """A world with one dominant action would not test decisioning at all."""
    winners = {
        best_intervention(make_event(failure_reason=reason))
        for reason in (
            "bank_timeout",
            "expired_card",
            "insufficient_funds",
            "authentication_failed",
            "declined_by_bank",
        )
    }
    assert len(winners) > 1


def test_the_best_action_varies_across_the_generated_distribution() -> None:
    winners = {best_intervention(event) for event in generate_events(seed=42, count=500)}
    assert len(winners) >= 3


def test_fraud_is_kept_inert_by_policy_and_not_by_the_world() -> None:
    """The world may value a fraud event; the policy gate is what stops action.

    Encoding "fraud is unrecoverable" into ground truth would make the safety
    result trivially true for the wrong reason.
    """
    fraud = make_event(risk_flag="fraud_suspect")
    normal = make_event(risk_flag="normal")
    for intervention in INTERVENTIONS:
        assert true_probability_bps(fraud, intervention) == true_probability_bps(
            normal, intervention
        )
        assert true_probability_bps(fraud, intervention) > 0


# ---------------------------------------------------------------------------
# The world is not the estimator
# ---------------------------------------------------------------------------


def _classification(event: PaymentEvent, root: str) -> ClassificationResult:
    return ClassificationResult(
        event_id=event.event_id,
        root_cause_category=root,
        confidence=0.9,
        reasoning="test fixture",
        candidate_interventions=INTERVENTIONS,
    )


def test_the_estimator_and_the_world_are_not_the_same_function() -> None:
    """They may agree sometimes; they must not be mechanically identical."""
    estimator = RecoveryProbabilityEstimator()
    disagreements = 0
    comparisons = 0
    for event in generate_events(seed=42, count=200):
        root = (
            "terminal"
            if event.failure_reason in ("transaction_declined", "payment_failed")
            else "transient"
        )
        classification = _classification(event, root)
        for intervention in INTERVENTIONS:
            comparisons += 1
            believed = estimator.estimate(
                event, classification, intervention
            ).basis_points
            if believed != true_probability_bps(event, intervention):
                disagreements += 1
    assert comparisons > 0
    assert disagreements > 0


def test_the_estimator_and_the_world_disagree_about_rankings_somewhere() -> None:
    """Being wrong about LEVELS is cheap; being wrong about ORDER costs money."""
    estimator = RecoveryProbabilityEstimator()
    ranking_disagreements = 0
    for event in generate_events(seed=42, count=200):
        classification = _classification(event, "transient")
        believed_best = max(
            INTERVENTIONS,
            key=lambda name: (
                estimator.estimate(event, classification, name).basis_points,
                name,
            ),
        )
        if believed_best != best_intervention(event):
            ranking_disagreements += 1
    assert ranking_disagreements > 0


# ---------------------------------------------------------------------------
# True expected value
# ---------------------------------------------------------------------------


def test_true_ev_deducts_cost_and_friction_for_actions_only() -> None:
    event = make_event(amount_paise=1_000_000)
    link_probability = true_probability_bps(event, "payment_link")
    expected = (
        event.amount_paise * link_probability // PROBABILITY_SCALE
        - 100
        - event.amount_paise * 10 // PROBABILITY_SCALE
    )
    assert (
        true_expected_value_paise(event, "payment_link", DEFAULT_ECONOMIC_MODEL)
        == expected
    )


def test_no_action_true_ev_is_its_passive_recovery_value() -> None:
    event = make_event(amount_paise=1_000_000)
    assert true_expected_value_paise(
        event, NO_ACTION, DEFAULT_ECONOMIC_MODEL
    ) == event.amount_paise * true_probability_bps(event, NO_ACTION) // PROBABILITY_SCALE


def test_doing_nothing_is_a_real_recovery_process_not_zero() -> None:
    """The control arm must be a baseline, not a straw man."""
    assert true_probability_bps(make_event(), NO_ACTION) > 0


# ---------------------------------------------------------------------------
# Common randomness contract
# ---------------------------------------------------------------------------


def test_a_draw_depends_only_on_its_key() -> None:
    first = deterministic_draw_bps(42, "evt_000001", "retry_delayed", 0)
    for _ in range(20):
        deterministic_draw_bps(42, "evt_999999", "payment_link", 0)
    assert deterministic_draw_bps(42, "evt_000001", "retry_delayed", 0) == first


def test_every_key_component_changes_the_draw() -> None:
    base = deterministic_draw_bps(42, "evt_000001", "retry_delayed", 0)
    assert deterministic_draw_bps(43, "evt_000001", "retry_delayed", 0) != base
    assert deterministic_draw_bps(42, "evt_000002", "retry_delayed", 0) != base
    assert deterministic_draw_bps(42, "evt_000001", "reminder", 0) != base
    assert deterministic_draw_bps(42, "evt_000001", "retry_delayed", 1) != base


def test_draws_are_in_range_and_reasonably_uniform() -> None:
    draws = [
        deterministic_draw_bps(42, f"evt_{index:06d}", "retry_delayed", 0)
        for index in range(4000)
    ]
    assert all(0 <= draw < PROBABILITY_SCALE for draw in draws)
    assert 4500 < sum(draws) / len(draws) < 5500


def test_realizing_an_outcome_never_mutates_the_world() -> None:
    hidden = world()
    event = make_event()
    first = hidden.realize(event, "retry_delayed")
    hidden.realize(event, "payment_link")
    hidden.realize(make_event(event_id="evt_other"), "reminder")
    assert hidden.realize(event, "retry_delayed").to_dict() == first.to_dict()


def test_different_interventions_can_produce_different_outcomes() -> None:
    hidden = world()
    outcomes = {
        intervention: hidden.realize(event, intervention).recovered
        for event in generate_events(seed=42, count=40)
        for intervention in INTERVENTIONS
    }
    assert True in outcomes.values() and False in outcomes.values()


def test_a_recovered_outcome_carries_the_event_amount() -> None:
    hidden = world()
    for event in generate_events(seed=42, count=100):
        outcome = hidden.realize(event, "retry_delayed")
        assert outcome.recovered_amount_paise == (
            event.amount_paise if outcome.recovered else 0
        )


def test_the_world_takes_no_strategy_argument() -> None:
    """Strategy-independence asserted against the signature itself."""
    import inspect

    for function in (true_probability_bps, true_expected_value_paise):
        assert "strategy" not in inspect.signature(function).parameters
    assert "strategy" not in inspect.signature(HiddenWorld.realize).parameters

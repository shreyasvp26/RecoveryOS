"""Phase 8 tests for the hidden recovery outcome model (evaluation-only)."""

from __future__ import annotations

import pytest

from app.classification import CANDIDATE_INTERVENTIONS
from app.generator import generate_events
from app.models import CustomerHistory, PaymentEvent
from app.outcome_model import (
    HiddenOutcomeModel,
    InvalidOutcomeProbabilityError,
    InvalidSeedError,
    MissingGroundTruthError,
    OutcomeModelError,
    generate_hidden_outcome_model,
)


def _event(event_id: str, risk_flag: str = "normal") -> PaymentEvent:
    return PaymentEvent(
        event_id=event_id,
        order_id=f"order_{event_id}",
        payment_id=f"pay_{event_id}",
        customer_id=f"cust_{event_id}",
        amount_paise=75000,
        currency="INR",
        payment_method="card",
        failure_reason="bank_timeout",
        bank="HDFC",
        risk_flag=risk_flag,
        customer_history=CustomerHistory(4, 1, True),
        timestamp="2026-08-27T12:00:00+00:00",
    )


def _model(seed: int = 42, count: int = 12) -> HiddenOutcomeModel:
    return generate_hidden_outcome_model(generate_events(seed=seed, count=count), seed)


def _valid_probabilities() -> dict[str, dict[str, float]]:
    return {
        "evt_a": {intervention: 0.5 for intervention in CANDIDATE_INTERVENTIONS},
        "evt_b": {intervention: 0.25 for intervention in CANDIDATE_INTERVENTIONS},
    }


def test_generation_is_deterministic_for_same_seed_and_events() -> None:
    events = generate_events(seed=42, count=10)
    first = generate_hidden_outcome_model(events, 42)
    second = generate_hidden_outcome_model(events, 42)
    assert first == second
    assert first.to_dict() == second.to_dict()


def test_generation_is_deterministic_for_other_seed_and_count() -> None:
    events = generate_events(seed=1337, count=7)
    first = generate_hidden_outcome_model(events, 1337)
    second = generate_hidden_outcome_model(events, 1337)
    assert first == second


def test_probabilities_depend_only_on_seed_and_event_identity() -> None:
    events = generate_events(seed=5, count=50)
    model = generate_hidden_outcome_model(events, 5)
    probe = events[0]
    assert model.recovery_probability(probe.event_id, "retry_delayed") == (
        generate_hidden_outcome_model(events[:20], 5).recovery_probability(
            probe.event_id, "retry_delayed"
        )
    )
    assert model.recovery_probability(probe.event_id, "retry_delayed") == (
        generate_hidden_outcome_model(events[:30], 5).recovery_probability(
            probe.event_id, "retry_delayed"
        )
    )


def test_different_seed_produces_a_different_model() -> None:
    events = generate_events(seed=42, count=10)
    first = generate_hidden_outcome_model(events, 42)
    second = generate_hidden_outcome_model(events, 43)
    assert first != second
    assert any(
        first.recovery_probability(event.event_id, intervention)
        != second.recovery_probability(event.event_id, intervention)
        for event in events
        for intervention in CANDIDATE_INTERVENTIONS
    )


def test_every_event_covers_every_locked_intervention() -> None:
    model = _model()
    for event_id, by_intervention in model.to_dict().items():
        assert set(by_intervention) == set(CANDIDATE_INTERVENTIONS)


def test_no_action_is_covered_explicitly() -> None:
    model = _model()
    for by_intervention in model.to_dict().values():
        assert "no_action" in by_intervention
        assert 0.0 <= by_intervention["no_action"] <= 1.0


def test_all_probabilities_are_within_bounds() -> None:
    model = _model(count=30)
    for by_intervention in model.to_dict().values():
        for probability in by_intervention.values():
            assert 0.0 <= probability <= 1.0


def test_probabilities_are_event_specific() -> None:
    model = _model(seed=20260827, count=10)
    vectors = {frozenset(mapping.items()) for mapping in model.to_dict().values()}
    assert len(vectors) > 1


def test_missing_event_ground_truth_is_explicit() -> None:
    model = _model()
    with pytest.raises(MissingGroundTruthError):
        model.recovery_probability("evt_ghost", "retry_delayed")


def test_missing_intervention_ground_truth_is_explicit() -> None:
    model = _model()
    event_id = next(iter(model.event_ids))
    with pytest.raises(MissingGroundTruthError):
        model.recovery_probability(event_id, "not_an_intervention")


def test_probability_above_one_fails_explicitly_not_clamped() -> None:
    probabilities = _valid_probabilities()
    probabilities["evt_a"]["retry_delayed"] = 1.5
    with pytest.raises(InvalidOutcomeProbabilityError):
        HiddenOutcomeModel(seed=42, probabilities=probabilities)


def test_probability_below_zero_fails_explicitly_not_clamped() -> None:
    probabilities = _valid_probabilities()
    probabilities["evt_a"]["retry_delayed"] = -0.01
    with pytest.raises(InvalidOutcomeProbabilityError):
        HiddenOutcomeModel(seed=42, probabilities=probabilities)


def test_non_numeric_probability_fails_explicitly() -> None:
    probabilities = _valid_probabilities()
    probabilities["evt_a"]["retry_delayed"] = "high"
    with pytest.raises(InvalidOutcomeProbabilityError):
        HiddenOutcomeModel(seed=42, probabilities=probabilities)


def test_boolean_probability_fails_explicitly() -> None:
    probabilities = _valid_probabilities()
    probabilities["evt_a"]["retry_delayed"] = True
    with pytest.raises(InvalidOutcomeProbabilityError):
        HiddenOutcomeModel(seed=42, probabilities=probabilities)


def test_missing_intervention_coverage_fails_explicitly() -> None:
    probabilities = _valid_probabilities()
    del probabilities["evt_a"]["reminder"]
    with pytest.raises(InvalidOutcomeProbabilityError):
        HiddenOutcomeModel(seed=42, probabilities=probabilities)


def test_untracked_intervention_fails_explicitly() -> None:
    probabilities = _valid_probabilities()
    probabilities["evt_a"]["wire_transfer"] = 0.5
    with pytest.raises(InvalidOutcomeProbabilityError):
        HiddenOutcomeModel(seed=42, probabilities=probabilities)


def test_invalid_seed_is_rejected() -> None:
    events = [_event("evt_x")]
    for bad_seed in ("42", 42.0, None, True):
        with pytest.raises(InvalidSeedError):
            generate_hidden_outcome_model(events, bad_seed)


def test_empty_event_set_is_rejected() -> None:
    with pytest.raises(OutcomeModelError):
        generate_hidden_outcome_model([], 42)


def test_non_payment_event_is_rejected() -> None:
    events = [_event("evt_x"), {"event_id": "evt_y"}]
    with pytest.raises(OutcomeModelError):
        generate_hidden_outcome_model(events, 42)


def test_overlapping_event_sets_reuse_identical_probabilities() -> None:
    events = generate_events(seed=7, count=20)
    full = generate_hidden_outcome_model(events, 7)
    subset = generate_hidden_outcome_model(events[:5], 7)
    for event in events[:5]:
        for intervention in CANDIDATE_INTERVENTIONS:
            assert (
                full.recovery_probability(event.event_id, intervention)
                == subset.recovery_probability(event.event_id, intervention)
            )


def test_model_is_independent_of_classification_and_policy_state() -> None:
    model = _model(seed=99, count=16)
    assert model.seed == 99
    assert model.event_ids == {
        event.event_id for event in generate_events(seed=99, count=16)
    }

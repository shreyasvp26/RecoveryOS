"""Phase 8 tests for the deterministic recovery outcome simulation."""

from __future__ import annotations

import random

import pytest

from app.classification import CANDIDATE_INTERVENTIONS
from app.generator import generate_events
from app.models import CustomerHistory, PaymentEvent
from app.outcome import OutcomeSimulator, RecoveryOutcome
from app.outcome_model import (
    MissingGroundTruthError,
    OutcomeModelError,
    generate_hidden_outcome_model,
)


def _event(event_id: str, amount_paise: int = 75000) -> PaymentEvent:
    return PaymentEvent(
        event_id=event_id,
        order_id=f"order_{event_id}",
        payment_id=f"pay_{event_id}",
        customer_id=f"cust_{event_id}",
        amount_paise=amount_paise,
        currency="INR",
        payment_method="card",
        failure_reason="bank_timeout",
        bank="HDFC",
        risk_flag="normal",
        customer_history=CustomerHistory(4, 1, True),
        timestamp="2026-08-27T12:00:00+00:00",
    )


def _simulator(seed: int = 42, count: int = 12) -> OutcomeSimulator:
    events = generate_events(seed=seed, count=count)
    model = generate_hidden_outcome_model(events, seed)
    return OutcomeSimulator(model)


def _manual_draw(seed: int, event_id: str, intervention: str, p: float) -> bool:
    draw = random.Random(f"{seed}:{event_id}:{intervention}").random()
    return draw < p


def test_simulation_is_reproducible_for_same_triple() -> None:
    events = generate_events(seed=42, count=10)
    model = generate_hidden_outcome_model(events, 42)
    simulator = OutcomeSimulator(model)
    probe = events[0]
    first = simulator.simulate(probe, "retry_delayed")
    second = simulator.simulate(probe, "retry_delayed")
    assert first == second
    assert first.to_dict() == second.to_dict()


def test_simulation_matches_manual_bernoulli_draw() -> None:
    events = generate_events(seed=7, count=20)
    model = generate_hidden_outcome_model(events, 7)
    simulator = OutcomeSimulator(model)
    for event in events[:6]:
        for intervention in ("retry_immediate", "retry_delayed", "no_action"):
            p = model.recovery_probability(event.event_id, intervention)
            outcome = simulator.simulate(event, intervention)
            assert outcome.recovered is (
                _manual_draw(7, event.event_id, intervention, p)
            )


def _outcomes_by_triple(
    simulator: OutcomeSimulator, events: list[PaymentEvent]
) -> dict[tuple[str, str], RecoveryOutcome]:
    results: dict[tuple[str, str], RecoveryOutcome] = {}
    for event in events:
        for intervention in CANDIDATE_INTERVENTIONS:
            results[(event.event_id, intervention)] = simulator.simulate(
                event, intervention
            )
    return results


def test_outcome_is_independent_of_evaluation_order() -> None:
    events = generate_events(seed=99, count=30)
    model = generate_hidden_outcome_model(events, 99)
    simulator = OutcomeSimulator(model)

    ascending = _outcomes_by_triple(simulator, events)
    descending = _outcomes_by_triple(simulator, list(reversed(events)))
    ascending_again = _outcomes_by_triple(simulator, events)
    assert ascending == ascending_again
    assert ascending == descending


def test_simulation_result_depends_on_event_identity_and_seed() -> None:
    events = generate_events(seed=20260828, count=40)
    model_a = generate_hidden_outcome_model(events, 20260828)
    model_b = generate_hidden_outcome_model(events, 20260829)
    simulator_a = OutcomeSimulator(model_a)
    simulator_b = OutcomeSimulator(model_b)

    def outcomes_for(simulator: OutcomeSimulator) -> list[RecoveryOutcome]:
        return [
            simulator.simulate(event, "retry_delayed") for event in events
        ]

    result_a = outcomes_for(simulator_a)
    result_b = outcomes_for(simulator_b)
    assert result_a != result_b


def test_recovered_amount_is_derived_from_event_amount() -> None:
    event = _event("evt_amount", amount_paise=123456)
    model = generate_hidden_outcome_model([event], 42)
    simulator = OutcomeSimulator(model)
    outcome = simulator.simulate(event, "retry_delayed")
    if outcome.recovered:
        assert outcome.recovered_amount_paise == 123456
    else:
        assert outcome.recovered_amount_paise == 0


def test_no_action_is_simulatable_as_baseline_only() -> None:
    event = _event("evt_baseline")
    model = generate_hidden_outcome_model([event], 42)
    simulator = OutcomeSimulator(model)
    outcome = simulator.simulate(event, "no_action")
    assert isinstance(outcome.recovered, bool)
    assert outcome.intervention == "no_action"


def test_covers_all_root_cause_categories() -> None:
    from app.classification import ClassificationResult

    events = generate_events(seed=11, count=60)
    by_category: dict[str, PaymentEvent] = {}
    categories: list[str] = [
        "transient",
        "customer_action_needed",
        "fraud_suspect",
        "terminal",
    ]
    for category in categories:
        event = _event(category)
        ClassificationResult(
            event_id=event.event_id,
            root_cause_category=category,
            confidence=0.9,
            reasoning="test",
            candidate_interventions=tuple(
                sorted(CANDIDATE_INTERVENTIONS - {"no_action"})
            ),
        )
        by_category[category] = event
    all_events = list(by_category.values()) + events
    model = generate_hidden_outcome_model(all_events, 11)
    simulator = OutcomeSimulator(model)
    seen: set[str] = set()
    for category in categories:
        event = by_category[category]
        seen.add(category)
        for intervention in CANDIDATE_INTERVENTIONS:
            outcome = simulator.simulate(event, intervention)
            assert outcome.event_id == event.event_id
            assert outcome.intervention == intervention
            assert isinstance(outcome.recovered, bool)
    assert set(categories) <= seen
    assert set(categories) <= set(model.event_ids)


def test_missing_event_ground_truth_fails_explicitly() -> None:
    simulator = _simulator()
    with pytest.raises(MissingGroundTruthError):
        simulator.simulate(_event("evt_ghost"), "retry_delayed")


def test_untracked_intervention_fails_closed() -> None:
    simulator = _simulator()
    event = generate_events(seed=42, count=12)[0]
    with pytest.raises(OutcomeModelError):
        simulator.simulate(event, "not_an_intervention")


def test_non_payment_event_fails_closed() -> None:
    simulator = _simulator()
    with pytest.raises(OutcomeModelError):
        simulator.simulate({"event_id": "evt_x"}, "retry_delayed")


def test_recovery_outcome_validates_its_fields() -> None:
    with pytest.raises(OutcomeModelError):
        RecoveryOutcome("evt_x", "not_locked", True, 100)
    with pytest.raises(OutcomeModelError):
        RecoveryOutcome("", "retry_delayed", True, 100)
    with pytest.raises(OutcomeModelError):
        RecoveryOutcome("evt_x", "reminder", "yes", 100)
    with pytest.raises(OutcomeModelError):
        RecoveryOutcome("evt_x", "reminder", True, -5)


def test_recovery_outcome_record_is_minimal() -> None:
    simulator = _simulator()
    event = generate_events(seed=42, count=12)[0]
    outcome = simulator.simulate(event, "payment_link")
    assert set(outcome.to_dict()) == {
        "event_id",
        "intervention",
        "recovered",
        "recovered_amount_paise",
    }

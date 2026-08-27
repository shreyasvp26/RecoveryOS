"""Phase 4 tests for the deterministic synthetic PaymentEvent generator."""

from __future__ import annotations

import pytest

from app.generator import (
    DEFAULT_COUNT,
    DEFAULT_SEED,
    generate_event_dicts,
    generate_events,
)
from app.models import PAYMENT_METHODS, RISK_FLAGS, PaymentEvent

LOCKED_FIELDS = frozenset(
    {
        "event_id",
        "order_id",
        "payment_id",
        "customer_id",
        "amount_paise",
        "currency",
        "payment_method",
        "failure_reason",
        "bank",
        "risk_flag",
        "customer_history",
        "timestamp",
    }
)

LOCKED_HISTORY_FIELDS = frozenset(
    {
        "prior_successful_payments",
        "prior_failed_payments",
        "has_active_subscription",
    }
)

_FORBIDDEN_FUTURE_FIELDS = frozenset(
    {
        "true_recovery_probability",
        "recovery_probability",
        "best_intervention",
        "true_outcome",
        "benchmark_score",
        "simulated_revenue",
        "recovery_amount",
        "recovery_label",
        "strategy_ground_truth",
        "expected_recovery",
    }
)


def test_generation_is_deterministic_for_same_seed() -> None:
    first = generate_events(seed=DEFAULT_SEED, count=DEFAULT_COUNT)
    second = generate_events(seed=DEFAULT_SEED, count=DEFAULT_COUNT)
    assert first == second


def test_generation_is_deterministic_for_other_seed_and_count() -> None:
    first = generate_events(seed=123, count=7)
    second = generate_events(seed=123, count=7)
    assert first == second


def test_serialized_datasets_match_for_same_seed() -> None:
    assert generate_event_dicts(seed=99, count=5) == generate_event_dicts(
        seed=99, count=5
    )


def test_different_seeds_are_capable_of_differing() -> None:
    assert generate_events(seed=42, count=DEFAULT_COUNT) != generate_events(
        seed=7, count=DEFAULT_COUNT
    )


def test_generated_events_satisfy_locked_contract() -> None:
    for event in generate_events(seed=DEFAULT_SEED, count=DEFAULT_COUNT):
        assert isinstance(event, PaymentEvent)
        assert PaymentEvent.from_dict(event.to_dict()) == event


def test_identifiers_are_unique_within_dataset() -> None:
    events = generate_events(seed=DEFAULT_SEED, count=20)
    assert len({event.event_id for event in events}) == len(events)
    assert len({event.order_id for event in events}) == len(events)
    assert len({event.payment_id for event in events}) == len(events)


def test_customer_ids_may_repeat_within_dataset() -> None:
    events = generate_events(seed=DEFAULT_SEED, count=20)
    assert all(event.customer_id.startswith("cust_") for event in events)
    assert len({event.customer_id for event in events}) < len(events)


def test_domain_values_are_valid() -> None:
    for event in generate_events(seed=DEFAULT_SEED, count=DEFAULT_COUNT):
        assert event.payment_method in PAYMENT_METHODS
        assert event.risk_flag in RISK_FLAGS
        assert isinstance(event.failure_reason, str) and event.failure_reason.strip()
        assert isinstance(event.bank, str) and event.bank.strip()
        assert event.currency == "INR"
        assert isinstance(event.amount_paise, int)
        assert not isinstance(event.amount_paise, bool)
        assert event.amount_paise > 0


def test_generated_events_cover_locked_payment_methods() -> None:
    methods = {
        event.payment_method
        for event in generate_events(seed=DEFAULT_SEED, count=40)
    }
    assert methods == set(PAYMENT_METHODS)


def test_generated_events_cover_locked_risk_flags() -> None:
    flags = {event.risk_flag for event in generate_events(seed=DEFAULT_SEED, count=40)}
    assert flags == set(RISK_FLAGS)


def test_serialized_contract_has_no_extra_or_missing_fields() -> None:
    for event_dict in generate_event_dicts(seed=DEFAULT_SEED, count=DEFAULT_COUNT):
        assert set(event_dict) == LOCKED_FIELDS
        assert set(event_dict["customer_history"]) == LOCKED_HISTORY_FIELDS


def test_no_future_outcome_fields_exist() -> None:
    collected = {
        key
        for event_dict in generate_event_dicts(seed=DEFAULT_SEED, count=DEFAULT_COUNT)
        for key in event_dict
    }
    assert collected.isdisjoint(_FORBIDDEN_FUTURE_FIELDS)


def test_timestamps_are_deterministic_and_valid() -> None:
    first = generate_events(seed=99, count=DEFAULT_COUNT)
    second = generate_events(seed=99, count=DEFAULT_COUNT)
    assert [event.timestamp for event in first] == [
        event.timestamp for event in second
    ]
    for event in first:
        PaymentEvent.from_dict(event.to_dict())


def test_count_controls_dataset_size() -> None:
    assert len(generate_events(seed=DEFAULT_SEED, count=3)) == 3
    assert len(generate_events(seed=DEFAULT_SEED, count=3)) != len(
        generate_events(seed=DEFAULT_SEED, count=10)
    )


@pytest.mark.parametrize("count", [0, -5])
def test_invalid_count_is_rejected(count) -> None:
    with pytest.raises(ValueError):
        generate_events(seed=DEFAULT_SEED, count=count)

"""Phase 5 tests for the advisory classification contract."""

from __future__ import annotations

import pytest

from app.classification import (
    CANDIDATE_INTERVENTIONS,
    CLASSIFICATION_KEYS,
    ROOT_CAUSE_CATEGORIES,
    ClassificationResult,
)

_FORBIDDEN_BUSINESS_FIELDS = frozenset(
    {
        "recovery_probability",
        "expected_revenue",
        "best_intervention",
        "policy_decision",
        "allowed",
        "execution",
        "true_outcome",
        "benchmark_score",
    }
)


def valid_result(**overrides) -> dict:
    base = {
        "event_id": "evt_000001",
        "root_cause_category": "transient",
        "confidence": 0.91,
        "reasoning": "Payments from this bank frequently recover on retry.",
        "candidate_interventions": ["retry_delayed", "payment_link"],
    }
    base.update(overrides)
    return base


def test_valid_classification_builds() -> None:
    result = ClassificationResult.from_dict(valid_result())
    assert result.event_id == "evt_000001"
    assert result.root_cause_category == "transient"
    assert result.candidate_interventions == ("retry_delayed", "payment_link")


def test_round_trip_preserves_contract() -> None:
    data = valid_result()
    result = ClassificationResult.from_dict(data)
    assert result.to_dict() == data


@pytest.mark.parametrize(
    "category",
    ["transient", "customer_action_needed", "fraud_suspect", "terminal"],
)
def test_valid_root_cause_categories_accepted(category) -> None:
    result = ClassificationResult.from_dict(valid_result(root_cause_category=category))
    assert result.root_cause_category == category


@pytest.mark.parametrize("bad", ["unknown", "", None, "TRANSIENT", 42])
def test_invalid_root_cause_rejected(bad) -> None:
    with pytest.raises(ValueError):
        ClassificationResult.from_dict(valid_result(root_cause_category=bad))


@pytest.mark.parametrize(
    "interventions",
    [
        ["send_whatsapp_payment_link"],
        ["payment_link", "unknown"],
        "payment_link",
        [None],
        "retry_immediate",
    ],
)
def test_invalid_intervention_rejected(interventions) -> None:
    with pytest.raises(ValueError):
        ClassificationResult.from_dict(valid_result(candidate_interventions=interventions))


def test_all_locked_interventions_accepted() -> None:
    for intervention in sorted(CANDIDATE_INTERVENTIONS):
        result = ClassificationResult.from_dict(
            valid_result(candidate_interventions=[intervention])
        )
        assert result.candidate_interventions == (intervention,)


@pytest.mark.parametrize("bad", [-0.1, 1.1, "0.9", True, None])
def test_invalid_confidence_rejected(bad) -> None:
    with pytest.raises(ValueError):
        ClassificationResult.from_dict(valid_result(confidence=bad))


@pytest.mark.parametrize("confidence", [0.0, 0.5, 1.0, 0.91])
def test_valid_confidence_accepted(confidence) -> None:
    result = ClassificationResult.from_dict(valid_result(confidence=confidence))
    assert 0.0 <= result.confidence <= 1.0


@pytest.mark.parametrize("field", sorted(CLASSIFICATION_KEYS))
def test_each_required_field_is_required(field) -> None:
    data = valid_result()
    del data[field]
    with pytest.raises(ValueError):
        ClassificationResult.from_dict(data)


def test_unexpected_field_is_rejected() -> None:
    with pytest.raises(ValueError):
        ClassificationResult.from_dict(valid_result(recovery_probability=0.5))


def test_contract_has_no_business_or_outcome_fields() -> None:
    serialized = ClassificationResult.from_dict(valid_result()).to_dict()
    assert set(serialized) == set(CLASSIFICATION_KEYS)
    assert _FORBIDDEN_BUSINESS_FIELDS.isdisjoint(serialized)


def test_empty_interventions_are_allowed() -> None:
    result = ClassificationResult.from_dict(valid_result(candidate_interventions=[]))
    assert result.candidate_interventions == ()


def test_root_cause_taxonomy_is_locked() -> None:
    assert ROOT_CAUSE_CATEGORIES == frozenset(
        {"transient", "customer_action_needed", "fraud_suspect", "terminal"}
    )


def test_intervention_taxonomy_is_locked() -> None:
    assert CANDIDATE_INTERVENTIONS == frozenset(
        {
            "retry_immediate",
            "retry_delayed",
            "payment_link",
            "reminder",
            "alternate_method_prompt",
            "no_action",
        }
    )
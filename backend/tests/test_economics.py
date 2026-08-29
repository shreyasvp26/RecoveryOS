"""Phase 16 tests: monetary arithmetic, cost model, friction model, and EV.

Every expected value below is calculated by hand in the assertion so that the
economic arithmetic is verified against arithmetic, not against itself.
"""

from __future__ import annotations

import pytest

from app.economics import (
    DEFAULT_ECONOMIC_MODEL,
    EXECUTABLE_INTERVENTIONS,
    PROBABILITY_SCALE,
    CandidateEvaluation,
    EconomicModel,
    EconomicsError,
    InvalidMoneyError,
    InvalidProbabilityError,
    InterventionEconomics,
    RecoveryProbability,
    UnsupportedInterventionError,
    evaluate_candidate,
    expected_recovered_value_paise,
    friction_cost_paise,
)

FREE = InterventionEconomics(cost_paise=0, friction_bps=0)


def _model(**overrides: InterventionEconomics) -> EconomicModel:
    """An economic model that is free by default, for isolating one term."""
    assumptions = {name: FREE for name in EXECUTABLE_INTERVENTIONS}
    assumptions.update(overrides)
    return EconomicModel(assumptions=assumptions)


# ---------------------------------------------------------------------------
# Probability representation and validation
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("basis_points", [0, 1, 5_000, 9_999, PROBABILITY_SCALE])
def test_valid_probabilities_are_accepted_across_the_whole_domain(
    basis_points: int,
) -> None:
    assert RecoveryProbability(basis_points).basis_points == basis_points


@pytest.mark.parametrize("basis_points", [-1, -10_000, PROBABILITY_SCALE + 1, 20_000])
def test_out_of_domain_probability_is_rejected_never_clamped(
    basis_points: int,
) -> None:
    with pytest.raises(InvalidProbabilityError):
        RecoveryProbability(basis_points)


@pytest.mark.parametrize("basis_points", [0.5, "5000", None, True, False])
def test_non_integer_probability_is_rejected(basis_points: object) -> None:
    with pytest.raises(InvalidProbabilityError):
        RecoveryProbability(basis_points)  # type: ignore[arg-type]


def test_probability_boundaries_map_to_exact_fractions() -> None:
    assert RecoveryProbability(0).as_fraction == 0.0
    assert RecoveryProbability(PROBABILITY_SCALE).as_fraction == 1.0
    assert RecoveryProbability(2_500).as_fraction == 0.25


# ---------------------------------------------------------------------------
# Monetary arithmetic and the rounding policy
# ---------------------------------------------------------------------------


def test_expected_recovered_value_is_probability_times_amount() -> None:
    # 50% of ₹1,000.00 (100000 paise) is exactly ₹500.00.
    assert expected_recovered_value_paise(100_000, RecoveryProbability(5_000)) == 50_000


def test_probability_zero_recovers_nothing() -> None:
    assert expected_recovered_value_paise(100_000, RecoveryProbability(0)) == 0


def test_probability_one_recovers_the_full_amount() -> None:
    assert (
        expected_recovered_value_paise(
            100_000, RecoveryProbability(PROBABILITY_SCALE)
        )
        == 100_000
    )


def test_expected_value_scales_linearly_with_amount() -> None:
    probability = RecoveryProbability(2_500)
    assert expected_recovered_value_paise(1_000, probability) == 250
    assert expected_recovered_value_paise(10_000, probability) == 2_500
    assert expected_recovered_value_paise(100_000, probability) == 25_000


def test_fractional_paise_are_floored_not_rounded() -> None:
    # 3333 bps of 1 paise is 0.3333 paise -> floors to 0, never rounds to 0/1
    # by float behaviour.
    assert expected_recovered_value_paise(1, RecoveryProbability(3_333)) == 0
    # 9999 bps of 1 paise is 0.9999 paise -> floors to 0, NOT round-half-up 1.
    assert expected_recovered_value_paise(1, RecoveryProbability(9_999)) == 0
    # 7 paise at 5000 bps is 3.5 paise -> floors to 3, not banker's-rounded 4.
    assert expected_recovered_value_paise(7, RecoveryProbability(5_000)) == 3


def test_rounding_is_exact_at_the_representation_boundary() -> None:
    # 10000 paise at 1 bp is exactly 1 paise, with no residue.
    assert expected_recovered_value_paise(10_000, RecoveryProbability(1)) == 1
    # 9999 paise at 1 bp is 0.9999 paise and floors to 0.
    assert expected_recovered_value_paise(9_999, RecoveryProbability(1)) == 0


def test_very_large_amounts_stay_exact_under_integer_arithmetic() -> None:
    # ₹10,00,00,000.00 in paise; float64 would lose precision here.
    huge = 100_000_000_000
    assert (
        expected_recovered_value_paise(huge, RecoveryProbability(3_333))
        == huge * 3_333 // PROBABILITY_SCALE
        == 33_330_000_000
    )


@pytest.mark.parametrize("amount", [-1, -100_000])
def test_negative_amount_is_rejected(amount: int) -> None:
    with pytest.raises(InvalidMoneyError):
        expected_recovered_value_paise(amount, RecoveryProbability(5_000))


@pytest.mark.parametrize("amount", [1000.5, "1000", None, True])
def test_non_integer_amount_is_rejected(amount: object) -> None:
    with pytest.raises(InvalidMoneyError):
        expected_recovered_value_paise(amount, RecoveryProbability(5_000))  # type: ignore[arg-type]


def test_raw_float_probability_cannot_be_used_for_money() -> None:
    with pytest.raises(InvalidProbabilityError):
        expected_recovered_value_paise(100_000, 0.5)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Cost model
# ---------------------------------------------------------------------------


def test_every_executable_intervention_has_a_cost_assumption() -> None:
    assert set(DEFAULT_ECONOMIC_MODEL.assumptions) == EXECUTABLE_INTERVENTIONS


def test_no_action_is_never_priced_because_it_is_not_executable() -> None:
    assert "no_action" not in DEFAULT_ECONOMIC_MODEL.assumptions
    with pytest.raises(UnsupportedInterventionError):
        DEFAULT_ECONOMIC_MODEL.for_intervention("no_action")


def test_no_cost_is_negative() -> None:
    for economics in DEFAULT_ECONOMIC_MODEL.assumptions.values():
        assert economics.cost_paise >= 0


def test_all_costs_are_integer_paise_never_floats() -> None:
    for economics in DEFAULT_ECONOMIC_MODEL.assumptions.values():
        assert isinstance(economics.cost_paise, int)
        assert not isinstance(economics.cost_paise, bool)


def test_cost_lookup_is_deterministic_across_repeated_reads() -> None:
    first = [
        DEFAULT_ECONOMIC_MODEL.for_intervention(name)
        for name in sorted(EXECUTABLE_INTERVENTIONS)
    ]
    second = [
        DEFAULT_ECONOMIC_MODEL.for_intervention(name)
        for name in sorted(EXECUTABLE_INTERVENTIONS)
    ]
    assert first == second


def test_unknown_intervention_cost_is_an_explicit_error_not_a_default() -> None:
    with pytest.raises(UnsupportedInterventionError):
        DEFAULT_ECONOMIC_MODEL.for_intervention("wire_transfer")


def test_negative_cost_is_rejected() -> None:
    with pytest.raises(InvalidMoneyError):
        InterventionEconomics(cost_paise=-1, friction_bps=0)


def test_model_missing_an_intervention_is_rejected() -> None:
    incomplete = {
        name: FREE for name in sorted(EXECUTABLE_INTERVENTIONS)[:-1]
    }
    with pytest.raises(EconomicsError, match="missing assumptions"):
        EconomicModel(assumptions=incomplete)


def test_model_pricing_a_non_executable_intervention_is_rejected() -> None:
    extra = {name: FREE for name in EXECUTABLE_INTERVENTIONS}
    extra["no_action"] = FREE
    with pytest.raises(EconomicsError, match="non-executable"):
        EconomicModel(assumptions=extra)


# ---------------------------------------------------------------------------
# Friction model
# ---------------------------------------------------------------------------


def test_friction_converts_to_paise_as_a_proportion_of_amount() -> None:
    # 15 bps of 100000 paise = 0.15% of ₹1,000.00 = 150 paise.
    assert friction_cost_paise(100_000, 15) == 150


def test_friction_is_deterministic_on_repeated_evaluation() -> None:
    assert friction_cost_paise(75_000, 10) == friction_cost_paise(75_000, 10) == 75


def test_zero_friction_costs_nothing_at_any_amount() -> None:
    assert friction_cost_paise(0, 0) == 0
    assert friction_cost_paise(10_000_000, 0) == 0


def test_friction_is_intervention_specific() -> None:
    frictions = {
        name: DEFAULT_ECONOMIC_MODEL.for_intervention(name).friction_bps
        for name in EXECUTABLE_INTERVENTIONS
    }
    # A background retry is invisible to the customer; a prompt is not.
    assert frictions["retry_delayed"] == 0
    assert frictions["retry_immediate"] == 0
    assert frictions["alternate_method_prompt"] > frictions["reminder"] > 0
    assert len(set(frictions.values())) > 1


def test_friction_conversion_is_floored_like_every_other_money_term() -> None:
    # 5 bps of 1999 paise = 0.9995 paise -> 0.
    assert friction_cost_paise(1_999, 5) == 0
    # 5 bps of 2000 paise = 1.0 paise -> 1.
    assert friction_cost_paise(2_000, 5) == 1


@pytest.mark.parametrize("friction_bps", [-1, PROBABILITY_SCALE + 1])
def test_out_of_domain_friction_is_rejected(friction_bps: int) -> None:
    with pytest.raises(EconomicsError):
        friction_cost_paise(100_000, friction_bps)
    with pytest.raises(EconomicsError):
        InterventionEconomics(cost_paise=0, friction_bps=friction_bps)


def test_non_integer_friction_is_rejected() -> None:
    with pytest.raises(EconomicsError):
        InterventionEconomics(cost_paise=0, friction_bps=0.5)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Expected value: the full equation
# ---------------------------------------------------------------------------


def test_expected_value_subtracts_cost_and_friction_explicitly() -> None:
    model = _model(
        payment_link=InterventionEconomics(cost_paise=100, friction_bps=10)
    )
    evaluation = evaluate_candidate(
        intervention="payment_link",
        amount_paise=100_000,
        probability=RecoveryProbability(3_000),
        model=model,
    )
    # By hand: recovered = 100000 * 3000 / 10000 = 30000
    #          friction  = 100000 *   10 / 10000 =   100
    #          EV        = 30000 - 100 - 100     = 29800
    assert evaluation.expected_recovered_value_paise == 30_000
    assert evaluation.intervention_cost_paise == 100
    assert evaluation.friction_cost_paise == 100
    assert evaluation.expected_value_paise == 29_800


def test_zero_cost_leaves_expected_value_equal_to_expected_recovery() -> None:
    evaluation = evaluate_candidate(
        intervention="retry_delayed",
        amount_paise=100_000,
        probability=RecoveryProbability(4_000),
        model=_model(),
    )
    assert evaluation.expected_value_paise == 40_000
    assert evaluation.intervention_cost_paise == 0
    assert evaluation.friction_cost_paise == 0


def test_zero_probability_yields_the_negated_total_cost() -> None:
    model = _model(reminder=InterventionEconomics(cost_paise=20, friction_bps=5))
    evaluation = evaluate_candidate(
        intervention="reminder",
        amount_paise=100_000,
        probability=RecoveryProbability(0),
        model=model,
    )
    # Nothing recovered, but the action still costs 20 + 50 paise.
    assert evaluation.expected_recovered_value_paise == 0
    assert evaluation.expected_value_paise == -70


def test_certain_recovery_yields_amount_minus_costs() -> None:
    model = _model(
        payment_link=InterventionEconomics(cost_paise=100, friction_bps=10)
    )
    evaluation = evaluate_candidate(
        intervention="payment_link",
        amount_paise=100_000,
        probability=RecoveryProbability(PROBABILITY_SCALE),
        model=model,
    )
    assert evaluation.expected_value_paise == 100_000 - 100 - 100


def test_expected_value_may_be_negative_when_cost_exceeds_recovery() -> None:
    model = _model(
        payment_link=InterventionEconomics(cost_paise=10_000, friction_bps=0)
    )
    evaluation = evaluate_candidate(
        intervention="payment_link",
        amount_paise=1_000,
        probability=RecoveryProbability(1_000),
        model=model,
    )
    # recovered = 100; EV = 100 - 10000 = -9900. A real, meaningful result.
    assert evaluation.expected_value_paise == -9_900


def test_evaluation_is_deterministic_on_repeated_calls() -> None:
    def evaluate() -> CandidateEvaluation:
        return evaluate_candidate(
            intervention="reminder",
            amount_paise=123_457,
            probability=RecoveryProbability(2_345),
            model=DEFAULT_ECONOMIC_MODEL,
        )

    assert evaluate() == evaluate() == evaluate()


def test_evaluation_exposes_every_term_of_the_equation() -> None:
    evaluation = evaluate_candidate(
        intervention="reminder",
        amount_paise=100_000,
        probability=RecoveryProbability(2_000),
        model=DEFAULT_ECONOMIC_MODEL,
    )
    assert evaluation.to_dict() == {
        "intervention": "reminder",
        "estimated_probability_bps": 2_000,
        "amount_paise": 100_000,
        "expected_recovered_value_paise": 20_000,
        "intervention_cost_paise": 20,
        "friction_cost_paise": 50,
        "expected_value_paise": 20_000 - 20 - 50,
    }
    # The reported terms must actually reconcile.
    assert evaluation.expected_value_paise == (
        evaluation.expected_recovered_value_paise
        - evaluation.intervention_cost_paise
        - evaluation.friction_cost_paise
    )


def test_evaluating_an_unsupported_intervention_is_explicit() -> None:
    with pytest.raises(UnsupportedInterventionError):
        evaluate_candidate(
            intervention="wire_transfer",
            amount_paise=100_000,
            probability=RecoveryProbability(5_000),
            model=DEFAULT_ECONOMIC_MODEL,
        )


def test_evaluating_no_action_is_explicit() -> None:
    with pytest.raises(UnsupportedInterventionError):
        evaluate_candidate(
            intervention="no_action",
            amount_paise=100_000,
            probability=RecoveryProbability(5_000),
            model=DEFAULT_ECONOMIC_MODEL,
        )


def test_invalid_monetary_state_cannot_produce_an_evaluation() -> None:
    with pytest.raises(InvalidMoneyError):
        evaluate_candidate(
            intervention="reminder",
            amount_paise=-1,
            probability=RecoveryProbability(5_000),
            model=DEFAULT_ECONOMIC_MODEL,
        )

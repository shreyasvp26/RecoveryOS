"""Phase 16 tests: the deterministic economic intervention optimizer.

The controlling invariant under test is

    optimizer_decision_set  subset-of  policy_allowed_candidates

together with determinism, candidate-order invariance, explicit tie-breaking,
and controlled failure. A stub estimator is used wherever an exact expected
value must be asserted, so the optimizer's arithmetic and ranking are tested
independently of the production score model.
"""

from __future__ import annotations

import itertools

import pytest

from app.classification import ClassificationResult
from app.economics import (
    DEFAULT_ECONOMIC_MODEL,
    EXECUTABLE_INTERVENTIONS,
    EconomicModel,
    InterventionEconomics,
    RecoveryProbability,
    UnsupportedInterventionError,
)
from app.estimator import RecoveryProbabilityEstimator
from app.optimizer import (
    REASON_MAX_EXPECTED_VALUE,
    REASON_NO_ALLOWED_CANDIDATE,
    REASON_NO_CANDIDATES,
    AllowedCandidates,
    EconomicInterventionOptimizer,
    OptimizerError,
)
from app.policy import (
    RULE_COOLDOWN,
    RULE_CUSTOMER_LIMIT,
    RULE_DUPLICATE,
    RULE_FRAUD,
    RULE_SPEND_CAP,
    RULE_TERMINAL,
    PolicyDecision,
)
from app.models import CustomerHistory, PaymentEvent
from app.selector import INTERVENTION_PRIORITY, NO_ACTION

EVENT_ID = "evt_optimizer"
EVALUATED_AT = "2026-08-27T13:00:00+00:00"

ALL_FREE = EconomicModel(
    assumptions={
        name: InterventionEconomics(cost_paise=0, friction_bps=0)
        for name in EXECUTABLE_INTERVENTIONS
    }
)


def _event(amount_paise: int = 100_000) -> PaymentEvent:
    return PaymentEvent(
        event_id=EVENT_ID,
        order_id="order_optimizer",
        payment_id="pay_optimizer",
        customer_id="cust_optimizer",
        amount_paise=amount_paise,
        currency="INR",
        payment_method="card",
        failure_reason="bank_timeout",
        bank="HDFC",
        risk_flag="normal",
        customer_history=CustomerHistory(
            prior_successful_payments=4,
            prior_failed_payments=1,
            has_active_subscription=True,
        ),
        timestamp="2026-08-27T12:00:00+00:00",
    )


def _classification(candidates: tuple[str, ...]) -> ClassificationResult:
    return ClassificationResult(
        event_id=EVENT_ID,
        root_cause_category="transient",
        confidence=0.9,
        reasoning="advisory diagnosis for optimizer tests",
        candidate_interventions=candidates,
    )


def _allow(intervention: str) -> PolicyDecision:
    return PolicyDecision(
        event_id=EVENT_ID,
        proposed_intervention=intervention,
        allowed=True,
        denial_reason=None,
        policy_rules_applied=("fraud_check_passed",),
        evaluated_at=EVALUATED_AT,
    )


def _deny(intervention: str, reason: str) -> PolicyDecision:
    return PolicyDecision(
        event_id=EVENT_ID,
        proposed_intervention=intervention,
        allowed=False,
        denial_reason=reason,
        policy_rules_applied=(reason,),
        evaluated_at=EVALUATED_AT,
    )


class StubEstimator:
    """Returns fixed probabilities so expected values are exactly known."""

    def __init__(self, basis_points: dict[str, int]) -> None:
        self.basis_points = basis_points
        self.seen: list[str] = []

    def estimate(self, event, classification, intervention) -> RecoveryProbability:
        self.seen.append(intervention)
        return RecoveryProbability(self.basis_points[intervention])


class BrokenEstimator:
    """Returns something that is not a RecoveryProbability."""

    def __init__(self, value: object) -> None:
        self.value = value

    def estimate(self, event, classification, intervention):
        return self.value


def _optimizer(estimator, model: EconomicModel = ALL_FREE):
    return EconomicInterventionOptimizer(estimator=estimator, model=model)


def _select(
    candidates: tuple[str, ...],
    decisions: dict[str, PolicyDecision],
    estimator,
    model: EconomicModel = ALL_FREE,
    amount_paise: int = 100_000,
):
    allowed = AllowedCandidates.from_policy_decisions(candidates, decisions)
    return _optimizer(estimator, model).select(
        _event(amount_paise), _classification(candidates), allowed
    )


# ---------------------------------------------------------------------------
# Expected value drives the decision
# ---------------------------------------------------------------------------


def test_highest_expected_value_wins() -> None:
    decision = _select(
        ("retry_delayed", "payment_link"),
        {"retry_delayed": _allow("retry_delayed"), "payment_link": _allow("payment_link")},
        StubEstimator({"retry_delayed": 1_000, "payment_link": 9_000}),
    )
    # payment_link: 90000 vs retry_delayed: 10000, despite retry_delayed
    # outranking payment_link in the V1 priority ordering.
    assert decision.selected_intervention == "payment_link"
    assert decision.selection_reason == REASON_MAX_EXPECTED_VALUE
    assert decision.evaluations[0].expected_value_paise == 90_000


def test_lower_expected_value_loses_even_with_higher_v1_priority() -> None:
    decision = _select(
        ("retry_delayed", "reminder"),
        {"retry_delayed": _allow("retry_delayed"), "reminder": _allow("reminder")},
        StubEstimator({"retry_delayed": 100, "reminder": 8_000}),
    )
    assert decision.selected_intervention == "reminder"


def test_cost_and_friction_can_flip_the_winner() -> None:
    """A marginally better probability does not justify an expensive action."""
    model = EconomicModel(
        assumptions={
            **ALL_FREE.assumptions,
            "payment_link": InterventionEconomics(cost_paise=5_000, friction_bps=0),
        }
    )
    decision = _select(
        ("retry_delayed", "payment_link"),
        {"retry_delayed": _allow("retry_delayed"), "payment_link": _allow("payment_link")},
        StubEstimator({"retry_delayed": 5_000, "payment_link": 5_100}),
        model=model,
    )
    # payment_link: 51000 - 5000 = 46000 < retry_delayed: 50000
    assert decision.selected_intervention == "retry_delayed"


def test_the_reported_evaluations_reconcile_with_the_selection() -> None:
    decision = _select(
        ("retry_delayed", "payment_link", "reminder"),
        {
            "retry_delayed": _allow("retry_delayed"),
            "payment_link": _allow("payment_link"),
            "reminder": _allow("reminder"),
        },
        StubEstimator(
            {"retry_delayed": 1_000, "payment_link": 9_000, "reminder": 5_000}
        ),
        model=DEFAULT_ECONOMIC_MODEL,
    )
    best = max(decision.evaluations, key=lambda e: e.expected_value_paise)
    assert decision.selected_intervention == best.intervention
    # Evaluations are reported best-first.
    assert decision.evaluations[0].intervention == decision.selected_intervention


def test_very_large_amounts_are_handled_exactly() -> None:
    decision = _select(
        ("retry_delayed", "payment_link"),
        {"retry_delayed": _allow("retry_delayed"), "payment_link": _allow("payment_link")},
        StubEstimator({"retry_delayed": 5_000, "payment_link": 5_001}),
        amount_paise=100_000_000_000,
    )
    assert decision.selected_intervention == "payment_link"
    assert decision.evaluations[0].expected_value_paise == 50_010_000_000


# ---------------------------------------------------------------------------
# THE core safety invariant: only policy-allowed candidates are selectable
# ---------------------------------------------------------------------------


def test_a_denied_candidate_is_never_in_the_allowed_set() -> None:
    allowed = AllowedCandidates.from_policy_decisions(
        ("retry_delayed", "payment_link"),
        {
            "retry_delayed": _allow("retry_delayed"),
            "payment_link": _deny("payment_link", RULE_SPEND_CAP),
        },
    )
    assert allowed.allowed == ("retry_delayed",)
    assert "payment_link" in allowed.considered


def test_the_optimizer_never_evaluates_a_denied_candidate() -> None:
    """An enormous denied EV must not even be computed, let alone selected."""
    estimator = StubEstimator({"retry_delayed": 100, "payment_link": 10_000})
    decision = _select(
        ("retry_delayed", "payment_link"),
        {
            "retry_delayed": _allow("retry_delayed"),
            "payment_link": _deny("payment_link", RULE_FRAUD),
        },
        estimator,
    )
    assert decision.selected_intervention == "retry_delayed"
    assert "payment_link" not in estimator.seen
    assert all(e.intervention != "payment_link" for e in decision.evaluations)


@pytest.mark.parametrize(
    "rule",
    [
        RULE_FRAUD,
        RULE_TERMINAL,
        RULE_DUPLICATE,
        RULE_COOLDOWN,
        RULE_CUSTOMER_LIMIT,
        RULE_SPEND_CAP,
    ],
)
def test_no_policy_rule_can_be_overridden_by_a_huge_expected_value(rule: str) -> None:
    """Every V1 denial reason resists economic pressure identically.

    payment_link is denied and given a certain recovery of the full amount;
    retry_delayed is allowed with a near-zero probability. The denied option is
    worth ~500x more and must still be unavailable.
    """
    decision = _select(
        ("payment_link", "retry_delayed"),
        {
            "payment_link": _deny("payment_link", rule),
            "retry_delayed": _allow("retry_delayed"),
        },
        StubEstimator({"payment_link": 10_000, "retry_delayed": 20}),
        amount_paise=500_000,
    )
    assert decision.selected_intervention == "retry_delayed"
    assert decision.allowed_candidates == ("retry_delayed",)


def test_all_candidates_denied_produces_no_action() -> None:
    decision = _select(
        ("retry_delayed", "payment_link"),
        {
            "retry_delayed": _deny("retry_delayed", RULE_FRAUD),
            "payment_link": _deny("payment_link", RULE_FRAUD),
        },
        StubEstimator({"retry_delayed": 10_000, "payment_link": 10_000}),
    )
    assert decision.selected_intervention == NO_ACTION
    assert decision.selection_reason == REASON_NO_ALLOWED_CANDIDATE
    assert decision.is_actionable is False
    assert decision.evaluations == ()


def test_a_candidate_with_no_policy_decision_is_never_selectable() -> None:
    """Absence of a decision is not permission."""
    decision = _select(
        ("retry_delayed", "payment_link"),
        {"retry_delayed": _allow("retry_delayed")},
        StubEstimator({"retry_delayed": 100, "payment_link": 10_000}),
    )
    assert decision.selected_intervention == "retry_delayed"
    assert decision.allowed_candidates == ("retry_delayed",)


def test_a_decision_authorizing_a_different_intervention_is_malformed() -> None:
    """An ALLOW for X must not be reused to authorize Y."""
    with pytest.raises(OptimizerError, match="unrelated intervention"):
        AllowedCandidates.from_policy_decisions(
            ("payment_link",), {"payment_link": _allow("retry_delayed")}
        )


def test_a_forged_non_policy_decision_is_rejected() -> None:
    with pytest.raises(OptimizerError, match="must be a PolicyDecision"):
        AllowedCandidates.from_policy_decisions(
            ("payment_link",), {"payment_link": {"allowed": True}}
        )


def test_the_optimizer_refuses_a_candidate_set_not_derived_from_policy() -> None:
    """The only accepted input type is one that filters policy decisions itself."""
    with pytest.raises(OptimizerError, match="AllowedCandidates"):
        _optimizer(StubEstimator({})).select(
            _event(),
            _classification(("retry_delayed",)),
            ["retry_delayed", "payment_link"],  # type: ignore[arg-type]
        )


def test_mixed_allowed_and_denied_selects_only_from_the_allowed() -> None:
    decision = _select(
        ("retry_immediate", "retry_delayed", "payment_link", "reminder"),
        {
            "retry_immediate": _deny("retry_immediate", RULE_COOLDOWN),
            "retry_delayed": _allow("retry_delayed"),
            "payment_link": _deny("payment_link", RULE_SPEND_CAP),
            "reminder": _allow("reminder"),
        },
        StubEstimator(
            {
                "retry_immediate": 10_000,
                "retry_delayed": 3_000,
                "payment_link": 10_000,
                "reminder": 4_000,
            }
        ),
    )
    assert set(decision.allowed_candidates) == {"retry_delayed", "reminder"}
    assert decision.selected_intervention == "reminder"


def test_a_single_allowed_candidate_is_selected() -> None:
    decision = _select(
        ("reminder",), {"reminder": _allow("reminder")}, StubEstimator({"reminder": 1})
    )
    assert decision.selected_intervention == "reminder"


# ---------------------------------------------------------------------------
# no_action semantics
# ---------------------------------------------------------------------------


def test_an_empty_candidate_set_produces_a_controlled_no_action() -> None:
    decision = _select((), {}, StubEstimator({}))
    assert decision.selected_intervention == NO_ACTION
    assert decision.selection_reason == REASON_NO_CANDIDATES
    assert decision.is_actionable is False


def test_no_action_is_never_treated_as_an_executable_option() -> None:
    decision = _select(
        (NO_ACTION,), {}, StubEstimator({})
    )
    assert decision.selected_intervention == NO_ACTION
    assert decision.allowed_candidates == ()
    assert decision.selection_reason == REASON_NO_ALLOWED_CANDIDATE


def test_no_action_alongside_a_real_candidate_never_wins() -> None:
    decision = _select(
        (NO_ACTION, "reminder"),
        {"reminder": _allow("reminder")},
        StubEstimator({"reminder": 1}),
    )
    assert decision.selected_intervention == "reminder"


# ---------------------------------------------------------------------------
# Candidate-order invariance
# ---------------------------------------------------------------------------


def test_the_decision_is_invariant_under_every_candidate_permutation() -> None:
    candidates = ("retry_delayed", "payment_link", "reminder")
    decisions = {name: _allow(name) for name in candidates}
    probabilities = {
        "retry_delayed": 3_000,
        "payment_link": 7_000,
        "reminder": 5_000,
    }
    results = set()
    for permutation in itertools.permutations(candidates):
        decision = _select(
            permutation,
            {name: decisions[name] for name in permutation},
            StubEstimator(probabilities),
        )
        results.add(
            (
                decision.selected_intervention,
                tuple(e.intervention for e in decision.evaluations),
            )
        )
    assert len(results) == 1
    assert next(iter(results))[0] == "payment_link"


def test_order_invariance_holds_when_every_candidate_is_tied() -> None:
    candidates = ("retry_immediate", "retry_delayed", "payment_link", "reminder")
    probabilities = {name: 5_000 for name in candidates}
    selected = {
        _select(
            permutation,
            {name: _allow(name) for name in permutation},
            StubEstimator(probabilities),
        ).selected_intervention
        for permutation in itertools.permutations(candidates)
    }
    # The tie resolves to the highest V1 priority among them, every time.
    assert selected == {"retry_delayed"}


def test_repeated_evaluation_returns_an_identical_result() -> None:
    candidates = ("retry_delayed", "payment_link")
    probabilities = {"retry_delayed": 3_000, "payment_link": 7_000}
    results = {
        _select(
            candidates,
            {name: _allow(name) for name in candidates},
            StubEstimator(probabilities),
        )
        for _ in range(25)
    }
    assert len(results) == 1


# ---------------------------------------------------------------------------
# Tie-breaking
# ---------------------------------------------------------------------------


def test_an_exact_tie_falls_back_to_the_v1_priority_ordering() -> None:
    decision = _select(
        ("payment_link", "retry_delayed"),
        {"payment_link": _allow("payment_link"), "retry_delayed": _allow("retry_delayed")},
        StubEstimator({"payment_link": 5_000, "retry_delayed": 5_000}),
    )
    assert decision.selected_intervention == "retry_delayed"


def test_the_v1_tie_break_follows_the_documented_priority_at_every_level() -> None:
    """Each adjacent pair in the V1 ordering resolves in the documented direction."""
    for higher, lower in zip(INTERVENTION_PRIORITY, INTERVENTION_PRIORITY[1:]):
        decision = _select(
            (lower, higher),
            {lower: _allow(lower), higher: _allow(higher)},
            StubEstimator({lower: 5_000, higher: 5_000}),
        )
        assert decision.selected_intervention == higher


def test_a_near_tie_is_decided_by_economics_not_by_priority() -> None:
    """One paise of expected value is enough; priority never gets consulted."""
    decision = _select(
        ("retry_delayed", "payment_link"),
        {"retry_delayed": _allow("retry_delayed"), "payment_link": _allow("payment_link")},
        StubEstimator({"retry_delayed": 5_000, "payment_link": 5_001}),
    )
    assert decision.evaluations[0].expected_value_paise == 50_010
    assert decision.evaluations[1].expected_value_paise == 50_000
    assert decision.selected_intervention == "payment_link"


def test_the_v1_priority_ordering_has_exactly_one_definition() -> None:
    """The optimizer must not carry its own copy of the ordering."""
    from app import optimizer as optimizer_module

    assert optimizer_module.INTERVENTION_PRIORITY is INTERVENTION_PRIORITY


# ---------------------------------------------------------------------------
# Controlled failure: invalid state never becomes an economic decision
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("basis_points", [10_001, 50_000, -1, -10_000])
def test_an_out_of_domain_probability_cannot_produce_a_decision(
    basis_points: int,
) -> None:
    """A broken estimator stops the decision; it is never silently clamped."""
    from app.economics import InvalidProbabilityError

    with pytest.raises(InvalidProbabilityError):
        _select(
            ("reminder",),
            {"reminder": _allow("reminder")},
            StubEstimator({"reminder": basis_points}),
        )


@pytest.mark.parametrize("value", [0.5, 5_000, None, "5000"])
def test_a_malformed_estimator_output_is_rejected(value: object) -> None:
    with pytest.raises(OptimizerError, match="RecoveryProbability is required"):
        _select(
            ("reminder",), {"reminder": _allow("reminder")}, BrokenEstimator(value)
        )


def test_an_invalid_cost_cannot_be_configured() -> None:
    from app.economics import InvalidMoneyError

    with pytest.raises(InvalidMoneyError):
        InterventionEconomics(cost_paise=-500, friction_bps=0)


def test_an_invalid_friction_cannot_be_configured() -> None:
    from app.economics import EconomicsError

    with pytest.raises(EconomicsError):
        InterventionEconomics(cost_paise=0, friction_bps=-5)


def test_an_incomplete_economic_model_is_rejected_at_construction() -> None:
    from app.economics import EconomicsError

    with pytest.raises(EconomicsError):
        EconomicInterventionOptimizer(
            estimator=RecoveryProbabilityEstimator(),
            model=EconomicModel(assumptions={"reminder": InterventionEconomics(0, 0)}),
        )


def test_a_non_model_is_rejected_at_construction() -> None:
    with pytest.raises(OptimizerError, match="EconomicModel"):
        EconomicInterventionOptimizer(
            estimator=RecoveryProbabilityEstimator(), model={"reminder": 0}
        )


def test_an_unsupported_intervention_in_the_candidate_set_is_rejected() -> None:
    with pytest.raises(OptimizerError, match="not one of"):
        AllowedCandidates.from_policy_decisions(("wire_transfer",), {})


def test_a_duplicate_candidate_is_rejected() -> None:
    with pytest.raises(OptimizerError, match="duplicate"):
        AllowedCandidates.from_policy_decisions(
            ("reminder", "reminder"), {"reminder": _allow("reminder")}
        )


def test_a_non_sequence_candidate_set_is_rejected() -> None:
    with pytest.raises(OptimizerError, match="sequence"):
        AllowedCandidates.from_policy_decisions("reminder", {})  # type: ignore[arg-type]


def test_mismatched_event_and_classification_are_rejected() -> None:
    allowed = AllowedCandidates.from_policy_decisions(
        ("reminder",), {"reminder": _allow("reminder")}
    )
    other = ClassificationResult.from_dict(
        dict(_classification(("reminder",)).to_dict(), event_id="evt_other")
    )
    with pytest.raises(OptimizerError, match="do not match"):
        _optimizer(StubEstimator({"reminder": 100})).select(_event(), other, allowed)


def test_a_non_event_input_is_rejected() -> None:
    allowed = AllowedCandidates.from_policy_decisions(
        ("reminder",), {"reminder": _allow("reminder")}
    )
    with pytest.raises(OptimizerError, match="PaymentEvent"):
        _optimizer(StubEstimator({"reminder": 100})).select(
            {"event_id": EVENT_ID}, _classification(("reminder",)), allowed  # type: ignore[arg-type]
        )


def test_the_production_estimator_and_model_compose_without_error() -> None:
    """The real estimator and real cost model produce a valid live decision."""
    candidates = tuple(sorted(EXECUTABLE_INTERVENTIONS))
    decision = _select(
        candidates,
        {name: _allow(name) for name in candidates},
        RecoveryProbabilityEstimator(),
        model=DEFAULT_ECONOMIC_MODEL,
    )
    assert decision.selected_intervention in EXECUTABLE_INTERVENTIONS
    assert len(decision.evaluations) == len(candidates)
    # transient bank_timeout on a card: a delayed retry is the economic choice.
    assert decision.selected_intervention == "retry_delayed"

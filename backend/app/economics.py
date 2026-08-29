"""Deterministic economic primitives for V2 intervention selection (Phase 16).

This module owns the arithmetic of the V2 decision engine: the probability
representation, the intervention cost model, the intervention friction model,
and the expected-value calculation. It contains no policy logic, no execution,
no LLM call, no network access, and no benchmark/ground-truth dependency.

MONETARY REPRESENTATION
-----------------------
Money is integer paise everywhere, matching the locked ``PaymentEvent``
contract (``amount_paise``) and the persisted ``InterventionAttempt``
(``cost_paise``). Binary floating point is never used for money.

PROBABILITY REPRESENTATION
--------------------------
Probabilities are integer basis points on ``[0, PROBABILITY_SCALE]`` where
``PROBABILITY_SCALE == 10_000``. 10000 bps == 1.0, 0 bps == 0.0. Integer basis
points keep every downstream calculation exact and reproducible.

ROUNDING POLICY
---------------
Every paise-valued product uses floor division on non-negative integers:

    expected_recovered_value_paise = amount_paise * probability_bps // 10_000
    friction_cost_paise            = amount_paise * friction_bps    // 10_000

Floor division is exact, deterministic, and independent of platform floating
point. It is also conservative: it never over-states expected recovery and
never under-states friction. Python's ``round()`` is deliberately not used.

COST AND FRICTION ARE MODELLED ASSUMPTIONS
------------------------------------------
The values in ``DEFAULT_ECONOMIC_MODEL`` are RecoveryOS controlled evaluation
assumptions. They are NOT Razorpay commercial pricing, and the friction figures
are NOT observed customer behaviour. See docs/ECONOMIC_MODEL.md.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

from .classification import CANDIDATE_INTERVENTIONS
from .selector import NO_ACTION

# Basis-point scale: 10000 bps == probability 1.0 == 100%.
PROBABILITY_SCALE: int = 10_000

# The interventions that can actually be executed. no_action is excluded by
# construction: it is not an executable intervention and is never priced,
# never scored, and never selected as an action.
EXECUTABLE_INTERVENTIONS: frozenset[str] = frozenset(
    CANDIDATE_INTERVENTIONS - {NO_ACTION}
)


class EconomicsError(Exception):
    """Base class for all explicit economic-model failures."""


class InvalidProbabilityError(EconomicsError):
    """A recovery probability is malformed or outside [0, PROBABILITY_SCALE].

    Invalid probabilities are rejected, never clamped. A caller that supplies
    a probability above 1.0 or below 0.0 has a broken estimator, and silently
    repairing it would produce an economic decision from invalid state.
    """


class InvalidMoneyError(EconomicsError):
    """A monetary value is malformed or negative."""


class UnsupportedInterventionError(EconomicsError):
    """No economic assumption exists for the requested intervention."""


def _require_non_negative_int(value: Any, name: str) -> int:
    """Return value when it is a non-negative plain int, else fail explicitly."""
    if isinstance(value, bool) or not isinstance(value, int):
        raise InvalidMoneyError(f"{name} must be an integer, got {value!r}")
    if value < 0:
        raise InvalidMoneyError(f"{name} must be non-negative, got {value}")
    return value


@dataclass(frozen=True)
class RecoveryProbability:
    """An estimated recovery probability in integer basis points.

    The valid domain is ``0 <= basis_points <= PROBABILITY_SCALE``. Anything
    else raises ``InvalidProbabilityError``; the value is never clamped here.
    """

    basis_points: int

    def __post_init__(self) -> None:
        if isinstance(self.basis_points, bool) or not isinstance(
            self.basis_points, int
        ):
            raise InvalidProbabilityError(
                f"basis_points must be an integer, got {self.basis_points!r}"
            )
        if not (0 <= self.basis_points <= PROBABILITY_SCALE):
            raise InvalidProbabilityError(
                f"basis_points must satisfy 0 <= p <= {PROBABILITY_SCALE}, "
                f"got {self.basis_points}"
            )

    @property
    def as_fraction(self) -> float:
        """The probability as a float, for display and logging only.

        Never use this for money arithmetic; the integer basis points are the
        authoritative representation.
        """
        return self.basis_points / PROBABILITY_SCALE


@dataclass(frozen=True)
class InterventionEconomics:
    """The modelled economic assumptions for one executable intervention.

    ``cost_paise`` is the direct cost of performing the intervention once.

    ``friction_bps`` converts customer friction into money: friction is
    modelled as a proportion of the transaction value, on the assumption that
    the relationship cost of annoying a customer scales with the size of the
    transaction being pursued. The monetary deduction is therefore

        friction_cost_paise = amount_paise * friction_bps // PROBABILITY_SCALE

    so friction is never an unexplained bare number.
    """

    cost_paise: int
    friction_bps: int

    def __post_init__(self) -> None:
        _require_non_negative_int(self.cost_paise, "cost_paise")
        if isinstance(self.friction_bps, bool) or not isinstance(
            self.friction_bps, int
        ):
            raise EconomicsError(
                f"friction_bps must be an integer, got {self.friction_bps!r}"
            )
        if not (0 <= self.friction_bps <= PROBABILITY_SCALE):
            raise EconomicsError(
                f"friction_bps must satisfy 0 <= f <= {PROBABILITY_SCALE}, "
                f"got {self.friction_bps}"
            )


@dataclass(frozen=True)
class EconomicModel:
    """Cost and friction assumptions covering every executable intervention.

    Construction fails unless the model prices EXACTLY the executable
    interventions: a missing entry would make an intervention silently free,
    and an extra entry would price something that cannot be executed.
    """

    assumptions: Mapping[str, InterventionEconomics]

    def __post_init__(self) -> None:
        if not isinstance(self.assumptions, Mapping):
            raise EconomicsError("assumptions must be a mapping")
        missing = EXECUTABLE_INTERVENTIONS - set(self.assumptions)
        if missing:
            raise EconomicsError(
                f"economic model is missing assumptions for {sorted(missing)}"
            )
        extra = set(self.assumptions) - EXECUTABLE_INTERVENTIONS
        if extra:
            raise EconomicsError(
                f"economic model prices non-executable interventions "
                f"{sorted(extra)}"
            )
        for intervention, economics in self.assumptions.items():
            if not isinstance(economics, InterventionEconomics):
                raise EconomicsError(
                    f"assumption for {intervention!r} must be an "
                    "InterventionEconomics"
                )
        object.__setattr__(self, "assumptions", dict(self.assumptions))

    def for_intervention(self, intervention: str) -> InterventionEconomics:
        """Return the assumptions for an intervention, or fail explicitly."""
        economics = self.assumptions.get(intervention)
        if economics is None:
            raise UnsupportedInterventionError(
                f"no economic assumption exists for intervention "
                f"{intervention!r}"
            )
        return economics


# RecoveryOS controlled evaluation assumptions — NOT Razorpay commercial rates
# and NOT observed customer-friction measurements.
#
# cost_paise reflects the modelled direct cost of performing the action once:
# API-only retries cost nothing to attempt; a messaging-based nudge costs a
# notional ₹0.20; a hosted Payment Link costs a notional ₹1.00 to create and
# deliver.
#
# friction_bps reflects the modelled customer-experience cost, expressed as a
# proportion of the transaction value: a background retry is invisible to the
# customer and carries no friction, while any action that demands customer
# attention or effort carries progressively more.
DEFAULT_ECONOMIC_MODEL = EconomicModel(
    assumptions={
        "retry_immediate": InterventionEconomics(cost_paise=0, friction_bps=0),
        "retry_delayed": InterventionEconomics(cost_paise=0, friction_bps=0),
        "reminder": InterventionEconomics(cost_paise=20, friction_bps=5),
        "payment_link": InterventionEconomics(cost_paise=100, friction_bps=10),
        "alternate_method_prompt": InterventionEconomics(
            cost_paise=20, friction_bps=15
        ),
    }
)


def expected_recovered_value_paise(
    amount_paise: int, probability: RecoveryProbability
) -> int:
    """Return ``amount x probability`` in paise, floored to whole paise."""
    _require_non_negative_int(amount_paise, "amount_paise")
    if not isinstance(probability, RecoveryProbability):
        raise InvalidProbabilityError(
            "probability must be a RecoveryProbability, got "
            f"{type(probability).__name__}"
        )
    return amount_paise * probability.basis_points // PROBABILITY_SCALE


def friction_cost_paise(amount_paise: int, friction_bps: int) -> int:
    """Return the monetary friction deduction in paise, floored."""
    _require_non_negative_int(amount_paise, "amount_paise")
    if isinstance(friction_bps, bool) or not isinstance(friction_bps, int):
        raise EconomicsError(f"friction_bps must be an integer, got {friction_bps!r}")
    if not (0 <= friction_bps <= PROBABILITY_SCALE):
        raise EconomicsError(
            f"friction_bps must satisfy 0 <= f <= {PROBABILITY_SCALE}, "
            f"got {friction_bps}"
        )
    return amount_paise * friction_bps // PROBABILITY_SCALE


@dataclass(frozen=True)
class CandidateEvaluation:
    """The fully-decomposed economics of one policy-allowed candidate.

    Every term of the expected-value equation is exposed so that a decision
    can be explained and re-derived by hand from the audit trail.
    """

    intervention: str
    estimated_probability_bps: int
    amount_paise: int
    expected_recovered_value_paise: int
    intervention_cost_paise: int
    friction_cost_paise: int
    expected_value_paise: int

    def to_dict(self) -> dict[str, Any]:
        """Serialize the evaluation for audit and trace output."""
        return {
            "intervention": self.intervention,
            "estimated_probability_bps": self.estimated_probability_bps,
            "amount_paise": self.amount_paise,
            "expected_recovered_value_paise": self.expected_recovered_value_paise,
            "intervention_cost_paise": self.intervention_cost_paise,
            "friction_cost_paise": self.friction_cost_paise,
            "expected_value_paise": self.expected_value_paise,
        }


def evaluate_candidate(
    intervention: str,
    amount_paise: int,
    probability: RecoveryProbability,
    model: EconomicModel,
) -> CandidateEvaluation:
    """Compute the expected value of one candidate intervention.

        expected_value = amount x probability
                         - intervention_cost
                         - friction_cost

    The result may be negative: an intervention that costs more than it is
    expected to recover is a real and meaningful outcome, not an error.
    """
    if intervention not in EXECUTABLE_INTERVENTIONS:
        raise UnsupportedInterventionError(
            f"intervention must be one of {sorted(EXECUTABLE_INTERVENTIONS)}, "
            f"got {intervention!r}"
        )
    if not isinstance(model, EconomicModel):
        raise EconomicsError("model must be an EconomicModel")

    economics = model.for_intervention(intervention)
    recovered = expected_recovered_value_paise(amount_paise, probability)
    friction = friction_cost_paise(amount_paise, economics.friction_bps)
    return CandidateEvaluation(
        intervention=intervention,
        estimated_probability_bps=probability.basis_points,
        amount_paise=amount_paise,
        expected_recovered_value_paise=recovered,
        intervention_cost_paise=economics.cost_paise,
        friction_cost_paise=friction,
        expected_value_paise=recovered - economics.cost_paise - friction,
    )

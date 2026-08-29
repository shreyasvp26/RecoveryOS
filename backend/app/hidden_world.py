"""Signal-bearing hidden world — Phase 17 evaluation-only ground truth.

WHAT THIS IS
------------
A synthetic, deterministic, frozen model of "what would actually have happened"
for every (event, intervention) pair. It is the benchmark's ground truth. It is
NOT RecoveryOS's belief about the world, and it is NOT a production module.

WHY IT REPLACES THE PHASE 8 MODEL FOR PHASE 17
----------------------------------------------
``outcome_model.py`` draws an independent uniform probability per
(event_id, intervention). That world carries no causal signal: no failure
reason, payment rail, or customer history changes anything, and therefore no
benchmark built on it can distinguish good targeting from lucky targeting. It
remains in the repository, untouched, as the frozen Phase 9 compatibility
world (see docs/BENCHMARK.md); Phase 17 evaluation uses THIS module instead.

THE PROBABILITY IS A FUNCTION OF FEATURES, NOT OF IDENTITY
----------------------------------------------------------
``P_true`` is computed from the event's legitimate observable domain features
only: failure_reason, payment_method, customer_history, has_active_subscription
and amount band. ``event_id``, ``order_id``, ``payment_id``, ``customer_id``,
``timestamp``, ``bank`` and event ORDER never enter the probability. There is
no ``probabilities[event_id]`` mapping anywhere in this module: two events with
identical observable features necessarily have identical hidden probabilities.

Event identity participates in exactly one place — realizing the Bernoulli
outcome (the coin flip), which needs some per-event entropy. That is the common
randomness contract below, and it never changes the probability itself.

STRATEGY INDEPENDENCE
---------------------
``P_true(event, intervention)`` takes no strategy argument and reads no run
state. Every value exists before any strategy runs, so V1, V2, Naive Retry, No
Action and the Oracle all face the identical world regardless of which of them
executes first, or whether the others execute at all.

COMMON RANDOMNESS CONTRACT
--------------------------
    draw_bps = blake2b(f"{RANDOMIZATION_VERSION}|{seed}|{event_id}"
                       f"|{intervention}|{replication}") mod 10_000
    recovered = draw_bps < P_true_bps

The draw is a pure function of that key. It never uses a shared mutable RNG
stream, ``random.seed``, the wall clock, a UUID, the network, or the number or
order of events simulated before it. Reversing the event list, reordering the
strategies, or replaying a single event in isolation all produce byte-identical
outcomes.

HONESTY STATEMENT
-----------------
Every coefficient below is a controlled synthetic assumption chosen to express
an interpretable causal story about payment failure, frozen before any Phase 17
benchmark result was observed. They are NOT measured recovery rates, and no
figure derived from them is a claim about production Razorpay performance.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Mapping

from .classification import CANDIDATE_INTERVENTIONS
from .economics import (
    EconomicModel,
    InterventionEconomics,
    PROBABILITY_SCALE,
    friction_cost_paise,
)
from .models import PaymentEvent
from .selector import NO_ACTION

# Bumping this string changes every realized outcome, so it is part of the
# frozen methodology and is recorded on every benchmark report.
RANDOMIZATION_VERSION = "phase17-blake2b-uniform-v1"

_INTERVENTIONS: tuple[str, ...] = tuple(sorted(CANDIDATE_INTERVENTIONS))

# The amount above which a failed payment is modelled as commanding more
# customer attention (a ₹10,000 failure gets read; a ₹50 failure does not).
HIGH_VALUE_THRESHOLD_PAISE = 1_000_000


class HiddenWorldError(Exception):
    """The hidden world was asked for something it cannot answer honestly."""


def _frozen(table: Mapping[str, Any]) -> Mapping[str, Any]:
    """Read-only view of a coefficient table, including its nested tables.

    The hidden world is frozen methodology, not configuration. Exposing the
    tables as read-only views means nothing holding a module reference — a
    strategy, a test, or a future phase — can retune ground truth at runtime.
    """
    return MappingProxyType(
        {
            key: MappingProxyType(dict(value)) if isinstance(value, Mapping) else value
            for key, value in table.items()
        }
    )


# ---------------------------------------------------------------------------
# The frozen hidden model, in integer basis points
# ---------------------------------------------------------------------------

# base(intervention): the world's unconditional recovery rate per action.
#
# no_action is NOT zero. A failed payment is not a closed door: some customers
# notice and re-pay on their own. Modelling the control arm as a real passive
# recovery process is what makes "intervening was not worth it" an outcome the
# benchmark can actually express, instead of making every intervention look
# free. This baseline is used consistently for every arm that attempts nothing.
BASE_TRUE_BPS: Mapping[str, int] = _frozen(
    {
        NO_ACTION: 500,
        "retry_immediate": 1500,
        "retry_delayed": 2600,
        "payment_link": 2400,
        "reminder": 1700,
        "alternate_method_prompt": 1900,
    }
)

# failure_reason x intervention. This is the primary source of signal: it is
# what makes the correct action DIFFER by failure class, so that no single
# intervention is globally best and a benchmark over these events genuinely
# tests targeting rather than enthusiasm.
FAILURE_REASON_TRUE_BPS: Mapping[str, Mapping[str, int]] = _frozen(
    {
        # A bank outage passes. Retrying into it fails again; waiting works.
        "bank_timeout": {
            NO_ACTION: 300,
            "retry_immediate": -900,
            "retry_delayed": 2200,
            "payment_link": 200,
            "reminder": 0,
            "alternate_method_prompt": 700,
        },
        "network_issue": {
            NO_ACTION: 200,
            "retry_immediate": -500,
            "retry_delayed": 1800,
            "payment_link": 100,
            "reminder": 0,
            "alternate_method_prompt": 400,
        },
        # The account needs funding: that takes time AND a prompt to the payer.
        "insufficient_funds": {
            NO_ACTION: -100,
            "retry_immediate": -1300,
            "retry_delayed": 1100,
            "payment_link": 500,
            "reminder": 1400,
            "alternate_method_prompt": 200,
        },
        # Authentication needs the customer present, so silent retries do not
        # help and a customer-facing path does.
        "authentication_failed": {
            NO_ACTION: -200,
            "retry_immediate": -800,
            "retry_delayed": -300,
            "payment_link": 1500,
            "reminder": 400,
            "alternate_method_prompt": 1200,
        },
        # The instrument itself is dead: only a different instrument recovers.
        "expired_card": {
            NO_ACTION: -300,
            "retry_immediate": -1400,
            "retry_delayed": -1300,
            "payment_link": 1800,
            "reminder": -200,
            "alternate_method_prompt": 2000,
        },
        "declined_by_bank": {
            NO_ACTION: -200,
            "retry_immediate": -900,
            "retry_delayed": -400,
            "payment_link": 600,
            "reminder": 0,
            "alternate_method_prompt": 1300,
        },
        # Terminal refusals. The world allows a sliver of passive recovery and
        # crushes every intervention: money spent here is money burned. This is
        # where an unconditional retry baseline is supposed to lose.
        "transaction_declined": {
            NO_ACTION: -400,
            "retry_immediate": -4500,
            "retry_delayed": -4500,
            "payment_link": -4500,
            "reminder": -4500,
            "alternate_method_prompt": -4500,
        },
        "payment_failed": {
            NO_ACTION: -400,
            "retry_immediate": -4500,
            "retry_delayed": -4500,
            "payment_link": -4500,
            "reminder": -4500,
            "alternate_method_prompt": -4500,
        },
    }
)

# payment_method x intervention: the rail changes which recovery path is easy.
PAYMENT_METHOD_TRUE_BPS: Mapping[str, Mapping[str, int]] = _frozen(
    {
        "upi": {
            "retry_immediate": 400,
            "retry_delayed": 500,
            "payment_link": -100,
            "alternate_method_prompt": 200,
        },
        "card": {
            "retry_delayed": -100,
            "payment_link": 400,
            "alternate_method_prompt": 300,
        },
        "netbanking": {
            "retry_immediate": -200,
            "retry_delayed": 300,
            "payment_link": 200,
        },
        "wallet": {
            "retry_immediate": 100,
            "reminder": 300,
            "alternate_method_prompt": 500,
        },
    }
)

# Customer reliability bands. The band EDGES are deliberately not the
# estimator's band edges (estimator.py uses 10/3/0 and 5/3): RecoveryOS's model
# of payer reliability is close to the world's but not identical, which is
# exactly the kind of honest estimation error the benchmark must be able to
# punish. Applied uniformly across every intervention including no_action.
WORLD_RELIABLE_MIN_SUCCESSES = 15
WORLD_ESTABLISHED_MIN_SUCCESSES = 5
WORLD_UNRELIABLE_MIN_FAILURES = 4
WORLD_STRUGGLING_MIN_FAILURES = 2

WORLD_RELIABLE_BPS = 500
WORLD_ESTABLISHED_BPS = 250
WORLD_NEW_CUSTOMER_BPS = -350
WORLD_UNRELIABLE_BPS = -700
WORLD_STRUGGLING_BPS = -250

# A live mandate means the rails can re-attempt without the payer lifting a
# finger, and makes asking the payer to act comparatively wasteful.
SUBSCRIPTION_TRUE_BPS: Mapping[str, int] = _frozen(
    {
        NO_ACTION: 300,
        "retry_immediate": 500,
        "retry_delayed": 700,
        "payment_link": -200,
        "reminder": 200,
        "alternate_method_prompt": 0,
    }
)

# Amount x intervention interaction. A large failed payment is one the customer
# actually notices, so customer-facing recovery lands better; a large failure
# is also less likely to resolve itself silently. The estimator deliberately
# ignores amount when estimating probability (it enters V2 only through the
# expected-value multiplication), so this term is a guaranteed source of
# genuine estimation error rather than a mirror of RecoveryOS's beliefs.
HIGH_VALUE_TRUE_BPS: Mapping[str, int] = _frozen(
    {
        NO_ACTION: -100,
        "payment_link": 300,
        "reminder": 200,
        "alternate_method_prompt": 200,
    }
)


def _saturate_bps(value: int) -> int:
    """Bound an additive score into the probability domain [0, 10000].

    Saturation is the model's DEFINED behaviour: the score is a sum of bounded
    coefficients, so an extreme but legitimate feature combination can push the
    total past an endpoint, and a true probability of 0 or 1 is the correct
    reading of that. Nothing invalid is being repaired.
    """
    return max(0, min(PROBABILITY_SCALE, value))


def _lookup(table: Mapping[str, Mapping[str, int]], feature: str, intervention: str) -> int:
    """Return a coefficient, treating an unmodelled feature value as neutral.

    ``failure_reason`` is a free string in the locked domain contract, so an
    unrecognized value contributes nothing rather than being invented.
    """
    return table.get(feature, {}).get(intervention, 0)


def _customer_history_bps(event: PaymentEvent) -> int:
    """Score the payer's track record, independently of the intervention."""
    history = event.customer_history
    score = 0
    if history.prior_successful_payments >= WORLD_RELIABLE_MIN_SUCCESSES:
        score += WORLD_RELIABLE_BPS
    elif history.prior_successful_payments >= WORLD_ESTABLISHED_MIN_SUCCESSES:
        score += WORLD_ESTABLISHED_BPS
    elif history.prior_successful_payments == 0:
        score += WORLD_NEW_CUSTOMER_BPS

    if history.prior_failed_payments >= WORLD_UNRELIABLE_MIN_FAILURES:
        score += WORLD_UNRELIABLE_BPS
    elif history.prior_failed_payments >= WORLD_STRUGGLING_MIN_FAILURES:
        score += WORLD_STRUGGLING_BPS
    return score


def true_probability_bps(event: PaymentEvent, intervention: str) -> int:
    """Return ``P_true(recovery | event, intervention)`` in basis points.

        score = base(intervention)
              + failure_reason x intervention
              + payment_method x intervention
              + customer_history
              + subscription x intervention
              + amount_band x intervention

    A pure function of the event's observable features and the intervention.
    It takes no seed, no strategy, no run state, and no event identity.
    """
    if not isinstance(event, PaymentEvent):
        raise HiddenWorldError(
            f"expected a PaymentEvent, got {type(event).__name__}"
        )
    if intervention not in CANDIDATE_INTERVENTIONS:
        raise HiddenWorldError(
            f"intervention {intervention!r} is not one of "
            f"{sorted(CANDIDATE_INTERVENTIONS)}"
        )

    score = BASE_TRUE_BPS[intervention]
    score += _lookup(FAILURE_REASON_TRUE_BPS, event.failure_reason, intervention)
    score += _lookup(PAYMENT_METHOD_TRUE_BPS, event.payment_method, intervention)
    score += _customer_history_bps(event)
    if event.customer_history.has_active_subscription:
        score += SUBSCRIPTION_TRUE_BPS.get(intervention, 0)
    if event.amount_paise >= HIGH_VALUE_THRESHOLD_PAISE:
        score += HIGH_VALUE_TRUE_BPS.get(intervention, 0)
    return _saturate_bps(score)


def true_expected_value_paise(
    event: PaymentEvent, intervention: str, model: EconomicModel
) -> int:
    """Return the world's true expected value of one action on one event.

        true_EV = amount x P_true - intervention_cost - friction_cost

    Integer paise throughout, using the SAME cost and friction assumptions the
    V2 optimizer uses, so that regret measures decision quality and not a
    difference in accounting. ``no_action`` is not an executable intervention
    and is therefore never priced: its true EV is its passive recovery value.
    """
    if not isinstance(model, EconomicModel):
        raise HiddenWorldError("model must be an EconomicModel")
    probability_bps = true_probability_bps(event, intervention)
    recovered = event.amount_paise * probability_bps // PROBABILITY_SCALE
    if intervention == NO_ACTION:
        return recovered
    economics: InterventionEconomics = model.for_intervention(intervention)
    friction = friction_cost_paise(event.amount_paise, economics.friction_bps)
    return recovered - economics.cost_paise - friction


def deterministic_draw_bps(
    seed: int, event_id: str, intervention: str, replication: int
) -> int:
    """Return the common-randomness uniform draw on [0, 10000).

    A pure function of the key ``(randomization version, seed, event identity,
    intervention, replication)``. Two different strategies that pick the same
    intervention on the same event therefore see the SAME coin, and a strategy
    that runs first cannot consume a draw that a later strategy needed.
    """
    if type(seed) is not int:
        raise HiddenWorldError(f"seed must be an integer, got {seed!r}")
    if not isinstance(event_id, str) or not event_id.strip():
        raise HiddenWorldError("event_id must be a non-empty string")
    if intervention not in CANDIDATE_INTERVENTIONS:
        raise HiddenWorldError(
            f"intervention {intervention!r} is not one of "
            f"{sorted(CANDIDATE_INTERVENTIONS)}"
        )
    if type(replication) is not int or replication < 0:
        raise HiddenWorldError("replication must be a non-negative integer")

    key = (
        f"{RANDOMIZATION_VERSION}|{seed}|{event_id}|{intervention}|{replication}"
    )
    digest = hashlib.blake2b(key.encode("utf-8"), digest_size=8).digest()
    return int.from_bytes(digest, "big") % PROBABILITY_SCALE


@dataclass(frozen=True)
class HiddenOutcome:
    """One realized ground-truth outcome. Evaluation-only.

    Carries the hidden probability and the draw because the evaluation layer
    needs them to compute regret and to audit the run. This type is never
    returned through a production API and never reaches a decision module.
    """

    event_id: str
    intervention: str
    true_probability_bps: int
    draw_bps: int
    recovered: bool
    recovered_amount_paise: int

    def to_dict(self) -> dict[str, Any]:
        """Serialize for benchmark artifacts and test inspection."""
        return {
            "event_id": self.event_id,
            "intervention": self.intervention,
            "true_probability_bps": self.true_probability_bps,
            "draw_bps": self.draw_bps,
            "recovered": self.recovered,
            "recovered_amount_paise": self.recovered_amount_paise,
        }


class HiddenWorld:
    """The frozen synthetic world one benchmark run is evaluated against.

    Holds only the outcome seed and the replication index. It deliberately
    holds NO per-event state: every answer is recomputed from the event's
    features, so no strategy can mutate the world by asking it a question, and
    asking about event B can never change the answer for event A.
    """

    def __init__(
        self, outcome_seed: int, model: EconomicModel, replication: int = 0
    ) -> None:
        if type(outcome_seed) is not int:
            raise HiddenWorldError(
                f"outcome_seed must be an integer, got {outcome_seed!r}"
            )
        if type(replication) is not int or replication < 0:
            raise HiddenWorldError("replication must be a non-negative integer")
        if not isinstance(model, EconomicModel):
            raise HiddenWorldError("model must be an EconomicModel")
        self._outcome_seed = outcome_seed
        self._replication = replication
        self._model = model

    @property
    def outcome_seed(self) -> int:
        """The seed every outcome draw in this world is keyed on."""
        return self._outcome_seed

    @property
    def replication(self) -> int:
        """The replication index this world realizes."""
        return self._replication

    @property
    def economic_model(self) -> EconomicModel:
        """The cost/friction assumptions used for true expected value."""
        return self._model

    @property
    def interventions(self) -> tuple[str, ...]:
        """Every intervention the world models, including ``no_action``."""
        return _INTERVENTIONS

    def probability_bps(self, event: PaymentEvent, intervention: str) -> int:
        """``P_true`` in basis points for one event/intervention."""
        return true_probability_bps(event, intervention)

    def true_ev_paise(self, event: PaymentEvent, intervention: str) -> int:
        """The world's true expected value in paise for one event/intervention."""
        return true_expected_value_paise(event, intervention, self._model)

    def realize(self, event: PaymentEvent, intervention: str) -> HiddenOutcome:
        """Realize the ground-truth outcome of one action on one event.

        Recovery is a single Bernoulli draw against ``P_true``. Realizing an
        outcome is READ-ONLY: it records nothing and changes nothing, so the
        same call always returns the same result no matter how many times, in
        what order, or by which strategy it is made.
        """
        if not isinstance(event, PaymentEvent):
            raise HiddenWorldError(
                f"expected a PaymentEvent, got {type(event).__name__}"
            )
        probability = true_probability_bps(event, intervention)
        draw = deterministic_draw_bps(
            self._outcome_seed, event.event_id, intervention, self._replication
        )
        recovered = draw < probability
        return HiddenOutcome(
            event_id=event.event_id,
            intervention=intervention,
            true_probability_bps=probability,
            draw_bps=draw,
            recovered=recovered,
            recovered_amount_paise=event.amount_paise if recovered else 0,
        )

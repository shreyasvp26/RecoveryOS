"""Deterministic recovery probability estimator (Phase 16).

Estimates ``P(recovery | event, intervention)`` from information that is
legitimately available at decision time. The estimator is a small, transparent,
additive score model in integer basis points:

    probability_bps = saturate(
          base(intervention)
        + root_cause_adjustment(intervention)
        + failure_reason_adjustment(intervention)
        + payment_method_adjustment(intervention)
        + customer_history_adjustment(intervention)
    )

ISOLATION GUARANTEES (verified by test_optimizer_isolation.py)
--------------------------------------------------------------
No LLM call, no network access, no randomness, no wall-clock time, no
persistence, and no import of ``outcome_model``, ``outcome``, ``benchmark`` or
any other evaluation-layer module. The hidden benchmark ground truth is never
read, and no ``event_id`` ever influences an estimate.

WHAT THE ESTIMATOR DELIBERATELY DOES NOT USE
--------------------------------------------
``bank``       — the repository holds no bank-reliability data, so any
                 bank-specific coefficient would be fabricated.
``amount``     — value enters the decision through the expected-value
                 calculation; using it again here would double-count it.
``confidence`` — the classifier's self-reported confidence is a non-
                 deterministic LLM output; depending on it would make the
                 estimate non-reproducible.
``event_id``, ``timestamp``, ``order_id``, ``payment_id``, ``customer_id`` —
                 identifiers carry no recovery signal and are the exact route
                 by which benchmark ground truth could leak in.

HONESTY STATEMENT
-----------------
Every coefficient below is a RecoveryOS controlled evaluation assumption
expressing an interpretable causal story about payment failure. They are NOT
empirically validated production recovery rates and were NOT fitted against
the benchmark's hidden outcome model. See docs/ECONOMIC_MODEL.md.
"""

from __future__ import annotations

from types import MappingProxyType
from typing import Any, Mapping

from .classification import ClassificationResult
from .economics import (
    EXECUTABLE_INTERVENTIONS,
    PROBABILITY_SCALE,
    RecoveryProbability,
    UnsupportedInterventionError,
)
from .models import PaymentEvent

def _frozen(table: Mapping[str, Any]) -> Mapping[str, Any]:
    """Read-only view of a coefficient table, including its nested tables.

    The tables below are RecoveryOS evaluation constants, not configuration.
    Exposing them as read-only views keeps the estimator deterministic:
    nothing holding a module reference can retune the model at runtime.
    """
    return MappingProxyType(
        {
            key: MappingProxyType(dict(value)) if isinstance(value, Mapping) else value
            for key, value in table.items()
        }
    )


# Modelled baseline recovery likelihood per intervention, before any event
# feature is considered. Ordering reflects the causal story that automated
# re-attempts succeed more often than actions requiring customer effort, and
# that a delayed re-attempt beats an immediate one because most payment
# failures are transient.
BASE_RECOVERY_BPS: Mapping[str, int] = _frozen(
    {
        "retry_immediate": 1800,
        "retry_delayed": 3200,
        "payment_link": 2800,
        "reminder": 2000,
        "alternate_method_prompt": 2200,
    }
)

# Adjustment by the classifier's advisory root-cause category. A transient
# fault rewards re-attempting; a fault needing the customer to act rewards
# reaching the customer; fraud and terminal categories suppress everything
# (those events are separately and authoritatively denied by policy).
ROOT_CAUSE_ADJUSTMENT_BPS: Mapping[str, Mapping[str, int]] = _frozen({
    "transient": {
        "retry_immediate": 600,
        "retry_delayed": 1600,
        "payment_link": 200,
        "reminder": 100,
        "alternate_method_prompt": 300,
    },
    "customer_action_needed": {
        "retry_immediate": -1200,
        "retry_delayed": -800,
        "payment_link": 1400,
        "reminder": 900,
        "alternate_method_prompt": 1100,
    },
    "fraud_suspect": {
        "retry_immediate": -1500,
        "retry_delayed": -1500,
        "payment_link": -1500,
        "reminder": -1500,
        "alternate_method_prompt": -1500,
    },
    "terminal": {
        "retry_immediate": -1800,
        "retry_delayed": -1800,
        "payment_link": -1800,
        "reminder": -1800,
        "alternate_method_prompt": -1800,
    },
})

# Adjustment by the observed failure reason. ``failure_reason`` is a free
# string in the locked domain contract, so an unrecognized value contributes
# nothing rather than being guessed at. The keys below cover the values the
# repository's event generator produces.
FAILURE_REASON_ADJUSTMENT_BPS: Mapping[str, Mapping[str, int]] = _frozen({
    # The bank was unreachable: retrying straight away hits the same outage,
    # retrying later does not, and routing around the bank helps.
    "bank_timeout": {
        "retry_immediate": -800,
        "retry_delayed": 1200,
        "alternate_method_prompt": 600,
    },
    "network_issue": {
        "retry_immediate": -400,
        "retry_delayed": 1000,
        "alternate_method_prompt": 400,
    },
    # The customer needs money in the account, which takes time and a nudge.
    "insufficient_funds": {
        "retry_immediate": -1500,
        "retry_delayed": 200,
        "payment_link": 600,
        "reminder": 800,
        "alternate_method_prompt": 300,
    },
    # Authentication needs the customer present, so re-attempts help little.
    "authentication_failed": {
        "retry_immediate": -600,
        "retry_delayed": -200,
        "payment_link": 700,
        "alternate_method_prompt": 900,
    },
    # The instrument itself is unusable; only a different instrument helps.
    "expired_card": {
        "retry_immediate": -1600,
        "retry_delayed": -1400,
        "payment_link": 800,
        "alternate_method_prompt": 1200,
    },
    "declined_by_bank": {
        "retry_immediate": -700,
        "retry_delayed": -300,
        "payment_link": 400,
        "alternate_method_prompt": 800,
    },
    "transaction_declined": {
        "retry_immediate": -600,
        "retry_delayed": -200,
        "payment_link": 300,
        "alternate_method_prompt": 700,
    },
    # A generic provider message carries no actionable signal.
    "payment_failed": {},
})

# Adjustment by payment rail.
PAYMENT_METHOD_ADJUSTMENT_BPS: Mapping[str, Mapping[str, int]] = _frozen(
    {
        "upi": {
            "retry_immediate": 300,
            "retry_delayed": 300,
            "alternate_method_prompt": 300,
        },
        "card": {"payment_link": 200, "alternate_method_prompt": 200},
        "netbanking": {"retry_delayed": 200, "payment_link": 200},
        "wallet": {"alternate_method_prompt": 300, "reminder": 100},
    }
)

# Customer reliability bands, applied uniformly across interventions: a payer
# with a long successful history is more likely to complete any recovery path,
# and a payer with repeated recent failures is less likely to complete any.
RELIABLE_CUSTOMER_MIN_SUCCESSES = 10
ESTABLISHED_CUSTOMER_MIN_SUCCESSES = 3
UNRELIABLE_CUSTOMER_MIN_FAILURES = 5
STRUGGLING_CUSTOMER_MIN_FAILURES = 3

RELIABLE_CUSTOMER_BPS = 400
ESTABLISHED_CUSTOMER_BPS = 200
NEW_CUSTOMER_BPS = -200
UNRELIABLE_CUSTOMER_BPS = -600
STRUGGLING_CUSTOMER_BPS = -300

# An active subscription implies a stored mandate, so an automated re-attempt
# can succeed without the customer doing anything.
SUBSCRIPTION_ADJUSTMENT_BPS: Mapping[str, int] = _frozen(
    {
        "retry_immediate": 200,
        "retry_delayed": 400,
        "reminder": 100,
    }
)


class EstimationError(Exception):
    """The estimator received input it cannot score safely."""


def _saturate(basis_points: int) -> int:
    """Bound an additive score into the valid probability domain.

    Saturation is the estimator's DEFINED behaviour, not silent repair of a
    bad value: the score model is a sum of bounded coefficients, so an extreme
    feature combination legitimately pushes the total past an endpoint, and a
    probability of 0 or 1 is the correct reading of that. Distinct from
    ``RecoveryProbability``, which rejects out-of-domain values supplied from
    outside the model.
    """
    return max(0, min(PROBABILITY_SCALE, basis_points))


class RecoveryProbabilityEstimator:
    """Deterministic, interpretable estimator of recovery probability.

    Pure: ``estimate`` is a function of (event, classification, intervention)
    only. The same inputs always produce the same probability, and no state is
    carried between calls.
    """

    def estimate(
        self,
        event: PaymentEvent,
        classification: ClassificationResult,
        intervention: str,
    ) -> RecoveryProbability:
        """Estimate ``P(recovery | event, intervention)`` in basis points."""
        if not isinstance(event, PaymentEvent):
            raise EstimationError("event must be a PaymentEvent")
        if not isinstance(classification, ClassificationResult):
            raise EstimationError("classification must be a ClassificationResult")
        if event.event_id != classification.event_id:
            raise EstimationError(
                "event and classification event_id do not match"
            )
        if intervention not in EXECUTABLE_INTERVENTIONS:
            raise UnsupportedInterventionError(
                f"intervention must be one of "
                f"{sorted(EXECUTABLE_INTERVENTIONS)}, got {intervention!r}"
            )

        score = BASE_RECOVERY_BPS[intervention]
        score += self._lookup(
            ROOT_CAUSE_ADJUSTMENT_BPS,
            classification.root_cause_category,
            intervention,
        )
        score += self._lookup(
            FAILURE_REASON_ADJUSTMENT_BPS, event.failure_reason, intervention
        )
        score += self._lookup(
            PAYMENT_METHOD_ADJUSTMENT_BPS, event.payment_method, intervention
        )
        score += self._customer_history_adjustment(event, intervention)
        return RecoveryProbability(basis_points=_saturate(score))

    @staticmethod
    def _lookup(
        table: Mapping[str, Mapping[str, int]], feature: str, intervention: str
    ) -> int:
        """Return an adjustment, treating an unknown feature value as neutral.

        An unrecognized ``failure_reason`` is expected — the locked contract
        defines no finite taxonomy for it — so the estimator contributes zero
        rather than inventing a coefficient.
        """
        return table.get(feature, {}).get(intervention, 0)

    @staticmethod
    def _customer_history_adjustment(event: PaymentEvent, intervention: str) -> int:
        """Score the payer's track record and mandate status."""
        history = event.customer_history
        adjustment = 0

        if history.prior_successful_payments >= RELIABLE_CUSTOMER_MIN_SUCCESSES:
            adjustment += RELIABLE_CUSTOMER_BPS
        elif history.prior_successful_payments >= ESTABLISHED_CUSTOMER_MIN_SUCCESSES:
            adjustment += ESTABLISHED_CUSTOMER_BPS
        elif history.prior_successful_payments == 0:
            adjustment += NEW_CUSTOMER_BPS

        if history.prior_failed_payments >= UNRELIABLE_CUSTOMER_MIN_FAILURES:
            adjustment += UNRELIABLE_CUSTOMER_BPS
        elif history.prior_failed_payments >= STRUGGLING_CUSTOMER_MIN_FAILURES:
            adjustment += STRUGGLING_CUSTOMER_BPS

        if history.has_active_subscription:
            adjustment += SUBSCRIPTION_ADJUSTMENT_BPS.get(intervention, 0)

        return adjustment

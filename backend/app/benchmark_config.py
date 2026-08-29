"""Frozen Phase 17 benchmark configuration — one inspectable source of truth.

Every parameter that can change a Phase 17 benchmark number lives here, in one
serializable value, so that a published result can be re-derived exactly and so
that "which knobs existed?" is answerable by reading a single object rather
than by grepping for magic constants.

FREEZE DISCIPLINE
-----------------
The values below were fixed BEFORE any Phase 17 benchmark was executed and
before it was known whether V2 beats V1. Tuning any of them after observing a
result would invalidate the experiment, so the methodology identifier is
versioned: a run that changes a frozen parameter must publish under a NEW
methodology name rather than silently making old numbers incomparable.

METHODOLOGY VERSIONS
--------------------
``phase9_v1_compat``      the frozen Phase 9 world (``outcome_model.py``:
                          independent uniform probabilities per event). Kept
                          reproducible; see ``app/benchmark.py``.
``phase17_signal_bearing_v1``
                          the Phase 17 world (``hidden_world.py``:
                          feature-driven causal probabilities). These two
                          methodologies have DIFFERENT hidden worlds and their
                          numbers are not comparable to each other.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from . import estimator as estimator_module
from .config import (
    DEFAULT_POLICY_DAILY_SPEND_CAP_PAISE,
    DEFAULT_POLICY_EVENT_COOLDOWN_MINUTES,
    DEFAULT_POLICY_MAX_INTERVENTIONS_PER_CUSTOMER_24H,
)
from .economics import DEFAULT_ECONOMIC_MODEL, EconomicModel
from .generator import (
    EVENT_GENERATOR_METHODOLOGY_VERSION,
    event_generator_fingerprint,
)
from .hidden_world import (
    HIDDEN_WORLD_METHODOLOGY_VERSION,
    RANDOMIZATION_VERSION,
    hidden_world_fingerprint,
)
from .policy import PolicyConfig


def frozen_policy_config() -> PolicyConfig:
    """The benchmark's policy configuration, pinned to the shipped defaults.

    Deliberately NOT ``config.build_policy_config()``: that reads environment
    variables, and a benchmark whose safety configuration depends on the shell
    it was launched from is not reproducible. Every arm receives this identical
    configuration, so policy can never be a source of unfairness between arms.
    """
    return PolicyConfig(
        max_interventions_per_customer_24h=(
            DEFAULT_POLICY_MAX_INTERVENTIONS_PER_CUSTOMER_24H
        ),
        event_cooldown_minutes=DEFAULT_POLICY_EVENT_COOLDOWN_MINUTES,
        daily_spend_cap_paise=DEFAULT_POLICY_DAILY_SPEND_CAP_PAISE,
    )

METHODOLOGY_PHASE9_V1_COMPAT = "phase9_v1_compat"
METHODOLOGY_PHASE17 = "phase17_signal_bearing_v1"

# The canonical headline run. 500 events at seed 42 is retained from Phase 9 so
# that dataset size stays comparable across phases.
CANONICAL_EVENT_COUNT = 500
CANONICAL_EVENT_SEED = 42
CANONICAL_OUTCOME_SEED = 42

# Frozen evaluation instant. Policy arithmetic (cooldowns, 24h windows) must
# never read the wall clock, so the benchmark supplies the time explicitly.
CANONICAL_EVALUATION_TIME = datetime(2026, 8, 27, 13, 0, tzinfo=timezone.utc)

EVALUATION_MODE_SIMULATED = "SIMULATED"

# Named, frozen robustness seeds. Declared here up front — not chosen after
# looking at results — so that "we ran until V2 won" is not possible: the set
# is fixed and every member is reported whatever it says. Seed 42 remains the
# single canonical headline.
ROBUSTNESS_SEEDS: tuple[int, ...] = (42, 7, 1337, 2024, 31415)

# The false-intervention rule, frozen before any result was observed.
#
#   An attempted intervention is a FALSE INTERVENTION when its true expected
#   value is strictly less than the true expected value of doing nothing on
#   that same event.
#
# Unit:        paise (integer), compared per event.
# Threshold:   the event's own true_EV(no_action) — not a flat constant.
# Rationale:   "false" should mean "the world says this action destroyed value
#              relative to the available alternative of not acting". A flat
#              paise threshold would be arbitrary and would scale wrongly
#              across a ₹50 and a ₹20,000 failure; the no-action baseline is
#              derived from the methodology's own control arm and is therefore
#              frozen by the same act that froze the hidden world.
# Denominator: interventions_attempted by that strategy.
# Edge cases:  zero attempts -> the rate is None, never 0.0; an attempt whose
#              true EV exactly equals the baseline is NOT false (strict <),
#              because breaking even is not a mistake.
FALSE_INTERVENTION_RULE = "true_ev_below_event_no_action_true_ev"


def _estimator_fingerprint() -> str:
    """A stable digest of the V2 estimator's frozen coefficient tables.

    Recorded on every run so a published number can be tied to the exact
    RecoveryOS belief model that produced it. Reading the tables is not a
    ground-truth dependency: these are the system's own public assumptions.
    """
    payload = {
        "base": dict(estimator_module.BASE_RECOVERY_BPS),
        "root_cause": {
            key: dict(value)
            for key, value in estimator_module.ROOT_CAUSE_ADJUSTMENT_BPS.items()
        },
        "failure_reason": {
            key: dict(value)
            for key, value in estimator_module.FAILURE_REASON_ADJUSTMENT_BPS.items()
        },
        "payment_method": {
            key: dict(value)
            for key, value in estimator_module.PAYMENT_METHOD_ADJUSTMENT_BPS.items()
        },
        "subscription": dict(estimator_module.SUBSCRIPTION_ADJUSTMENT_BPS),
        "bands": [
            estimator_module.RELIABLE_CUSTOMER_MIN_SUCCESSES,
            estimator_module.ESTABLISHED_CUSTOMER_MIN_SUCCESSES,
            estimator_module.UNRELIABLE_CUSTOMER_MIN_FAILURES,
            estimator_module.STRUGGLING_CUSTOMER_MIN_FAILURES,
            estimator_module.RELIABLE_CUSTOMER_BPS,
            estimator_module.ESTABLISHED_CUSTOMER_BPS,
            estimator_module.NEW_CUSTOMER_BPS,
            estimator_module.UNRELIABLE_CUSTOMER_BPS,
            estimator_module.STRUGGLING_CUSTOMER_BPS,
        ],
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.blake2b(encoded.encode("utf-8"), digest_size=16).hexdigest()


class BenchmarkConfigurationError(Exception):
    """The benchmark configuration is malformed; nothing is evaluated."""


@dataclass(frozen=True)
class Phase17BenchmarkConfig:
    """Every frozen parameter of one Phase 17 benchmark run.

    ``hidden_model_seed`` is recorded but deliberately UNUSED: the Phase 17
    hidden world is a pure function of event features and needs no randomness
    to define its probabilities (only the Bernoulli realization is seeded, via
    ``outcome_seed``). Keeping the field makes that fact auditable — a report
    can show that no seed influenced ground-truth probabilities at all.
    """

    methodology: str = METHODOLOGY_PHASE17
    event_count: int = CANONICAL_EVENT_COUNT
    event_seed: int = CANONICAL_EVENT_SEED
    outcome_seed: int = CANONICAL_OUTCOME_SEED
    hidden_model_seed: int | None = None
    replication: int = 0
    evaluation_time: datetime = CANONICAL_EVALUATION_TIME
    evaluation_mode: str = EVALUATION_MODE_SIMULATED
    randomization_version: str = RANDOMIZATION_VERSION
    false_intervention_rule: str = FALSE_INTERVENTION_RULE
    policy_config: PolicyConfig = field(default_factory=frozen_policy_config)
    economic_model: EconomicModel = DEFAULT_ECONOMIC_MODEL

    def __post_init__(self) -> None:
        if not isinstance(self.methodology, str) or not self.methodology.strip():
            raise BenchmarkConfigurationError("methodology must be a non-empty string")
        for name in ("event_count", "event_seed", "outcome_seed", "replication"):
            value = getattr(self, name)
            if type(value) is not int:
                raise BenchmarkConfigurationError(f"{name} must be an integer")
        if self.event_count < 1:
            raise BenchmarkConfigurationError("event_count must be at least 1")
        if self.replication < 0:
            raise BenchmarkConfigurationError("replication must be non-negative")
        if self.hidden_model_seed is not None and type(self.hidden_model_seed) is not int:
            raise BenchmarkConfigurationError(
                "hidden_model_seed must be an integer or None"
            )
        if not isinstance(self.evaluation_time, datetime):
            raise BenchmarkConfigurationError("evaluation_time must be a datetime")
        if self.evaluation_time.tzinfo is None:
            raise BenchmarkConfigurationError("evaluation_time must be timezone-aware")
        if self.evaluation_mode != EVALUATION_MODE_SIMULATED:
            raise BenchmarkConfigurationError(
                "Phase 17 benchmark results are simulated; evaluation_mode must be "
                f"{EVALUATION_MODE_SIMULATED!r}"
            )
        if not isinstance(self.policy_config, PolicyConfig):
            raise BenchmarkConfigurationError("policy_config must be a PolicyConfig")
        if not isinstance(self.economic_model, EconomicModel):
            raise BenchmarkConfigurationError("economic_model must be an EconomicModel")

    @property
    def evaluated_at(self) -> str:
        """The frozen evaluation instant as a UTC ISO8601 string."""
        return self.evaluation_time.astimezone(timezone.utc).isoformat()

    def to_dict(self) -> dict[str, Any]:
        """Serialize every frozen parameter for reproduction and audit."""
        return {
            "methodology": self.methodology,
            "event_count": self.event_count,
            "event_seed": self.event_seed,
            "outcome_seed": self.outcome_seed,
            "hidden_model_seed": self.hidden_model_seed,
            "replication": self.replication,
            "evaluation_time": self.evaluated_at,
            "evaluation_mode": self.evaluation_mode,
            "randomization_version": self.randomization_version,
            "false_intervention_rule": self.false_intervention_rule,
            "estimator_fingerprint": _estimator_fingerprint(),
            "hidden_world_methodology_version": HIDDEN_WORLD_METHODOLOGY_VERSION,
            "hidden_world_fingerprint": hidden_world_fingerprint(),
            "event_generator_methodology_version": (
                EVENT_GENERATOR_METHODOLOGY_VERSION
            ),
            "event_generator_fingerprint": event_generator_fingerprint(),
            "policy_config": {
                "max_interventions_per_customer_24h": (
                    self.policy_config.max_interventions_per_customer_24h
                ),
                "event_cooldown_minutes": self.policy_config.event_cooldown_minutes,
                "daily_spend_cap_paise": self.policy_config.daily_spend_cap_paise,
                "intervention_cost_paise": dict(
                    sorted(self.policy_config.intervention_cost_paise.items())
                ),
            },
            "economic_model": {
                intervention: {
                    "cost_paise": economics.cost_paise,
                    "friction_bps": economics.friction_bps,
                }
                for intervention, economics in sorted(
                    self.economic_model.assumptions.items()
                )
            },
        }

    def fingerprint(self) -> str:
        """A stable digest of the whole configuration.

        Two runs with the same fingerprint MUST produce identical results; a
        changed fingerprint is the signal that a published number is no longer
        comparable.
        """
        encoded = json.dumps(self.to_dict(), sort_keys=True, separators=(",", ":"))
        return hashlib.blake2b(encoded.encode("utf-8"), digest_size=16).hexdigest()

    def run_id(self) -> str:
        """The canonical, deterministic identifier of this run."""
        return (
            f"recoveryos-benchmark:{self.methodology}:"
            f"events={self.event_count}:seed={self.event_seed}:"
            f"outcome_seed={self.outcome_seed}:rep={self.replication}:"
            f"config={self.fingerprint()}"
        )

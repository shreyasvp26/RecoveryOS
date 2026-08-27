"""Deterministic financial safety policy — contracts.

Phase 6: the policy engine decides whether a proposed intervention is
permitted. It never selects the best intervention and never executes anything.
The LLM (Phase 5) recommends; this deterministic Python gate authorizes;
a future executor acts. All policy evaluation is a pure function of its
inputs; there are no LLM calls, no benchmark dependencies, and no execution
here — by construction.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Mapping

from .classification import (
    CANDIDATE_INTERVENTIONS,
    ROOT_CAUSE_CATEGORIES,
    ClassificationResult,
)
from .models import PaymentEvent

# Locked value sets (Phase 5, reused — never expanded here).
INTERVENTION_ATTEMPT_STATUSES: frozenset[str] = frozenset(
    {"attempted", "failed", "successful"}
)

# Blocking denial reasons, in deterministic evaluation order.
RULE_INVALID_INTERVENTION = "invalid_intervention"
RULE_FRAUD = "fraud_protection"
RULE_TERMINAL = "terminal_failure"
RULE_DUPLICATE = "duplicate_intervention"
RULE_CUSTOMER_LIMIT = "customer_intervention_limit_exceeded"
RULE_COOLDOWN = "event_cooldown_active"
RULE_SPEND_CAP = "spend_cap_exceeded"

# Passed-check names, matching the documented decision contract.
CHECK_FRAUD = "fraud_check_passed"
CHECK_TERMINAL = "terminal_check_passed"
CHECK_DUPLICATE = "duplicate_check_passed"
CHECK_RETRY_LIMIT = "retry_limit_passed"
CHECK_COOLDOWN = "cooldown_check_passed"
CHECK_SPEND_CAP = "spend_cap_passed"

# Deterministic, documented evaluation order. The first blocker determines
# the denial reason; the same inputs always produce the same decision.
DETERMINISTIC_RULE_ORDER: tuple[str, ...] = (
    RULE_INVALID_INTERVENTION,
    RULE_FRAUD,
    RULE_TERMINAL,
    RULE_DUPLICATE,
    RULE_CUSTOMER_LIMIT,
    RULE_COOLDOWN,
    RULE_SPEND_CAP,
)

POLICY_DECISION_KEYS: frozenset[str] = frozenset(
    {
        "event_id",
        "proposed_intervention",
        "allowed",
        "denial_reason",
        "policy_rules_applied",
        "evaluated_at",
    }
)

INTERVENTION_ATTEMPT_KEYS: frozenset[str] = frozenset(
    {
        "event_id",
        "intervention",
        "customer_id",
        "cost_paise",
        "attempted_at",
        "status",
    }
)


class PolicyError(Exception):
    """Base class for all explicit policy failures."""


class PolicyValidationError(PolicyError):
    """Policy inputs were malformed or could not be evaluated safely.

    This is the fail-closed signal: when required safety information cannot
    be determined safely, evaluation stops with a controlled error rather
    than guessing or allowing.
    """


def parse_aware_datetime(value: Any) -> datetime:
    """Parse an ISO8601 timestamp and require explicit timezone awareness.

    Mixing naive/aware timestamps is forbidden for policy arithmetic; a
    naive timestamp is treated as determined-safely-impossible.
    """
    if not isinstance(value, str) or not value.strip():
        raise PolicyValidationError("expected an ISO8601 timestamp string")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise PolicyValidationError(f"invalid timestamp {value!r}") from exc
    if parsed.tzinfo is None:
        raise PolicyValidationError(
            f"timestamp {value!r} is naive; policy requires timezone-aware timestamps"
        )
    return parsed.astimezone(timezone.utc)


@dataclass(frozen=True)
class PolicyConfig:
    """Deterministic safety-gate configuration.

    Values are resolved once from environment configuration (see config.py)
    and passed in explicitly; nothing is scattered across rule functions.
    """

    max_interventions_per_customer_24h: int = 2
    event_cooldown_minutes: int = 30
    daily_spend_cap_paise: int = 5_000_000
    intervention_cost_paise: Mapping[str, int] = field(
        default_factory=lambda: {
            intervention: 0 for intervention in CANDIDATE_INTERVENTIONS
        }
    )

    def __post_init__(self) -> None:
        if (
            not isinstance(self.max_interventions_per_customer_24h, int)
            or self.max_interventions_per_customer_24h < 1
        ):
            raise PolicyValidationError(
                "max_interventions_per_customer_24h must be a positive integer"
            )
        if (
            not isinstance(self.event_cooldown_minutes, int)
            or self.event_cooldown_minutes < 1
        ):
            raise PolicyValidationError(
                "event_cooldown_minutes must be a positive integer"
            )
        if (
            not isinstance(self.daily_spend_cap_paise, int)
            or self.daily_spend_cap_paise < 0
        ):
            raise PolicyValidationError(
                "daily_spend_cap_paise must be a non-negative integer"
            )
        if not isinstance(self.intervention_cost_paise, Mapping):
            raise PolicyValidationError(
                "intervention_cost_paise must be a mapping"
            )
        for intervention, cost in self.intervention_cost_paise.items():
            if not isinstance(cost, int) or cost < 0:
                raise PolicyValidationError(
                    f"cost for {intervention!r} must be a non-negative integer"
                )

    def intervention_cost(self, intervention: str) -> int:
        """Return the configured cost for an intervention (0 when unset)."""
        return self.intervention_cost_paise.get(intervention, 0)


@dataclass(frozen=True)
class PolicyHistory:
    """Historical context required by the rules, derived from persisted state.

    Computed by the persistence boundary (db.get_policy_history); never
    constructed from shadow in-memory state or the LLM.
    """

    customer_intervention_count_24h: int
    most_recent_event_intervention_time: datetime | None
    has_successful_intervention: bool
    existing_daily_spend_paise: int

    def __post_init__(self) -> None:
        for name in (
            "customer_intervention_count_24h",
            "existing_daily_spend_paise",
        ):
            value = getattr(self, name)
            if not isinstance(value, int) or isinstance(value, bool) or value < 0:
                raise PolicyValidationError(f"{name} must be a non-negative integer")
        if not isinstance(self.has_successful_intervention, bool):
            raise PolicyValidationError(
                "has_successful_intervention must be a boolean"
            )
        if (
            self.most_recent_event_intervention_time is not None
            and not isinstance(self.most_recent_event_intervention_time, datetime)
        ):
            raise PolicyValidationError(
                "most_recent_event_intervention_time must be a datetime or None"
            )


@dataclass(frozen=True)
class PolicyInput:
    """Everything the policy engine needs to evaluate one proposed intervention.

    Includes the persisted event, its advisory classification, the historical
    policy context, the proposed intervention, and the explicit evaluation
    timestamp. Evaluation never depends on wall-clock time except through this
    supplied timestamp.
    """

    event: PaymentEvent
    classification: ClassificationResult
    proposed_intervention: str
    history: PolicyHistory
    evaluation_time: datetime

    def __post_init__(self) -> None:
        if not isinstance(self.event, PaymentEvent):
            raise PolicyValidationError("event must be a PaymentEvent")
        if not isinstance(self.classification, ClassificationResult):
            raise PolicyValidationError(
                "classification must be a ClassificationResult"
            )
        if not isinstance(self.history, PolicyHistory):
            raise PolicyValidationError("history must be a PolicyHistory")
        if not isinstance(self.evaluation_time, datetime):
            raise PolicyValidationError("evaluation_time must be a datetime")
        if self.evaluation_time.tzinfo is None:
            raise PolicyValidationError(
                "evaluation_time must be timezone-aware"
            )
        if self.event.event_id != self.classification.event_id:
            raise PolicyValidationError(
                "event and classification event_id do not match"
            )


@dataclass(frozen=True)
class PolicyDecision:
    """The authoritative output of the policy gate: ALLOW or DENY.

    Every decision explains whether the intervention is allowed, why, which
    rule blocked it (when denied), and which checks were applied. A denial
    without an explicit reason is invalid.
    """

    event_id: str
    proposed_intervention: str
    allowed: bool
    denial_reason: str | None
    policy_rules_applied: tuple[str, ...]
    evaluated_at: str

    def __post_init__(self) -> None:
        if not isinstance(self.event_id, str) or not self.event_id.strip():
            raise PolicyValidationError("event_id must be a non-empty string")
        if self.proposed_intervention not in CANDIDATE_INTERVENTIONS:
            raise PolicyValidationError(
                f"proposed_intervention must be one of "
                f"{sorted(CANDIDATE_INTERVENTIONS)}, got "
                f"{self.proposed_intervention!r}"
            )
        if not isinstance(self.allowed, bool):
            raise PolicyValidationError("allowed must be a boolean")
        if self.allowed:
            if self.denial_reason is not None:
                raise PolicyValidationError(
                    "an allowed decision must not carry a denial_reason"
                )
        else:
            if not isinstance(self.denial_reason, str) or not self.denial_reason.strip():
                raise PolicyValidationError(
                    "a denied decision requires an explicit denial_reason"
                )
        if (
            not isinstance(self.policy_rules_applied, (list, tuple))
            or not self.policy_rules_applied
        ):
            raise PolicyValidationError(
                "policy_rules_applied must be a non-empty sequence"
            )
        object.__setattr__(
            self,
            "policy_rules_applied",
            tuple(
                rule
                for rule in self.policy_rules_applied
                if isinstance(rule, str) and rule.strip()
            ),
        )
        if not self.policy_rules_applied:
            raise PolicyValidationError(
                "policy_rules_applied must contain at least one rule name"
            )
        parse_aware_datetime(self.evaluated_at)

    def to_dict(self) -> dict[str, Any]:
        """Serialize to a plain dict, preserving the decision contract."""
        return {
            "event_id": self.event_id,
            "proposed_intervention": self.proposed_intervention,
            "allowed": self.allowed,
            "denial_reason": self.denial_reason,
            "policy_rules_applied": list(self.policy_rules_applied),
            "evaluated_at": self.evaluated_at,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "PolicyDecision":
        """Reconstruct a PolicyDecision from a plain dict."""
        if not isinstance(data, dict):
            raise PolicyValidationError("policy decision data must be an object")
        if any(key not in POLICY_DECISION_KEYS for key in data):
            raise PolicyValidationError("policy decision data contains unexpected fields")
        if any(key not in data for key in POLICY_DECISION_KEYS):
            raise PolicyValidationError("policy decision data is missing required fields")
        return cls(
            event_id=data["event_id"],
            proposed_intervention=data["proposed_intervention"],
            allowed=data["allowed"],
            denial_reason=data["denial_reason"],
            policy_rules_applied=data["policy_rules_applied"],
            evaluated_at=data["evaluated_at"],
        )


@dataclass(frozen=True)
class InterventionAttempt:
    """A minimal persisted intervention-history record.

    The future executor records attempted/failed/successful executions here.
    Phase 6 never writes success (no execution exists); the source of truth
    for the customer limit, cooldown, duplicate, and spend rules is the
    persisted intervention history, never the LLM and never in-memory state.
    """

    event_id: str
    intervention: str
    customer_id: str
    cost_paise: int
    attempted_at: str
    status: str

    def __post_init__(self) -> None:
        for name in ("event_id", "intervention", "customer_id", "attempted_at", "status"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip():
                raise PolicyValidationError(f"{name} must be a non-empty string")
        if self.intervention not in CANDIDATE_INTERVENTIONS:
            raise PolicyValidationError(
                f"intervention must be one of {sorted(CANDIDATE_INTERVENTIONS)}, "
                f"got {self.intervention!r}"
            )
        if self.status not in INTERVENTION_ATTEMPT_STATUSES:
            raise PolicyValidationError(
                f"status must be one of {sorted(INTERVENTION_ATTEMPT_STATUSES)}, "
                f"got {self.status!r}"
            )
        if not isinstance(self.cost_paise, int) or isinstance(self.cost_paise, bool):
            raise PolicyValidationError("cost_paise must be an integer")
        if self.cost_paise < 0:
            raise PolicyValidationError("cost_paise must be non-negative")
        parse_aware_datetime(self.attempted_at)

    def to_dict(self) -> dict[str, Any]:
        """Serialize to a plain dict, preserving the stored contract."""
        return {
            "event_id": self.event_id,
            "intervention": self.intervention,
            "customer_id": self.customer_id,
            "cost_paise": self.cost_paise,
            "attempted_at": self.attempted_at,
            "status": self.status,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "InterventionAttempt":
        """Reconstruct an InterventionAttempt from a plain dict."""
        if not isinstance(data, dict):
            raise PolicyValidationError("intervention attempt data must be an object")
        if any(key not in INTERVENTION_ATTEMPT_KEYS for key in data):
            raise PolicyValidationError(
                "intervention attempt data contains unexpected fields"
            )
        if any(key not in data for key in INTERVENTION_ATTEMPT_KEYS):
            raise PolicyValidationError(
                "intervention attempt data is missing required fields"
            )
        return cls(
            event_id=data["event_id"],
            intervention=data["intervention"],
            customer_id=data["customer_id"],
            cost_paise=data["cost_paise"],
            attempted_at=data["attempted_at"],
            status=data["status"],
        )


def _validate_input(input: PolicyInput) -> None:
    """Fail-closed validation of the evaluation inputs.

    Any input that cannot be evaluated safely raises PolicyValidationError;
    policy never guesses and never substitutes fabricated context.
    """
    if not isinstance(input.proposed_intervention, str):
        raise PolicyValidationError("proposed_intervention must be a string")
    if input.event.risk_flag not in ("normal", "fraud_suspect"):
        raise PolicyValidationError(
            f"unexpected risk_flag {input.event.risk_flag!r}"
        )
    if input.classification.root_cause_category not in ROOT_CAUSE_CATEGORIES:
        raise PolicyValidationError(
            f"unexpected root_cause_category "
            f"{input.classification.root_cause_category!r}"
        )
    if input.evaluation_time.tzinfo is None:
        raise PolicyValidationError("evaluation_time must be timezone-aware")


class PolicyEngine:
    """The deterministic financial safety gate.

    Pure and stateless: evaluate(input, config) -> PolicyDecision is a
    function of its inputs only. The same inputs always produce the same
    decision. The first blocker in DETERMINISTIC_RULE_ORDER determines the
    denial reason.
    """

    def evaluate(self, input: PolicyInput, config: PolicyConfig) -> PolicyDecision:
        """Evaluate one proposed intervention against the safety rules."""
        _validate_input(input)
        event = input.event
        classification = input.classification
        history = input.history
        evaluated_at = input.evaluation_time.astimezone(timezone.utc).isoformat()

        def denied(reason: str) -> PolicyDecision:
            return PolicyDecision(
                event_id=event.event_id,
                proposed_intervention=input.proposed_intervention,
                allowed=False,
                denial_reason=reason,
                policy_rules_applied=(reason,),
                evaluated_at=evaluated_at,
            )

        if input.proposed_intervention not in CANDIDATE_INTERVENTIONS:
            return denied(RULE_INVALID_INTERVENTION)

        passed: list[str] = []

        if event.risk_flag == "fraud_suspect":
            return denied(RULE_FRAUD)
        passed.append(CHECK_FRAUD)

        if classification.root_cause_category == "terminal":
            return denied(RULE_TERMINAL)
        passed.append(CHECK_TERMINAL)

        if history.has_successful_intervention:
            return denied(RULE_DUPLICATE)
        passed.append(CHECK_DUPLICATE)

        if (
            history.customer_intervention_count_24h
            >= config.max_interventions_per_customer_24h
        ):
            return denied(RULE_CUSTOMER_LIMIT)
        passed.append(CHECK_RETRY_LIMIT)

        most_recent = history.most_recent_event_intervention_time
        if most_recent is not None:
            elapsed_minutes = (
                input.evaluation_time - most_recent
            ).total_seconds() / 60.0
            if elapsed_minutes < config.event_cooldown_minutes:
                return denied(RULE_COOLDOWN)
        passed.append(CHECK_COOLDOWN)

        proposed_cost = config.intervention_cost(input.proposed_intervention)
        if history.existing_daily_spend_paise + proposed_cost > config.daily_spend_cap_paise:
            return denied(RULE_SPEND_CAP)
        passed.append(CHECK_SPEND_CAP)

        return PolicyDecision(
            event_id=event.event_id,
            proposed_intervention=input.proposed_intervention,
            allowed=True,
            denial_reason=None,
            policy_rules_applied=tuple(passed),
            evaluated_at=evaluated_at,
        )

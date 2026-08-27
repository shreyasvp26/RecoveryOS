"""Structured AI classification contract.

Phase 5: defines the advisory classification produced by the AI classifier.
A classification diagnoses the likely root cause of a failed payment and
recommends candidate interventions only. It never authorizes, selects, or
executes an action. No policy, executor, or benchmark logic lives here.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

# Locked finite value sets (Phase 5).
ROOT_CAUSE_CATEGORIES: frozenset[str] = frozenset(
    {"transient", "customer_action_needed", "fraud_suspect", "terminal"}
)

CANDIDATE_INTERVENTIONS: frozenset[str] = frozenset(
    {
        "retry_immediate",
        "retry_delayed",
        "payment_link",
        "reminder",
        "alternate_method_prompt",
        "no_action",
    }
)

CLASSIFICATION_KEYS: frozenset[str] = frozenset(
    {
        "event_id",
        "root_cause_category",
        "confidence",
        "reasoning",
        "candidate_interventions",
    }
)


@dataclass(frozen=True)
class ClassificationResult:
    """The structured output of an AI classification (advisory only).

    WARNING: Do not add business or execution fields. recovery_probability,
    expected_revenue, best_intervention, and policy/execution fields belong to
    later phases and must never be introduced here.
    """

    event_id: str
    root_cause_category: str
    confidence: float
    reasoning: str
    candidate_interventions: tuple[str, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.event_id, str) or not self.event_id.strip():
            raise ValueError("event_id must be a non-empty string")

        if self.root_cause_category not in ROOT_CAUSE_CATEGORIES:
            raise ValueError(
                f"root_cause_category must be one of {sorted(ROOT_CAUSE_CATEGORIES)}, "
                f"got {self.root_cause_category!r}"
            )

        if isinstance(self.confidence, bool) or not isinstance(
            self.confidence, (int, float)
        ):
            raise ValueError("confidence must be numeric")
        if not (0.0 <= self.confidence <= 1.0):
            raise ValueError("confidence must be between 0 and 1 (inclusive)")

        if not isinstance(self.reasoning, str) or not self.reasoning.strip():
            raise ValueError("reasoning must be a non-empty string")

        if not isinstance(self.candidate_interventions, (list, tuple)):
            raise ValueError("candidate_interventions must be a list")
        object.__setattr__(
            self, "candidate_interventions", tuple(self.candidate_interventions)
        )
        for intervention in self.candidate_interventions:
            if intervention not in CANDIDATE_INTERVENTIONS:
                raise ValueError(
                    f"candidate intervention {intervention!r} is not one of "
                    f"{sorted(CANDIDATE_INTERVENTIONS)}"
                )

    def to_dict(self) -> dict[str, Any]:
        """Serialize to a plain dict, preserving the locked contract exactly."""
        return {
            "event_id": self.event_id,
            "root_cause_category": self.root_cause_category,
            "confidence": self.confidence,
            "reasoning": self.reasoning,
            "candidate_interventions": list(self.candidate_interventions),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ClassificationResult":
        """Reconstruct a ClassificationResult from a plain dict."""
        if not isinstance(data, dict):
            raise ValueError("classification data must be an object")
        if any(key not in CLASSIFICATION_KEYS for key in data):
            raise ValueError("classification data contains unexpected fields")
        if any(key not in data for key in CLASSIFICATION_KEYS):
            raise ValueError("classification data is missing required fields")
        return cls(
            event_id=data["event_id"],
            root_cause_category=data["root_cause_category"],
            confidence=data["confidence"],
            reasoning=data["reasoning"],
            candidate_interventions=data["candidate_interventions"],
        )

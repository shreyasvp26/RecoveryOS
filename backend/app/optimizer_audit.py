"""Append-only audit contract for the V2 economic optimizer's decision.

Phase 18. The V2 economic decision already existed and is frozen; what was
missing was a durable record of it. This module owns the narrow contract that
turns one in-memory ``OptimizerDecision`` into a persistable, reconstructable
audit record, and back again.

WHAT THIS MODULE IS NOT
-----------------------
It is not a second economic model. It performs NO arithmetic: every monetary
and probability figure it carries is copied verbatim from the decision the
optimizer already produced, and reading a record back parses the persisted
numbers rather than re-deriving them. ``economics.py`` remains the single
source of truth for the expected-value equation.

It is also not an authorization or execution layer: a record is evidence of a
decision, and holding one confers no ability to run anything.

GROUND-TRUTH ISOLATION
----------------------
A record carries only information legitimately available to RecoveryOS at
decision time: the candidate sets, the estimated per-candidate economics, the
selection, and the reason. Benchmark hidden probabilities, simulated outcome
draws, oracle options, and realized benchmark values are evaluation-layer
state and can never appear here.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Sequence

from .economics import CandidateEvaluation
from .optimizer import OptimizerDecision

# The exact evaluation fields persisted, in the order the equation reads:
# amount x probability - intervention cost - friction = expected value.
_EVALUATION_FIELDS: tuple[str, ...] = (
    "intervention",
    "estimated_probability_bps",
    "amount_paise",
    "expected_recovered_value_paise",
    "intervention_cost_paise",
    "friction_cost_paise",
    "expected_value_paise",
)


class OptimizerAuditError(Exception):
    """A decision record is malformed and must not be persisted or trusted.

    Raised instead of repairing the record. An audit trail that silently
    corrects itself is not an audit trail.
    """


def _require_identifier(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value:
        raise OptimizerAuditError(f"{name} must be a non-empty string, got {value!r}")
    return value


def _evaluation_from_dict(data: Mapping[str, Any]) -> CandidateEvaluation:
    """Rebuild one evaluation from persisted fields, recomputing nothing."""
    if not isinstance(data, Mapping):
        raise OptimizerAuditError("each evaluation must be a mapping")
    missing = [field for field in _EVALUATION_FIELDS if field not in data]
    if missing:
        raise OptimizerAuditError(f"evaluation is missing fields {missing}")
    return CandidateEvaluation(**{field: data[field] for field in _EVALUATION_FIELDS})


@dataclass(frozen=True)
class OptimizerDecisionRecord:
    """One event's economic decision, exactly as the optimizer produced it.

    ``decided_at`` is supplied by the caller (the execution service's
    authoritative evaluation time), because the optimizer itself is pure and
    never reads a clock. Together with ``event_id`` it identifies the record.
    """

    event_id: str
    decided_at: str
    selected_intervention: str
    selection_reason: str
    candidates_considered: tuple[str, ...]
    allowed_candidates: tuple[str, ...]
    evaluations: tuple[CandidateEvaluation, ...]

    def __post_init__(self) -> None:
        _require_identifier(self.event_id, "event_id")
        _require_identifier(self.decided_at, "decided_at")
        _require_identifier(self.selected_intervention, "selected_intervention")
        _require_identifier(self.selection_reason, "selection_reason")
        for name in ("candidates_considered", "allowed_candidates"):
            value = getattr(self, name)
            if not isinstance(value, (list, tuple)):
                raise OptimizerAuditError(f"{name} must be a sequence")
            for candidate in value:
                _require_identifier(candidate, f"{name} entry")
            object.__setattr__(self, name, tuple(value))
        if not isinstance(self.evaluations, (list, tuple)):
            raise OptimizerAuditError("evaluations must be a sequence")
        for evaluation in self.evaluations:
            if not isinstance(evaluation, CandidateEvaluation):
                raise OptimizerAuditError(
                    "each evaluation must be a CandidateEvaluation"
                )
        object.__setattr__(self, "evaluations", tuple(self.evaluations))

        # The audit invariant that makes the record defensible: an evaluated
        # candidate is a policy-authorized candidate. A record that claims
        # otherwise describes a decision the architecture forbids.
        allowed = set(self.allowed_candidates)
        for evaluation in self.evaluations:
            if evaluation.intervention not in allowed:
                raise OptimizerAuditError(
                    f"evaluated candidate {evaluation.intervention!r} is not in "
                    "the policy-allowed set"
                )
        for candidate in self.allowed_candidates:
            if candidate not in self.candidates_considered:
                raise OptimizerAuditError(
                    f"allowed candidate {candidate!r} was never considered"
                )

    @classmethod
    def from_decision(
        cls, event_id: str, decided_at: str, decision: OptimizerDecision
    ) -> "OptimizerDecisionRecord":
        """Wrap the optimizer's own output; nothing is recalculated."""
        if not isinstance(decision, OptimizerDecision):
            raise OptimizerAuditError(
                "decision must be an OptimizerDecision produced by the optimizer"
            )
        return cls(
            event_id=event_id,
            decided_at=decided_at,
            selected_intervention=decision.selected_intervention,
            selection_reason=decision.selection_reason,
            candidates_considered=decision.candidates_considered,
            allowed_candidates=decision.allowed_candidates,
            evaluations=decision.evaluations,
        )

    def to_dict(self) -> dict[str, Any]:
        """Serialize the record for persistence and trace output."""
        return {
            "event_id": self.event_id,
            "decided_at": self.decided_at,
            "selected_intervention": self.selected_intervention,
            "selection_reason": self.selection_reason,
            "candidates_considered": list(self.candidates_considered),
            "allowed_candidates": list(self.allowed_candidates),
            "evaluations": [
                evaluation.to_dict() for evaluation in self.evaluations
            ],
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "OptimizerDecisionRecord":
        """Reconstruct a record from its persisted form."""
        if not isinstance(data, Mapping):
            raise OptimizerAuditError("record data must be a mapping")
        evaluations = data.get("evaluations", ())
        if not isinstance(evaluations, Sequence) or isinstance(evaluations, str):
            raise OptimizerAuditError("evaluations must be a sequence")
        try:
            return cls(
                event_id=data["event_id"],
                decided_at=data["decided_at"],
                selected_intervention=data["selected_intervention"],
                selection_reason=data["selection_reason"],
                candidates_considered=tuple(data["candidates_considered"]),
                allowed_candidates=tuple(data["allowed_candidates"]),
                evaluations=tuple(
                    _evaluation_from_dict(item) for item in evaluations
                ),
            )
        except KeyError as exc:
            raise OptimizerAuditError(f"record is missing field {exc}") from exc

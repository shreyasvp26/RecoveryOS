"""Deterministic economic intervention optimizer — V2 decision engine (Phase 16).

Replaces V1's fixed-priority selection with expected-value selection over the
candidates that the deterministic policy gate has ALREADY authorized:

    candidates -> policy gate -> allowed candidates -> optimizer -> selection

The optimizer is a DECISION layer, never an AUTHORIZATION layer. It cannot
authorize anything, cannot re-examine a denial, and cannot execute anything.

THE CORE SAFETY INVARIANT
-------------------------
    optimizer_decision_set  subset-of  policy_allowed_candidates

This is enforced structurally rather than by convention: the optimizer accepts
only an ``AllowedCandidates`` value, and ``AllowedCandidates`` can only be
built from a mapping of authoritative ``PolicyDecision`` objects, which it
filters itself. There is no code path that lets a caller hand the optimizer a
bare list of interventions, so a policy-denied candidate cannot be smuggled in
and cannot be resurrected regardless of how large its expected value is.

The optimizer knows nothing about WHY anything was denied. Fraud, terminal
failure, duplicate protection, cooldown, retry limits, customer limits, and
spend caps all live in ``policy.py`` and are never re-implemented, re-checked,
or overridden here.

SELECTION RULE
--------------
1. PRIMARY:   highest ``expected_value_paise``.
2. SECONDARY: V1's fixed-priority ordering, imported from ``selector.py`` so
              the ordering has exactly one authoritative definition.
3. FINAL:     alphabetical intervention name, so the result is total and
              stable even if the priority table ever grew a duplicate.

The sort key depends only on the candidate SET, never on input list order, so
any permutation of the same candidates yields the identical decision.

ISOLATION
---------
No LLM call, no network, no randomness, no wall-clock time, no persistence, no
executor call, and no import of the benchmark or hidden outcome model.
"""

from __future__ import annotations

from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Mapping

from .classification import CANDIDATE_INTERVENTIONS, ClassificationResult
from .economics import (
    CandidateEvaluation,
    EconomicModel,
    RecoveryProbability,
    evaluate_candidate,
)
from .estimator import RecoveryProbabilityEstimator
from .models import PaymentEvent
from .policy import PolicyDecision
from .selector import INTERVENTION_PRIORITY, NO_ACTION

# Why a particular result was produced. Every outcome is explicit; the
# optimizer never returns a selection it cannot account for.
REASON_MAX_EXPECTED_VALUE = "max_expected_value"
REASON_NO_CANDIDATES = "no_candidates"
REASON_NO_ALLOWED_CANDIDATE = "no_allowed_candidate"

# V1 priority as a lookup, used ONLY to break exact expected-value ties.
_PRIORITY_INDEX: dict[str, int] = {
    intervention: index
    for index, intervention in enumerate(INTERVENTION_PRIORITY)
}

# An intervention absent from the priority table sorts after every listed one
# rather than crashing the tie-break; the alphabetical term then orders it.
_UNRANKED = len(INTERVENTION_PRIORITY)


class OptimizerError(Exception):
    """The optimizer received state it cannot turn into a safe decision.

    Raised instead of guessing. An unusable estimate, an unusable economic
    model, or a malformed candidate set stops the decision rather than
    producing an economic result from invalid inputs.
    """


@dataclass(frozen=True)
class AllowedCandidates:
    """The policy-authorized candidate set — the optimizer's only input gate.

    The authorized set is derived from authoritative ``PolicyDecision``
    objects, which this type filters itself. A denied candidate, a candidate
    with no decision, a decision authorizing a different intervention, and
    ``no_action`` are all excluded before the optimizer ever runs.

    The invariant is enforced on EVERY construction path, not just on the
    ``from_policy_decisions`` convenience path: the authorizing decisions are
    carried on the value and re-validated in ``__post_init__``. Constructing
    this type directly with a fabricated ``allowed`` tuple therefore fails
    unless the caller can also present a genuine ALLOW decision bound to that
    exact intervention — at which point the candidate is authorized by
    definition. There is no way to present the optimizer with a candidate that
    policy did not authorize.
    """

    considered: tuple[str, ...]
    allowed: tuple[str, ...]
    decisions: Mapping[str, PolicyDecision]

    def __post_init__(self) -> None:
        for name in ("considered", "allowed"):
            value = getattr(self, name)
            if not isinstance(value, (list, tuple)):
                raise OptimizerError(f"{name} must be a sequence")
            value = tuple(value)
            # The same input-integrity rules the factory applies, applied here
            # too: the two construction paths must be indistinguishable, or the
            # weaker one becomes the contract.
            seen: set[str] = set()
            for candidate in value:
                if (
                    not isinstance(candidate, str)
                    or candidate not in CANDIDATE_INTERVENTIONS
                ):
                    raise OptimizerError(
                        f"{name} intervention {candidate!r} is not one of "
                        f"{sorted(CANDIDATE_INTERVENTIONS)}"
                    )
                if candidate in seen:
                    raise OptimizerError(
                        f"duplicate {name} intervention {candidate!r}"
                    )
                seen.add(candidate)
            object.__setattr__(self, name, value)
        if not isinstance(self.decisions, Mapping):
            raise OptimizerError("decisions must be a mapping")

        for candidate in self.allowed:
            if candidate == NO_ACTION:
                # no_action is not executable, so it is never an authorized
                # economic option; it is the absence of one.
                raise OptimizerError(
                    f"{NO_ACTION!r} is not executable and can never be an "
                    "allowed candidate"
                )
            if candidate not in self.considered:
                raise OptimizerError(
                    f"allowed candidate {candidate!r} was never considered"
                )
            decision = self.decisions.get(candidate)
            if not isinstance(decision, PolicyDecision):
                raise OptimizerError(
                    f"allowed candidate {candidate!r} carries no authoritative "
                    "PolicyDecision"
                )
            if decision.proposed_intervention != candidate:
                raise OptimizerError(
                    f"decision for {candidate!r} authorizes an unrelated "
                    f"intervention {decision.proposed_intervention!r}"
                )
            if decision.allowed is not True:
                raise OptimizerError(
                    f"candidate {candidate!r} is not authorized by policy "
                    "and can never be offered to the optimizer"
                )
        object.__setattr__(self, "decisions", MappingProxyType(dict(self.decisions)))

    def authorizing_decision(self, intervention: str) -> PolicyDecision:
        """Return the ALLOW decision backing an authorized intervention."""
        decision = self.decisions.get(intervention)
        if not isinstance(decision, PolicyDecision) or decision.allowed is not True:
            raise OptimizerError(
                f"no authoritative ALLOW decision exists for {intervention!r}"
            )
        return decision

    @classmethod
    def from_policy_decisions(
        cls,
        candidates: tuple[str, ...] | list[str],
        decisions: Mapping[str, PolicyDecision],
    ) -> "AllowedCandidates":
        """Derive the allowed set from authoritative policy decisions.

        Mirrors the V1 selector's fail-safe rules: an unknown intervention or
        a duplicate candidate is malformed input; a candidate with no decision
        is never allowed; a decision bound to a different intervention is
        malformed and stops the decision rather than being ignored.
        """
        if not isinstance(candidates, (list, tuple)):
            raise OptimizerError("candidates must be a sequence")
        if not isinstance(decisions, Mapping):
            raise OptimizerError("decisions must be a mapping")

        considered: list[str] = []
        seen: set[str] = set()
        for candidate in candidates:
            if (
                not isinstance(candidate, str)
                or candidate not in CANDIDATE_INTERVENTIONS
            ):
                raise OptimizerError(
                    f"candidate intervention {candidate!r} is not one of "
                    f"{sorted(CANDIDATE_INTERVENTIONS)}"
                )
            if candidate in seen:
                raise OptimizerError(
                    f"duplicate candidate intervention {candidate!r}"
                )
            seen.add(candidate)
            considered.append(candidate)

        allowed: list[str] = []
        for candidate in considered:
            if candidate == NO_ACTION:
                # no_action is not executable and is never a decision option.
                continue
            decision = decisions.get(candidate)
            if decision is None:
                # No authoritative decision: fail safe, never selectable.
                continue
            if not isinstance(decision, PolicyDecision):
                raise OptimizerError(
                    f"decision for {candidate!r} must be a PolicyDecision"
                )
            if decision.proposed_intervention != candidate:
                raise OptimizerError(
                    f"decision for {candidate!r} authorizes an unrelated "
                    f"intervention {decision.proposed_intervention!r}"
                )
            if decision.allowed:
                allowed.append(candidate)

        return cls(
            considered=tuple(considered),
            allowed=tuple(allowed),
            decisions=decisions,
        )


@dataclass(frozen=True)
class OptimizerDecision:
    """The explicit result of one economic selection.

    Carries enough to explain the decision without duplicating data that is
    already canonical elsewhere: the candidate sets, the per-candidate
    economics, the selection, and why.
    """

    candidates_considered: tuple[str, ...]
    allowed_candidates: tuple[str, ...]
    evaluations: tuple[CandidateEvaluation, ...]
    selected_intervention: str
    selection_reason: str

    @property
    def is_actionable(self) -> bool:
        """True only when a real intervention (not no_action) was selected."""
        return self.selected_intervention != NO_ACTION

    def to_dict(self) -> dict[str, Any]:
        """Serialize the decision for audit and trace output."""
        return {
            "candidates_considered": list(self.candidates_considered),
            "allowed_candidates": list(self.allowed_candidates),
            "evaluations": [
                evaluation.to_dict() for evaluation in self.evaluations
            ],
            "selected_intervention": self.selected_intervention,
            "selection_reason": self.selection_reason,
        }


def _ranking_key(evaluation: CandidateEvaluation) -> tuple[int, int, str]:
    """Deterministic total ordering: EV desc, V1 priority, then name.

    Depends only on the evaluation's own content, so the ranking is invariant
    under any permutation of the candidate list.
    """
    return (
        -evaluation.expected_value_paise,
        _PRIORITY_INDEX.get(evaluation.intervention, _UNRANKED),
        evaluation.intervention,
    )


class EconomicInterventionOptimizer:
    """Selects the highest expected-value policy-allowed intervention.

    Pure and stateless. It never executes, never persists, never authorizes,
    and never consults recovery ground truth.
    """

    def __init__(
        self,
        estimator: RecoveryProbabilityEstimator,
        model: EconomicModel,
    ) -> None:
        if not isinstance(model, EconomicModel):
            raise OptimizerError("model must be an EconomicModel")
        self._estimator = estimator
        self._model = model

    def select(
        self,
        event: PaymentEvent,
        classification: ClassificationResult,
        allowed_candidates: AllowedCandidates,
    ) -> OptimizerDecision:
        """Choose exactly one intervention, or an explicit no-action result."""
        if not isinstance(event, PaymentEvent):
            raise OptimizerError("event must be a PaymentEvent")
        if not isinstance(classification, ClassificationResult):
            raise OptimizerError("classification must be a ClassificationResult")
        if not isinstance(allowed_candidates, AllowedCandidates):
            raise OptimizerError(
                "allowed_candidates must be an AllowedCandidates derived from "
                "authoritative policy decisions"
            )
        if event.event_id != classification.event_id:
            raise OptimizerError(
                "event and classification event_id do not match"
            )

        if not allowed_candidates.considered:
            return self._no_action(allowed_candidates, REASON_NO_CANDIDATES)
        if not allowed_candidates.allowed:
            return self._no_action(
                allowed_candidates, REASON_NO_ALLOWED_CANDIDATE
            )

        evaluations: list[CandidateEvaluation] = []
        for intervention in allowed_candidates.allowed:
            # An ALLOW is authorization for ONE intervention on ONE event; a
            # decision issued for a different event authorizes nothing here.
            decision = allowed_candidates.authorizing_decision(intervention)
            if decision.event_id != event.event_id:
                raise OptimizerError(
                    f"decision authorizing {intervention!r} belongs to event "
                    f"{decision.event_id!r}, not {event.event_id!r}"
                )
            probability = self._estimator.estimate(
                event, classification, intervention
            )
            if not isinstance(probability, RecoveryProbability):
                raise OptimizerError(
                    f"estimator returned {type(probability).__name__} for "
                    f"{intervention!r}; a RecoveryProbability is required"
                )
            evaluations.append(
                evaluate_candidate(
                    intervention=intervention,
                    amount_paise=event.amount_paise,
                    probability=probability,
                    model=self._model,
                )
            )

        ranked = tuple(sorted(evaluations, key=_ranking_key))
        return OptimizerDecision(
            candidates_considered=allowed_candidates.considered,
            allowed_candidates=allowed_candidates.allowed,
            evaluations=ranked,
            selected_intervention=ranked[0].intervention,
            selection_reason=REASON_MAX_EXPECTED_VALUE,
        )

    @staticmethod
    def _no_action(
        allowed_candidates: AllowedCandidates, reason: str
    ) -> OptimizerDecision:
        """Return the controlled no-action result; nothing is ever executed."""
        return OptimizerDecision(
            candidates_considered=allowed_candidates.considered,
            allowed_candidates=allowed_candidates.allowed,
            evaluations=(),
            selected_intervention=NO_ACTION,
            selection_reason=reason,
        )

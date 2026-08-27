"""Deterministic V1 intervention selector.

Phase 7: given the advisory candidate interventions from the AI classifier
(Phase 5) and the authoritative policy decisions from the deterministic
policy gate (Phase 6), the selector chooses exactly one intervention to
execute. Selection is deterministic: no LLM reasoning, no randomness, and no
benchmark/recovery information is ever consulted. no_action is selected when
no actionable candidate is authorized. This module never executes anything.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

from .classification import CANDIDATE_INTERVENTIONS
from .policy import PolicyDecision

# The locked V1 priority ordering (highest first). Do not change.
INTERVENTION_PRIORITY: tuple[str, ...] = (
    "retry_delayed",
    "payment_link",
    "reminder",
    "alternate_method_prompt",
    "retry_immediate",
)

# no_action is explicitly non-executable and is never selected as an action.
NO_ACTION: str = "no_action"

# Priority index by intervention, for deterministic comparison.
_PRIORITY_INDEX: dict[str, int] = {
    intervention: index
    for index, intervention in enumerate(INTERVENTION_PRIORITY)
}


class SelectionError(Exception):
    """The selector received malformed input and cannot select safely."""


@dataclass(frozen=True)
class InterventionSelection:
    """The single intervention selected for execution.

    When no authorized actionable candidate exists, selected_intervention is
    the explicit no_action value, which is never executed.
    """

    selected_intervention: str

    def __post_init__(self) -> None:
        if self.selected_intervention not in CANDIDATE_INTERVENTIONS:
            raise SelectionError(
                f"selected_intervention must be one of "
                f"{sorted(CANDIDATE_INTERVENTIONS)}, got "
                f"{self.selected_intervention!r}"
            )

    @property
    def is_actionable(self) -> bool:
        """True only when a real intervention (not no_action) was selected."""
        return self.selected_intervention != NO_ACTION


def select_intervention(
    candidates: tuple[str, ...] | list[str],
    decisions: Mapping[str, PolicyDecision],
) -> InterventionSelection:
    """Select exactly one intervention from the authorized candidates.

    Conceptually:

        candidate interventions
            -> remove invalid / no_action
            -> keep candidates with an authoritative ALLOW decision
            -> apply the locked deterministic priority
            -> select exactly one (no_action when nothing is authorized)

    A candidate with no matching policy decision, or with a DENY decision,
    is never selected. A decision whose proposed_intervention does not match
    the candidate it authorizes is a malformed input and selection stops.
    """
    if not isinstance(candidates, (list, tuple)):
        raise SelectionError("candidates must be a sequence")
    if not isinstance(decisions, Mapping):
        raise SelectionError("decisions must be a mapping")

    actionable: list[str] = []
    seen: set[str] = set()
    for candidate in candidates:
        if not isinstance(candidate, str) or candidate not in CANDIDATE_INTERVENTIONS:
            raise SelectionError(
                f"candidate intervention {candidate!r} is not one of "
                f"{sorted(CANDIDATE_INTERVENTIONS)}"
            )
        if candidate in seen:
            raise SelectionError(f"duplicate candidate intervention {candidate!r}")
        seen.add(candidate)
        if candidate != NO_ACTION:
            actionable.append(candidate)

    authorized: list[str] = []
    for candidate in actionable:
        decision = decisions.get(candidate)
        if decision is None:
            # No authoritative decision: fail safe by never selecting it.
            continue
        if not isinstance(decision, PolicyDecision):
            raise SelectionError(
                f"decision for {candidate!r} must be a PolicyDecision"
            )
        if decision.proposed_intervention != candidate:
            raise SelectionError(
                f"decision for {candidate!r} authorizes an unrelated intervention "
                f"{decision.proposed_intervention!r}"
            )
        if decision.allowed:
            authorized.append(candidate)

    if not authorized:
        return InterventionSelection(selected_intervention=NO_ACTION)

    selected = min(authorized, key=lambda intervention: _PRIORITY_INDEX[intervention])
    return InterventionSelection(selected_intervention=selected)
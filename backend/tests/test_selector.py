"""Phase 7 selector tests: deterministic V1 intervention selection."""

from __future__ import annotations

import pytest

from app.policy import PolicyDecision
from app.selector import (
    INTERVENTION_PRIORITY,
    NO_ACTION,
    InterventionSelection,
    SelectionError,
    select_intervention,
)

VALID_EVALUATED_AT = "2026-08-27T13:00:00+00:00"


def _decision(
    intervention: str,
    allowed: bool,
    denial_reason: str | None = None,
    rules: tuple[str, ...] = ("fraud_check_passed",),
) -> PolicyDecision:
    if allowed:
        denial_reason = None
    return PolicyDecision.from_dict(
        {
            "event_id": "evt_select",
            "proposed_intervention": intervention,
            "allowed": allowed,
            "denial_reason": denial_reason,
            "policy_rules_applied": list(rules),
            "evaluated_at": VALID_EVALUATED_AT,
        }
    )


def _allow(intervention: str) -> PolicyDecision:
    return _decision(intervention, True)


def _deny(intervention: str, reason: str) -> PolicyDecision:
    return _decision(intervention, False, denial_reason=reason, rules=(reason,))


def test_priority_order_is_locked() -> None:
    assert INTERVENTION_PRIORITY == (
        "retry_delayed",
        "payment_link",
        "reminder",
        "alternate_method_prompt",
        "retry_immediate",
    )


def test_highest_priority_selected() -> None:
    selection = select_intervention(
        ("retry_delayed", "payment_link"),
        {"retry_delayed": _allow("retry_delayed"), "payment_link": _allow("payment_link")},
    )
    assert selection.selected_intervention == "retry_delayed"


def test_lower_priority_pair_selected_when_higher_absent() -> None:
    selection = select_intervention(
        ("payment_link", "reminder"),
        {"payment_link": _allow("payment_link"), "reminder": _allow("reminder")},
    )
    assert selection.selected_intervention == "payment_link"


def test_all_candidates_select_highest_priority() -> None:
    all_candidates = (
        "retry_delayed",
        "payment_link",
        "reminder",
        "alternate_method_prompt",
        "retry_immediate",
    )
    selection = select_intervention(
        all_candidates,
        {c: _allow(c) for c in all_candidates},
    )
    assert selection.selected_intervention == "retry_delayed"


def test_denied_higher_priority_never_selected() -> None:
    selection = select_intervention(
        ("retry_delayed", "payment_link"),
        {
            "retry_delayed": _deny("retry_delayed", "event_cooldown_active"),
            "payment_link": _allow("payment_link"),
        },
    )
    assert selection.selected_intervention == "payment_link"


def test_all_denied_selects_no_action() -> None:
    selection = select_intervention(
        ("retry_delayed", "payment_link"),
        {
            "retry_delayed": _deny("retry_delayed", "fraud_protection"),
            "payment_link": _deny("payment_link", "fraud_protection"),
        },
    )
    assert selection.selected_intervention == NO_ACTION
    assert selection.is_actionable is False


def test_no_decision_means_not_selected() -> None:
    selection = select_intervention(("retry_immediate",), {})
    assert selection.selected_intervention == NO_ACTION


def test_explicit_no_action_never_executed() -> None:
    selection = select_intervention(("no_action",), {})
    assert selection.selected_intervention == NO_ACTION
    assert selection.is_actionable is False


def test_only_no_action_candidate() -> None:
    selection = select_intervention(("no_action",), {})
    assert selection.selected_intervention == NO_ACTION


def test_invalid_candidate_raises() -> None:
    with pytest.raises(SelectionError):
        select_intervention(("wire_transfer",), {})


def test_duplicate_candidate_raises() -> None:
    with pytest.raises(SelectionError):
        select_intervention(("retry_delayed", "retry_delayed"), {})


def test_mismatched_decision_never_selects() -> None:
    with pytest.raises(SelectionError):
        select_intervention(
            ("payment_link",),
            {"payment_link": _allow("retry_delayed")},
        )


def test_non_decision_value_raises() -> None:
    with pytest.raises(SelectionError):
        select_intervention(("payment_link",), {"payment_link": "allowed"})


def test_active_candidates_ignored_decision_for_others() -> None:
    decisions = {
        "retry_delayed": _deny("retry_delayed", "event_cooldown_active"),
        "payment_link": _allow("payment_link"),
    }
    selection = select_intervention(("retry_delayed", "payment_link"), decisions)
    assert selection.selected_intervention == "payment_link"
    assert selection.is_actionable is True


def test_selection_is_deterministic() -> None:
    candidates = (
        "retry_delayed",
        "payment_link",
        "reminder",
        "alternate_method_prompt",
        "retry_immediate",
    )
    decisions = {c: _allow(c) for c in candidates}
    first = select_intervention(candidates, decisions)
    second = select_intervention(candidates, decisions)
    assert first == second == InterventionSelection("retry_delayed")


def test_empty_candidates_select_no_action() -> None:
    selection = select_intervention((), {})
    assert selection.selected_intervention == NO_ACTION

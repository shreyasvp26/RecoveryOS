"""Bounded execution orchestration service.

Phase 7: wires the frozen chain for one event —

    load event
        -> load classification        (none => no execution)
        -> authoritative policy decisions for every actionable candidate
        -> deterministic selection
        -> bounded execution
        -> persist outcome + attempt
        -> explicit result

The client supplies no intervention and no authorization: the authoritative
system state completely determines whether, and what, executes. No policy
decision is fabricated, and a denied candidate can never be selected or
executed. This service never calls the LLM, never benchmarks, and never
decides recoverability.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime

from .db import (
    get_classification_result,
    get_payment_event,
    get_policy_decision,
    get_policy_history,
    insert_execution_outcome,
    insert_intervention_attempt,
    insert_policy_decision,
)
from .executor import BoundedExecutor, ExecutionOutcome
from .policy import (
    InterventionAttempt,
    PolicyConfig,
    PolicyDecision,
    PolicyEngine,
    PolicyInput,
)
from .selector import NO_ACTION, select_intervention

STATUS_NOT_FOUND = "not_found"
STATUS_MISSING_CLASSIFICATION = "missing_classification"
STATUS_NO_ACTION = "no_action"
STATUS_EXECUTION_SUCCESS = "execution_success"
STATUS_EXECUTION_FAILED = "execution_failed"


@dataclass(frozen=True)
class ExecutionServiceResult:
    """The explicit outcome of running the execution flow for one event."""

    status: str
    event_id: str
    selected_intervention: str = NO_ACTION
    decision: PolicyDecision | None = None
    outcome: ExecutionOutcome | None = None


def _persist_decision(
    conn: sqlite3.Connection, decision: PolicyDecision
) -> PolicyDecision:
    """Persist a fresh authoritative decision, reusing an identical record.

    The decision was just produced by the deterministic engine; persisting it
    keeps the audit chain. If an identical decision already exists at the same
    timestamp, the persisted record is authoritative and is reused rather than
    overwritten or fabricated.
    """
    try:
        insert_policy_decision(conn, decision)
    except sqlite3.IntegrityError:
        existing = get_policy_decision(
            conn,
            decision.event_id,
            decision.proposed_intervention,
            decision.evaluated_at,
        )
        if existing is None:
            raise
        return existing
    return decision


def execute_event(
    conn: sqlite3.Connection,
    event_id: str,
    evaluation_time: datetime,
    config: PolicyConfig,
    razorpay_client: object | None,
) -> ExecutionServiceResult:
    """Run the deterministic selection + bounded execution flow for one event."""
    event = get_payment_event(conn, event_id)
    if event is None:
        return ExecutionServiceResult(status=STATUS_NOT_FOUND, event_id=event_id)

    classification = get_classification_result(conn, event_id)
    if classification is None:
        return ExecutionServiceResult(
            status=STATUS_MISSING_CLASSIFICATION, event_id=event_id
        )

    history = get_policy_history(conn, event, evaluation_time)
    decisions: dict[str, PolicyDecision] = {}
    for candidate in classification.candidate_interventions:
        if candidate == NO_ACTION:
            continue
        decision = PolicyEngine().evaluate(
            PolicyInput(
                event=event,
                classification=classification,
                proposed_intervention=candidate,
                history=history,
                evaluation_time=evaluation_time,
            ),
            config,
        )
        decisions[candidate] = _persist_decision(conn, decision)

    selection = select_intervention(classification.candidate_interventions, decisions)
    if not selection.is_actionable:
        return ExecutionServiceResult(
            status=STATUS_NO_ACTION,
            event_id=event_id,
            selected_intervention=NO_ACTION,
        )

    selected = selection.selected_intervention
    decision = decisions[selected]
    outcome = BoundedExecutor().execute(event, selected, decision, razorpay_client)

    insert_execution_outcome(conn, outcome)
    insert_intervention_attempt(
        conn,
        InterventionAttempt(
            event_id=event.event_id,
            intervention=selected,
            customer_id=event.customer_id,
            cost_paise=config.intervention_cost(selected),
            attempted_at=outcome.reported_at,
            status="successful" if outcome.status == "SUCCESS" else "failed",
        ),
    )

    status = (
        STATUS_EXECUTION_SUCCESS
        if outcome.status == "SUCCESS"
        else STATUS_EXECUTION_FAILED
    )
    return ExecutionServiceResult(
        status=status,
        event_id=event_id,
        selected_intervention=selected,
        decision=decision,
        outcome=outcome,
    )

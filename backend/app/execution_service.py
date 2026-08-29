"""Bounded execution orchestration service.

Phase 7, updated in Phase 16 — wires the chain for one event:

    load event
        -> load classification        (none => no execution)
        -> authoritative policy decisions for every actionable candidate
        -> policy-allowed candidate set
        -> deterministic economic selection (highest expected value)
        -> bounded execution
        -> persist outcome + attempt
        -> explicit result

Phase 16 replaced the V1 fixed-priority selector at this single integration
point. The ordering is non-negotiable: the policy gate runs FIRST and its
output is filtered into an ``AllowedCandidates`` set, which is the only thing
the optimizer can see. A policy-denied candidate is structurally unable to
reach the optimizer, so it can never be selected however valuable it looks.

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
from .classification import ClassificationResult
from .executor import BoundedExecutor, ExecutionOutcome
from .models import PaymentEvent
from .policy import (
    InterventionAttempt,
    PolicyConfig,
    PolicyDecision,
    PolicyEngine,
    PolicyInput,
)
from .economics import DEFAULT_ECONOMIC_MODEL
from .estimator import RecoveryProbabilityEstimator
from .optimizer import (
    AllowedCandidates,
    EconomicInterventionOptimizer,
    OptimizerDecision,
)
from .selector import NO_ACTION, select_intervention

STATUS_NOT_FOUND = "not_found"
STATUS_MISSING_CLASSIFICATION = "missing_classification"
STATUS_NO_ACTION = "no_action"
STATUS_EXECUTION_SUCCESS = "execution_success"
STATUS_EXECUTION_FAILED = "execution_failed"

# Which selection mechanism decides among the policy-allowed candidates.
# Both run strictly AFTER the policy gate and both choose only from candidates
# it authorized; they differ solely in how they rank those survivors.
SELECTION_V2_ECONOMIC = "v2_economic"
SELECTION_V1_FIXED_PRIORITY = "v1_fixed_priority"
SELECTION_STRATEGIES: frozenset[str] = frozenset(
    {SELECTION_V2_ECONOMIC, SELECTION_V1_FIXED_PRIORITY}
)


class SelectionStrategyError(Exception):
    """An unknown selection strategy was requested; nothing is executed."""


@dataclass(frozen=True)
class ExecutionServiceResult:
    """The explicit outcome of running the execution flow for one event."""

    status: str
    event_id: str
    selected_intervention: str = NO_ACTION
    decision: PolicyDecision | None = None
    outcome: ExecutionOutcome | None = None
    optimizer_decision: OptimizerDecision | None = None


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


def _select(
    event: PaymentEvent,
    classification: ClassificationResult,
    decisions: dict[str, PolicyDecision],
    strategy: str,
) -> tuple[str, OptimizerDecision | None]:
    """Choose one intervention from the policy-authorized candidates.

    Both strategies consume the SAME authoritative policy decisions and can
    only ever return a candidate that carries an ALLOW. The V2 path additionally
    returns its economic trace; the V1 path has no economics to report.
    """
    if strategy == SELECTION_V2_ECONOMIC:
        # The optimizer only ever sees candidates the policy gate authorized:
        # AllowedCandidates derives that set from the authoritative decisions,
        # so a denied candidate cannot reach economic evaluation at all.
        allowed_candidates = AllowedCandidates.from_policy_decisions(
            classification.candidate_interventions, decisions
        )
        decision = EconomicInterventionOptimizer(
            estimator=RecoveryProbabilityEstimator(),
            model=DEFAULT_ECONOMIC_MODEL,
        ).select(event, classification, allowed_candidates)
        return decision.selected_intervention, decision

    if strategy == SELECTION_V1_FIXED_PRIORITY:
        selection = select_intervention(
            classification.candidate_interventions, decisions
        )
        return selection.selected_intervention, None

    raise SelectionStrategyError(
        f"selection_strategy must be one of {sorted(SELECTION_STRATEGIES)}, "
        f"got {strategy!r}"
    )


def execute_event(
    conn: sqlite3.Connection,
    event_id: str,
    evaluation_time: datetime,
    config: PolicyConfig,
    razorpay_client: object | None,
    selection_strategy: str = SELECTION_V2_ECONOMIC,
) -> ExecutionServiceResult:
    """Run the deterministic selection + bounded execution flow for one event.

    ``selection_strategy`` chooses how the policy-allowed survivors are ranked.
    Production defaults to the V2 economic optimizer. The benchmark harness
    pins the V1 fixed-priority selector so that the recorded V1 baseline stays
    reproducible and the V2 arm can be introduced deliberately in Phase 17
    against a signal-bearing outcome model. The strategy affects RANKING ONLY:
    it can never widen the authorized set.
    """
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

    selected, optimizer_decision = _select(
        event, classification, decisions, selection_strategy
    )
    if selected == NO_ACTION:
        return ExecutionServiceResult(
            status=STATUS_NO_ACTION,
            event_id=event_id,
            selected_intervention=NO_ACTION,
            optimizer_decision=optimizer_decision,
        )

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
        optimizer_decision=optimizer_decision,
    )

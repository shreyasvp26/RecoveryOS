"""Bounded execution orchestration service.

Phase 7, updated in Phase 16 — wires the chain for one event:

    load event
        -> load classification        (none => no execution)
        -> authoritative policy decisions for every actionable candidate
        -> policy-allowed candidate set
        -> deterministic economic selection (highest expected value)
        -> persist the economic decision (Phase 18 audit record)
        -> durable execution claim (Phase 21 concurrency boundary)
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

Phase 21 added one thing only: a durable claim taken immediately before the
executor runs. The deterministic gate blocks sequential duplicates from
persisted history, but two simultaneous requests can both read that history
before either writes its attempt, so both would be authorized and both would
reach the provider. The claim is a concurrency primitive, not authorization:
it decides WHICH of several already-authorized attempts may cross the external
side-effect boundary, and it can never make a denied candidate executable.

Whether the claim survives the attempt depends on what the boundary reported.
A success or an unknown provider result keeps it, so the action is never
attempted again; only a known failure — one that proves nothing happened
provider-side — releases it and leaves the Phase 11 retry path intact.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Mapping

from .db import (
    claim_execution,
    get_classification_result,
    get_execution_claim,
    get_optimizer_decision,
    get_payment_event,
    get_policy_decision,
    get_policy_history,
    insert_execution_outcome,
    insert_intervention_attempt,
    insert_optimizer_decision,
    insert_policy_decision,
    release_execution_claim,
    resolve_execution_claim,
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
from .optimizer_audit import OptimizerDecisionRecord
from .razorpay_client import marks_provider_result_unknown
from .selector import NO_ACTION, select_intervention

STATUS_NOT_FOUND = "not_found"
STATUS_MISSING_CLASSIFICATION = "missing_classification"
STATUS_NO_ACTION = "no_action"
STATUS_EXECUTION_SUCCESS = "execution_success"
STATUS_EXECUTION_FAILED = "execution_failed"

# Phase 21 concurrency outcomes. None of these is a decision: each reports that
# the single permitted attempt for this logical action belongs to someone else,
# has already happened, or ended in a state RecoveryOS cannot confirm.
STATUS_EXECUTION_IN_PROGRESS = "execution_in_progress"
STATUS_ALREADY_EXECUTED = "already_executed"
STATUS_PROVIDER_RESULT_UNKNOWN = "provider_result_unknown"

CLAIM_STATUS_HELD = "claimed"
CLAIM_STATUS_COMPLETED = "completed"
CLAIM_STATUS_PROVIDER_RESULT_UNKNOWN = "provider_result_unknown"

# Recorded when the provider was called but RecoveryOS could not record what
# came back. The side effect may exist, so the claim is never released.
PROVIDER_RESULT_UNKNOWN_DETAIL = (
    "the provider was called but the result could not be confirmed or "
    "persisted; a side effect may exist, so this action is never retried "
    "automatically"
)

# Recorded when the provider boundary itself reported a failure it could not
# attribute — a timeout, a lost response, an unreadable reply. The outcome is
# persisted as FAILED, but a real Payment Link may exist, so the claim stays.
AMBIGUOUS_PROVIDER_RESULT_DETAIL = (
    "the provider did not return a result RecoveryOS could interpret; a real "
    "Payment Link may exist, so this action is not attempted again"
)

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


def _persist_optimizer_decision(
    conn: sqlite3.Connection,
    event_id: str,
    decided_at: str,
    decision: OptimizerDecision,
) -> OptimizerDecisionRecord:
    """Record the economic decision (Phase 18), reusing an identical record.

    Persisted BEFORE execution is attempted, so an execution failure still
    leaves an auditable record of what RecoveryOS decided. Re-running the same
    event at the same evaluation time re-derives an identical decision (the
    optimizer is deterministic), so the already-persisted row is authoritative
    and is reused rather than overwritten. A genuinely different decision at
    the same timestamp is a contradiction and is raised, never silently
    dropped.
    """
    record = OptimizerDecisionRecord.from_decision(event_id, decided_at, decision)
    try:
        insert_optimizer_decision(conn, record)
    except sqlite3.IntegrityError:
        existing = get_optimizer_decision(conn, event_id, decided_at)
        if existing is None or existing != record:
            raise
        return existing
    return record


def _claim_conflict_result(
    conn: sqlite3.Connection,
    event_id: str,
    intervention: str,
    optimizer_decision: OptimizerDecision | None,
) -> ExecutionServiceResult:
    """Report why this attempt must not proceed, from the existing claim.

    Nothing is executed and nothing is written: another attempt owns the single
    permitted crossing of the side-effect boundary for this logical action.
    """
    existing = get_execution_claim(conn, event_id, intervention)
    if existing is not None and existing["status"] == CLAIM_STATUS_PROVIDER_RESULT_UNKNOWN:
        status = STATUS_PROVIDER_RESULT_UNKNOWN
    elif existing is not None and existing["status"] == CLAIM_STATUS_HELD:
        status = STATUS_EXECUTION_IN_PROGRESS
    else:
        status = STATUS_ALREADY_EXECUTED
    return ExecutionServiceResult(
        status=status,
        event_id=event_id,
        selected_intervention=intervention,
        optimizer_decision=optimizer_decision,
    )


def _provider_result_is_unknown(outcome: ExecutionOutcome) -> bool:
    """Whether the executor reported a failure it could not attribute.

    Only a REAL_RAZORPAY attempt can leave a side effect behind, so only that
    mode can be ambiguous; a SIMULATED failure contacted nobody and keeps its
    existing retry semantics untouched.
    """
    return (
        outcome.status == "FAILED"
        and outcome.execution_mode == "REAL_RAZORPAY"
        and marks_provider_result_unknown(outcome.detail)
    )


def _park_claim_as_unknown(
    conn: sqlite3.Connection, event_id: str, intervention: str, resolved_at: str
) -> None:
    """Mark a claim unknown, never masking the original failure.

    Recording the uncertainty must not replace the exception that caused it, so
    a failure to write the claim status is deliberately swallowed here and the
    original error continues to propagate.
    """
    try:
        resolve_execution_claim(
            conn,
            event_id,
            intervention,
            CLAIM_STATUS_PROVIDER_RESULT_UNKNOWN,
            resolved_at,
            PROVIDER_RESULT_UNKNOWN_DETAIL,
        )
    except sqlite3.Error:
        pass


def select_for_strategy(
    event: PaymentEvent,
    classification: ClassificationResult,
    decisions: Mapping[str, PolicyDecision],
    strategy: str,
    estimator: RecoveryProbabilityEstimator | None = None,
) -> tuple[str, OptimizerDecision | None]:
    """Choose one intervention from the policy-authorized candidates.

    Both strategies consume the SAME authoritative policy decisions and can
    only ever return a candidate that carries an ALLOW. The V2 path additionally
    returns its economic trace; the V1 path has no economics to report.

    Public because the Phase 17 benchmark evaluates the V1 and V2 arms through
    THIS function rather than reimplementing either decision path. Selection is
    pure — no database, no execution, no provider — so the benchmark can reuse
    it without borrowing the persistence and Razorpay boundary that
    ``execute_event`` necessarily carries.

    Phase 23 (additive): ``estimator`` may inject the calibration-driven
    adaptive estimator. When omitted (the default), the frozen Phase 16
    ``RecoveryProbabilityEstimator`` is used, so existing V1/V2 benchmark and
    replay arms reproduce their recorded results exactly. The optimizer's rule
    and the policy authorization boundary are unchanged; the estimator only
    ranks, it never authorizes or executes.
    """
    if strategy == SELECTION_V2_ECONOMIC:
        # The optimizer only ever sees candidates the policy gate authorized:
        # AllowedCandidates derives that set from the authoritative decisions,
        # so a denied candidate cannot reach economic evaluation at all.
        allowed_candidates = AllowedCandidates.from_policy_decisions(
            classification.candidate_interventions, decisions
        )
        decision = EconomicInterventionOptimizer(
            estimator=estimator or RecoveryProbabilityEstimator(),
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
    estimator: RecoveryProbabilityEstimator | None = None,
) -> ExecutionServiceResult:
    """Run the deterministic selection + bounded execution flow for one event.

    ``selection_strategy`` chooses how the policy-allowed survivors are ranked.
    Production defaults to the V2 economic optimizer. The benchmark harness
    pins the V1 fixed-priority selector so that the recorded V1 baseline stays
    reproducible and the V2 arm can be introduced deliberately in Phase 17
    against a signal-bearing outcome model. The strategy affects RANKING ONLY:
    it can never widen the authorized set.

    Phase 23 (additive): ``estimator`` may inject the calibration-driven
    adaptive estimator through to ``select_for_strategy``. Omitted (the default)
    preserves the frozen Phase 16 behaviour exactly. Only the estimator that
    produces ranking probabilities is swapped; policy authorization, the
    optimizer rule, and the executor are unchanged.
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

    selected, optimizer_decision = select_for_strategy(
        event, classification, decisions, selection_strategy, estimator
    )
    if optimizer_decision is not None:
        # Audit before action: the economic decision exists independently of
        # whether the executor later succeeds, fails, or never runs at all.
        _persist_optimizer_decision(
            conn, event.event_id, evaluation_time.isoformat(), optimizer_decision
        )
    if selected == NO_ACTION:
        return ExecutionServiceResult(
            status=STATUS_NO_ACTION,
            event_id=event_id,
            selected_intervention=NO_ACTION,
            optimizer_decision=optimizer_decision,
        )

    decision = decisions[selected]

    # The concurrency boundary, and the LAST thing before the external side
    # effect. Policy has already authorized this candidate; the claim decides
    # only WHICH of several simultaneous authorized attempts may proceed.
    claimed_at = evaluation_time.astimezone(timezone.utc).isoformat()
    if not claim_execution(conn, event.event_id, selected, claimed_at):
        return _claim_conflict_result(conn, event_id, selected, optimizer_decision)

    try:
        outcome = BoundedExecutor().execute(event, selected, decision, razorpay_client)
    except BaseException:
        # The executor maps controlled provider failures to an explicit FAILED
        # outcome, so reaching here means something escaped it entirely and the
        # provider state is genuinely unknown. Park the claim rather than guess.
        _park_claim_as_unknown(conn, event.event_id, selected, claimed_at)
        raise

    try:
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
    except BaseException:
        _park_claim_as_unknown(conn, event.event_id, selected, claimed_at)
        raise

    if outcome.status == "SUCCESS":
        resolve_execution_claim(
            conn,
            event.event_id,
            selected,
            CLAIM_STATUS_COMPLETED,
            outcome.reported_at,
            None,
        )
    elif _provider_result_is_unknown(outcome):
        # The provider may have created a real Payment Link that RecoveryOS
        # never saw. Releasing the claim here would invite a second one, so the
        # claim is kept and this action is not attempted again.
        resolve_execution_claim(
            conn,
            event.event_id,
            selected,
            CLAIM_STATUS_PROVIDER_RESULT_UNKNOWN,
            outcome.reported_at,
            AMBIGUOUS_PROVIDER_RESULT_DETAIL,
        )
    else:
        # A known failure: the provider proved it did not act, or nothing ever
        # reached it. The attempt produced no lasting side effect, so the action
        # stays retryable exactly as it was before Phase 21. The claim existed
        # only for the duration of the attempt.
        release_execution_claim(conn, event.event_id, selected)

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

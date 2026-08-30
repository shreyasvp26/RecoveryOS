"""Outcome feedback projection (Phase 22) — evidence, not authority.

This module answers one question for a single executed action:

    RecoveryOS predicted a recovery probability. What did we actually observe?

It is a deterministic PROJECTION over records the existing decision path
already persists. There is no feedback table and no second lifecycle store:

    optimizer_decisions        the prediction RecoveryOS actually used (Phase 18)
    execution_outcomes         the bounded execution (Phase 7/11)
    webhook_recovery_outcomes  VERIFIED recovery from a paid link (Phase 12)

WHAT THIS MODULE MAY NOT DO
---------------------------
It executes nothing, authorizes nothing, and changes nothing. It never imports
the estimator, the optimizer, the policy engine, the executor or the benchmark
hidden outcome model. A prediction is READ from the persisted decision — it is
never recomputed, because a decision must be judged against the number
RecoveryOS actually used, not against a number a newer estimator would produce
today.

THE THREE WORLDS STAY SEPARATE
------------------------------
Only the operational world (REAL_RAZORPAY, real Payment Link, verified
webhook) produces an observation here. A SIMULATED execution and every
benchmark or Policy Lab simulation are structurally ineligible: no provider
observed any money, so there is no operational outcome to feed back. Hidden
benchmark ground truth can never enter this file.

UNCERTAINTY IS NEVER CONVERTED INTO FAILURE
-------------------------------------------
Waiting is PENDING. Unreadable is UNKNOWN. A failed execution is a failed
execution, not an observed payment failure. NOT_RECOVERED exists as a defined
outcome but is never inferred: the only supported provider evidence is
``payment_link.paid``, so RecoveryOS currently has no authoritative evidence
that a customer declined to pay. Fabricating it would corrupt every
calibration number downstream.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Sequence

from . import db
from .razorpay_client import marks_provider_result_unknown

# Observed payment outcomes.
OUTCOME_RECOVERED = "RECOVERED"
OUTCOME_PENDING = "PENDING"
OUTCOME_UNKNOWN = "UNKNOWN"
# Defined for completeness of the contract. Never produced from current
# evidence: see the module docstring.
OUTCOME_NOT_RECOVERED = "NOT_RECOVERED"

OUTCOMES: tuple[str, ...] = (
    OUTCOME_RECOVERED,
    OUTCOME_PENDING,
    OUTCOME_UNKNOWN,
    OUTCOME_NOT_RECOVERED,
)

# Why an execution produced no eligible operational observation. Every
# ineligible observation carries exactly one of these, so nothing is ever
# silently dropped.
REASON_ELIGIBLE = "eligible"
REASON_SIMULATED_EXECUTION = "simulated_execution"
REASON_AWAITING_OUTCOME = "awaiting_outcome"
REASON_AMBIGUOUS_PROVIDER_RESULT = "ambiguous_provider_result"
REASON_EXECUTION_FAILED = "execution_failed"
REASON_MISSING_PAYMENT_LINK_ID = "missing_payment_link_id"
REASON_MISSING_PREDICTION = "missing_prediction"

INELIGIBILITY_REASONS: tuple[str, ...] = (
    REASON_SIMULATED_EXECUTION,
    REASON_AWAITING_OUTCOME,
    REASON_AMBIGUOUS_PROVIDER_RESULT,
    REASON_EXECUTION_FAILED,
    REASON_MISSING_PAYMENT_LINK_ID,
    REASON_MISSING_PREDICTION,
)

REAL_EXECUTION_MODE = "REAL_RAZORPAY"
EXECUTION_SUCCESS = "SUCCESS"

DEFAULT_SCAN_LIMIT = 500


@dataclass(frozen=True)
class FeedbackObservation:
    """One executed action, its prediction, and what was actually observed.

    Deliberately narrow: it carries identifiers plus the few figures the
    analytics need. It duplicates no event data beyond the three segment
    dimensions the aggregations group by, and every value is copied verbatim
    from an authoritative persisted record.

    ``eligible`` means this observation may enter a calibration or performance
    statistic. ``recovered`` is the binary calibration target and is only ever
    True/False on an eligible observation; it is None whenever the evidence
    does not establish an outcome.
    """

    event_id: str
    intervention: str
    execution_mode: str
    execution_status: str
    executed_at: str
    # The prediction RecoveryOS actually used, read from the persisted
    # optimizer decision that selected this intervention. None when no such
    # decision exists (the observation is then ineligible).
    decided_at: str | None
    predicted_probability_bps: int | None
    expected_recovered_value_paise: int | None
    amount_paise: int | None
    payment_method: str | None
    bank: str | None
    failure_reason: str | None
    payment_link_id: str | None
    outcome: str
    eligible: bool
    reason: str
    recovered: bool | None
    # The TRUSTED amount the provider reported as paid. None when the provider
    # reported no amount — never substituted with the original event amount.
    recovered_amount_paise: int | None
    observed_at: str | None
    # The delivery id of the verified webhook that proves the recovery.
    evidence_id: str | None
    note: str

    def to_dict(self) -> dict[str, Any]:
        """Serialize the observation for API output and traceability."""
        return {
            "event_id": self.event_id,
            "intervention": self.intervention,
            "execution_mode": self.execution_mode,
            "execution_status": self.execution_status,
            "executed_at": self.executed_at,
            "decided_at": self.decided_at,
            "predicted_probability_bps": self.predicted_probability_bps,
            "expected_recovered_value_paise": self.expected_recovered_value_paise,
            "amount_paise": self.amount_paise,
            "payment_method": self.payment_method,
            "bank": self.bank,
            "failure_reason": self.failure_reason,
            "payment_link_id": self.payment_link_id,
            "outcome": self.outcome,
            "eligible": self.eligible,
            "reason": self.reason,
            "recovered": self.recovered,
            "recovered_amount_paise": self.recovered_amount_paise,
            "observed_at": self.observed_at,
            "evidence_id": self.evidence_id,
            "note": self.note,
        }


def find_prediction(
    optimizer_decisions: Sequence[Mapping[str, Any]],
    intervention: str,
    executed_at: str,
) -> Mapping[str, Any] | None:
    """Return the decision that predicted THIS execution, or None.

    The join is deterministic and never fuzzy: among the persisted decisions
    for this event, keep those that selected exactly this intervention and
    were decided at or before the execution was reported, then take the latest
    one. That is precisely the decision the execution acted on — an execution
    cannot have been driven by a decision made after it, and a decision that
    selected a different intervention did not drive this action.

    Ties on ``decided_at`` are broken by the order the records were read, which
    is itself ordered by ``decided_at`` in SQL, so repeated runs agree.
    """
    candidates = [
        decision
        for decision in optimizer_decisions
        if decision.get("selected_intervention") == intervention
        and str(decision.get("decided_at") or "") <= str(executed_at)
    ]
    if not candidates:
        return None
    return max(candidates, key=lambda decision: str(decision.get("decided_at") or ""))


def _evaluation_for(
    decision: Mapping[str, Any], intervention: str
) -> Mapping[str, Any] | None:
    """The persisted per-candidate economics for the selected intervention."""
    for evaluation in decision.get("evaluations") or ():
        if evaluation.get("intervention") == intervention:
            return evaluation
    return None


def _ineligible(
    *,
    event: Mapping[str, Any],
    execution: Mapping[str, Any],
    prediction: Mapping[str, Any] | None,
    evaluation: Mapping[str, Any] | None,
    outcome: str,
    reason: str,
    note: str,
) -> FeedbackObservation:
    return _observation(
        event=event,
        execution=execution,
        prediction=prediction,
        evaluation=evaluation,
        outcome=outcome,
        eligible=False,
        reason=reason,
        recovered=None,
        recovered_amount_paise=None,
        observed_at=None,
        evidence_id=None,
        note=note,
    )


def _observation(
    *,
    event: Mapping[str, Any],
    execution: Mapping[str, Any],
    prediction: Mapping[str, Any] | None,
    evaluation: Mapping[str, Any] | None,
    outcome: str,
    eligible: bool,
    reason: str,
    recovered: bool | None,
    recovered_amount_paise: int | None,
    observed_at: str | None,
    evidence_id: str | None,
    note: str,
) -> FeedbackObservation:
    return FeedbackObservation(
        event_id=str(event["event_id"]),
        intervention=str(execution["intervention"]),
        execution_mode=str(execution["execution_mode"]),
        execution_status=str(execution["status"]),
        executed_at=str(execution["reported_at"]),
        decided_at=(
            str(prediction["decided_at"]) if prediction is not None else None
        ),
        predicted_probability_bps=(
            evaluation.get("estimated_probability_bps")
            if evaluation is not None
            else None
        ),
        expected_recovered_value_paise=(
            evaluation.get("expected_recovered_value_paise")
            if evaluation is not None
            else None
        ),
        amount_paise=event.get("amount_paise"),
        payment_method=event.get("payment_method"),
        bank=event.get("bank"),
        failure_reason=event.get("failure_reason"),
        payment_link_id=execution.get("payment_link_id"),
        outcome=outcome,
        eligible=eligible,
        reason=reason,
        recovered=recovered,
        recovered_amount_paise=recovered_amount_paise,
        observed_at=observed_at,
        evidence_id=evidence_id,
        note=note,
    )


def build_observation(
    event: Mapping[str, Any],
    execution: Mapping[str, Any],
    optimizer_decisions: Sequence[Mapping[str, Any]],
    recoveries: Mapping[str, Mapping[str, Any]],
) -> FeedbackObservation:
    """Project one execution and its evidence into a feedback observation.

    Pure: the same records always produce the same observation. The order of
    the checks is the eligibility rule, and each branch states what the
    evidence does and does not establish.
    """
    intervention = str(execution["intervention"])
    prediction = find_prediction(
        optimizer_decisions, intervention, str(execution["reported_at"])
    )
    evaluation = (
        _evaluation_for(prediction, intervention) if prediction is not None else None
    )

    if execution["execution_mode"] != REAL_EXECUTION_MODE:
        return _ineligible(
            event=event,
            execution=execution,
            prediction=prediction,
            evaluation=evaluation,
            outcome=OUTCOME_UNKNOWN,
            reason=REASON_SIMULATED_EXECUTION,
            note=(
                "SIMULATED intervention: no provider was contacted, so no "
                "operational payment outcome exists to observe"
            ),
        )

    if execution["status"] != EXECUTION_SUCCESS:
        if marks_provider_result_unknown(execution.get("detail")):
            return _ineligible(
                event=event,
                execution=execution,
                prediction=prediction,
                evaluation=evaluation,
                outcome=OUTCOME_UNKNOWN,
                reason=REASON_AMBIGUOUS_PROVIDER_RESULT,
                note=(
                    "the provider was called and did not return a result "
                    "RecoveryOS could interpret; the outcome is unknown and is "
                    "not counted as a failure"
                ),
            )
        return _ineligible(
            event=event,
            execution=execution,
            prediction=prediction,
            evaluation=evaluation,
            outcome=OUTCOME_UNKNOWN,
            reason=REASON_EXECUTION_FAILED,
            note=(
                "the execution attempt itself failed, so no payment was ever "
                "requested; this is not an observed payment outcome"
            ),
        )

    payment_link_id = execution.get("payment_link_id")
    if not payment_link_id:
        return _ineligible(
            event=event,
            execution=execution,
            prediction=prediction,
            evaluation=evaluation,
            outcome=OUTCOME_UNKNOWN,
            reason=REASON_MISSING_PAYMENT_LINK_ID,
            note=(
                "a real execution succeeded without recording a Payment Link "
                "id, so no provider evidence can be correlated to it"
            ),
        )

    recovery = recoveries.get(payment_link_id)
    if recovery is None:
        return _ineligible(
            event=event,
            execution=execution,
            prediction=prediction,
            evaluation=evaluation,
            outcome=OUTCOME_PENDING,
            reason=REASON_AWAITING_OUTCOME,
            note=(
                "a real Payment Link exists and no verified payment has been "
                "observed yet; the outcome is pending, not failed"
            ),
        )

    if prediction is None or evaluation is None:
        # The recovery is real, but there is no persisted prediction to judge
        # it against. Calibration needs both halves, so it is excluded rather
        # than paired with a recomputed (and therefore different) estimate.
        return _observation(
            event=event,
            execution=execution,
            prediction=prediction,
            evaluation=evaluation,
            outcome=OUTCOME_RECOVERED,
            eligible=False,
            reason=REASON_MISSING_PREDICTION,
            recovered=None,
            recovered_amount_paise=recovery.get("amount_paid_paise"),
            observed_at=recovery.get("recovered_at"),
            evidence_id=recovery.get("delivery_id"),
            note=(
                "recovery is verified, but no persisted optimizer decision "
                "predicted this intervention, so it cannot be calibrated"
            ),
        )

    amount_paid = recovery.get("amount_paid_paise")
    note = "verified by a signed Razorpay webhook correlated to this Payment Link"
    if amount_paid is None:
        note = (
            "recovery is verified, but the provider reported no amount; the "
            "recovered amount is recorded as missing, never inferred from the "
            "original event amount"
        )
    return _observation(
        event=event,
        execution=execution,
        prediction=prediction,
        evaluation=evaluation,
        outcome=OUTCOME_RECOVERED,
        eligible=True,
        reason=REASON_ELIGIBLE,
        recovered=True,
        recovered_amount_paise=amount_paid,
        observed_at=recovery.get("recovered_at"),
        evidence_id=recovery.get("delivery_id"),
        note=note,
    )


def build_observations_for_event(
    event: Mapping[str, Any],
    executions: Sequence[Mapping[str, Any]],
    optimizer_decisions: Sequence[Mapping[str, Any]],
    recoveries: Mapping[str, Mapping[str, Any]],
) -> list[FeedbackObservation]:
    """Project every execution of one event, oldest first.

    An event can be intervened on more than once, and each attempt is its own
    observation paired with the decision that drove it. Ordering is total
    (reported time, then intervention) so repeated runs agree exactly.
    """
    ordered = sorted(
        executions,
        key=lambda row: (str(row.get("reported_at") or ""), str(row.get("intervention") or "")),
    )
    return [
        build_observation(event, execution, optimizer_decisions, recoveries)
        for execution in ordered
    ]


def build_observations(
    conn, *, scan_limit: int = DEFAULT_SCAN_LIMIT
) -> list[FeedbackObservation]:
    """Assemble every feedback observation from persisted state.

    Reads only. The scan is bounded by the same convention the Recovery
    Operations projection uses, so this stays a bounded read rather than an
    unbounded query engine.
    """
    events = db.list_events_for_recovery_queue(conn, scan_limit=scan_limit)
    event_ids = [event["event_id"] for event in events]
    executions = db.get_execution_outcomes_for_events(conn, event_ids)
    optimizer_decisions = db.get_optimizer_decisions_for_events(conn, event_ids)
    link_ids = sorted(
        {
            row["payment_link_id"]
            for rows in executions.values()
            for row in rows
            if row.get("payment_link_id")
        }
    )
    recoveries = db.get_webhook_recovery_outcomes_for_payment_links(conn, link_ids)

    observations: list[FeedbackObservation] = []
    for event in sorted(events, key=lambda row: str(row["event_id"])):
        event_executions = executions.get(event["event_id"], [])
        if not event_executions:
            continue
        observations.extend(
            build_observations_for_event(
                event,
                event_executions,
                optimizer_decisions.get(event["event_id"], []),
                recoveries,
            )
        )
    return observations


def eligible_observations(
    observations: Sequence[FeedbackObservation],
) -> list[FeedbackObservation]:
    """Filter to the observations that may enter a statistic."""
    return [observation for observation in observations if observation.eligible]


def ineligibility_counts(
    observations: Sequence[FeedbackObservation],
) -> dict[str, int]:
    """Count why observations were excluded (every reason key is present)."""
    counts = {reason: 0 for reason in INELIGIBILITY_REASONS}
    for observation in observations:
        if observation.eligible:
            continue
        counts[observation.reason] = counts.get(observation.reason, 0) + 1
    return counts

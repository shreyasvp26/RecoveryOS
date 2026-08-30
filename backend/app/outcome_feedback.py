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

VERIFIED RECOVERY IS NOT A CALIBRATION SAMPLE
---------------------------------------------
These are two different facts and the projection keeps them apart:

``verified_recovery``
    Authoritative positive evidence: the provider confirmed this link was
    paid. Always worth reporting, and reported on its own terms.

``calibration_eligible``
    This observation may enter a recovery-RATE denominator. That requires a
    TERMINAL BINARY outcome — RECOVERED or NOT_RECOVERED — plus the prediction
    that drove it.

Conflating them is the specific error this module must not make. Counting only
verified recoveries as the denominator makes the observed recovery rate 100% by
construction, because absence of a paid webhook is not evidence of non-payment.
Since the current provider contract yields no authoritative negative outcome,
the terminal-outcome population is normally EMPTY, and the honest result is
that no recovery rate can be computed yet — while the verified recoveries are
still counted and shown.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Sequence

from datetime import datetime, timezone

from . import db
# The repository's single timezone-aware ISO8601 parser, reused rather than
# reimplemented so feedback interprets a stored timestamp exactly as the rest
# of RecoveryOS does. It is a pure parsing utility: importing it grants this
# module no policy authority, and the integrity tests pin that no other policy
# symbol may be imported here.
from .policy import PolicyValidationError, parse_aware_datetime
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

# The outcomes that are TERMINAL: the payment question is settled, one way or
# the other. Only these can form a recovery-rate denominator. PENDING and
# UNKNOWN are explicitly absent, because "not observed yet" and "cannot tell"
# are not observations of a result.
TERMINAL_OUTCOMES: frozenset[str] = frozenset(
    {OUTCOME_RECOVERED, OUTCOME_NOT_RECOVERED}
)

# Why an execution produced no calibration-eligible observation. Every
# non-eligible observation carries exactly one of these, so nothing is ever
# silently dropped.
REASON_CALIBRATION_ELIGIBLE = "calibration_eligible"
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

# The projection is driven by the EXECUTION table, so its population is every
# execution RecoveryOS ever recorded rather than the events that happen to be
# most recent. The limit is a safety bound on one request's working set, not a
# reporting window: whenever it actually bites, the payload says so explicitly
# instead of presenting a prefix as though it were the whole history.
DEFAULT_OBSERVATION_LIMIT = 5_000

# Sorts an unparseable timestamp last without discarding the record.
_UNORDERABLE_INSTANT = datetime.max.replace(tzinfo=timezone.utc)



@dataclass(frozen=True)
class FeedbackObservation:
    """One executed action, its prediction, and what was actually observed.

    Deliberately narrow: it carries identifiers plus the few figures the
    analytics need. It duplicates no event data beyond the three segment
    dimensions the aggregations group by, and every value is copied verbatim
    from an authoritative persisted record.

    Three separate facts, deliberately not collapsed into one flag:

    ``verified_recovery``  authoritative positive provider evidence exists.
    ``terminal``           the payment question is settled (RECOVERED or
                           NOT_RECOVERED), so it could form a rate denominator.
    ``calibration_eligible``  terminal AND carrying the prediction that drove
                           it, so it can be compared against a prediction.

    ``recovered`` is the binary calibration target: True on RECOVERED, False on
    NOT_RECOVERED, and None whenever the evidence settles nothing.
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
    terminal: bool
    calibration_eligible: bool
    verified_recovery: bool
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
            "terminal": self.terminal,
            "calibration_eligible": self.calibration_eligible,
            "verified_recovery": self.verified_recovery,
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

    Timestamps are compared as INSTANTS, never as strings. ISO8601 text is not
    ordered the same way as the moments it denotes: ``2026-08-30T10:00:00+05:30``
    sorts after ``2026-08-30T04:30:00+00:00`` as text while being the very same
    instant, so a string comparison could discard the decision that actually
    drove an execution, or admit one made after it.

    A decision whose stored timestamp cannot be parsed as a timezone-aware
    instant is SKIPPED rather than trusted or guessed at, and an execution with
    an unusable timestamp joins to nothing. Fail-closed: an unreadable
    timestamp produces no prediction, never a wrong one.

    Ties on the same instant are broken by the raw ``decided_at`` text, which
    is a total, stable order over the persisted rows.
    """
    execution_instant = _instant(executed_at)
    if execution_instant is None:
        return None
    candidates = []
    for decision in optimizer_decisions:
        if decision.get("selected_intervention") != intervention:
            continue
        decided_instant = _instant(decision.get("decided_at"))
        if decided_instant is None or decided_instant > execution_instant:
            continue
        candidates.append((decided_instant, str(decision.get("decided_at")), decision))
    if not candidates:
        return None
    return max(candidates, key=lambda item: (item[0], item[1]))[2]


def _instant(value: Any) -> datetime | None:
    """Parse a persisted timestamp into a UTC instant, or None if unusable."""
    try:
        return parse_aware_datetime(value)
    except PolicyValidationError:
        return None


def _evaluation_for(
    decision: Mapping[str, Any], intervention: str
) -> Mapping[str, Any] | None:
    """The persisted per-candidate economics for the selected intervention."""
    for evaluation in decision.get("evaluations") or ():
        if evaluation.get("intervention") == intervention:
            return evaluation
    return None


def _non_terminal(
    *,
    event: Mapping[str, Any],
    execution: Mapping[str, Any],
    prediction: Mapping[str, Any] | None,
    evaluation: Mapping[str, Any] | None,
    outcome: str,
    reason: str,
    note: str,
) -> FeedbackObservation:
    """An observation whose payment question is not settled either way.

    Only PENDING and UNKNOWN reach here, and both carry ``recovered=None``: an
    unsettled outcome contributes to no numerator and no denominator.
    """
    return _observation(
        event=event,
        execution=execution,
        prediction=prediction,
        evaluation=evaluation,
        outcome=outcome,
        reason=reason,
        recovered=None,
        verified_recovery=False,
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
    reason: str,
    recovered: bool | None,
    verified_recovery: bool,
    recovered_amount_paise: int | None,
    observed_at: str | None,
    evidence_id: str | None,
    note: str,
) -> FeedbackObservation:
    # Derived here, never passed in: a caller cannot mark an unsettled outcome
    # as a calibration sample, which is exactly the mistake this projection
    # must be structurally unable to make.
    terminal = outcome in TERMINAL_OUTCOMES and recovered is not None
    calibration_eligible = terminal and evaluation is not None
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
        terminal=terminal,
        calibration_eligible=calibration_eligible,
        verified_recovery=verified_recovery,
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
        return _non_terminal(
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
            return _non_terminal(
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
        return _non_terminal(
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
        return _non_terminal(
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
        return _non_terminal(
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
        # The recovery is real and is still counted as verified evidence, but
        # there is no persisted prediction to judge it against. Calibration
        # needs both halves, so it is excluded from the rate rather than
        # paired with a recomputed (and therefore different) estimate.
        return _observation(
            event=event,
            execution=execution,
            prediction=prediction,
            evaluation=evaluation,
            outcome=OUTCOME_RECOVERED,
            reason=REASON_MISSING_PREDICTION,
            recovered=True,
            verified_recovery=True,
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
        reason=REASON_CALIBRATION_ELIGIBLE,
        recovered=True,
        verified_recovery=True,
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
    observation paired with the decision that drove it. Ordering is by the
    actual instant, then by the raw timestamp text and the intervention, which
    is total — so repeated runs agree exactly even across mixed UTC offsets.
    An unparseable timestamp sorts last rather than crashing the projection.
    """
    ordered = sorted(
        executions,
        key=lambda row: (
            _instant(row.get("reported_at")) or _UNORDERABLE_INSTANT,
            str(row.get("reported_at") or ""),
            str(row.get("intervention") or ""),
        ),
    )
    return [
        build_observation(event, execution, optimizer_decisions, recoveries)
        for execution in ordered
    ]


@dataclass(frozen=True)
class ObservationPopulation:
    """The observations, plus an honest statement of what they cover.

    ``complete`` is the property that matters: it is True only when every
    execution RecoveryOS has ever recorded was projected. When it is False the
    figures describe a deterministic prefix of history and must not be
    presented as overall performance.
    """

    observations: tuple[FeedbackObservation, ...]
    total_executions: int
    projected_executions: int
    limit: int

    @property
    def complete(self) -> bool:
        return self.projected_executions >= self.total_executions

    def to_dict(self) -> dict[str, Any]:
        return {
            "total_executions": self.total_executions,
            "projected_executions": self.projected_executions,
            "limit": self.limit,
            "complete": self.complete,
        }


def build_observation_population(
    conn, *, limit: int = DEFAULT_OBSERVATION_LIMIT
) -> ObservationPopulation:
    """Project persisted executions into observations, stating the coverage.

    Driven by the EXECUTION table rather than by a window of recent events, so
    an old execution whose event has since fallen outside any recency window is
    still measured. Reads only; writes nothing.
    """
    total_executions = db.count_execution_outcomes(conn)
    execution_rows = db.list_execution_outcomes(conn, limit=limit)
    event_ids = sorted({str(row["event_id"]) for row in execution_rows})
    events = db.get_payment_events_for_events(conn, event_ids)
    optimizer_decisions = db.get_optimizer_decisions_for_events(conn, event_ids)
    link_ids = sorted(
        {
            row["payment_link_id"]
            for row in execution_rows
            if row.get("payment_link_id")
        }
    )
    recoveries = db.get_webhook_recovery_outcomes_for_payment_links(conn, link_ids)

    by_event: dict[str, list[Mapping[str, Any]]] = {}
    for row in execution_rows:
        by_event.setdefault(str(row["event_id"]), []).append(row)

    observations: list[FeedbackObservation] = []
    for event_id in event_ids:
        event = events.get(event_id)
        if event is None:
            # An execution whose event is not persisted cannot be described
            # (no amount, no segment). Skipping it is honest; inventing an
            # event for it would not be.
            continue
        observations.extend(
            build_observations_for_event(
                event,
                by_event[event_id],
                optimizer_decisions.get(event_id, []),
                recoveries,
            )
        )
    return ObservationPopulation(
        observations=tuple(observations),
        total_executions=total_executions,
        projected_executions=len(execution_rows),
        limit=limit,
    )


def build_observations(
    conn, *, limit: int = DEFAULT_OBSERVATION_LIMIT
) -> list[FeedbackObservation]:
    """Assemble every feedback observation from persisted state."""
    return list(build_observation_population(conn, limit=limit).observations)


def calibration_observations(
    observations: Sequence[FeedbackObservation],
) -> list[FeedbackObservation]:
    """Filter to the terminal, predicted observations a rate may be built from.

    This is the ONLY population a recovery rate may be computed over. It is
    deliberately not "the verified recoveries": a denominator made of positive
    evidence alone is 100% by construction.
    """
    return [
        observation for observation in observations if observation.calibration_eligible
    ]


def verified_recoveries(
    observations: Sequence[FeedbackObservation],
) -> list[FeedbackObservation]:
    """Filter to authoritative positive provider evidence.

    Reported on its own terms, independently of whether it can be calibrated:
    a verified recovery with no persisted prediction is still a real recovery.
    """
    return [observation for observation in observations if observation.verified_recovery]


def ineligibility_counts(
    observations: Sequence[FeedbackObservation],
) -> dict[str, int]:
    """Count why observations are not calibration samples (all keys present)."""
    counts = {reason: 0 for reason in INELIGIBILITY_REASONS}
    for observation in observations:
        if observation.calibration_eligible:
            continue
        counts[observation.reason] = counts.get(observation.reason, 0) + 1
    return counts


def outcome_counts(observations: Sequence[FeedbackObservation]) -> dict[str, int]:
    """Count observations per observed outcome (every outcome key is present)."""
    counts = {outcome: 0 for outcome in OUTCOMES}
    for observation in observations:
        counts[observation.outcome] = counts.get(observation.outcome, 0) + 1
    return counts

"""Recovery Operations projection (Phase 21) — a READ MODEL, not a state machine.

The Recovery Operations Center answers one operational question: which failed
payments need attention, what does RecoveryOS recommend for them, did policy
allow it, what was selected, was it executed, and did the money actually come
back?

Every value on a queue row is derived from records the EXISTING decision path
already persists:

    payment_events          the failed payment
    classification_results  the advisory AI diagnosis (Phase 5)
    policy_decisions        the authoritative deterministic gate (Phase 6)
    optimizer_decisions     the economic selection audit (Phase 18)
    execution_outcomes      the bounded execution (Phase 7/11)
    webhook_recovery_outcomes  VERIFIED recovery from a paid link (Phase 12)

There is no recovery_queue table and no second lifecycle store: a queue row is
a pure function of those persisted facts, so it can never drift from, or
disagree with, the authoritative records.

Two rules govern the derived states:

1. Execution is not recovery. A REAL_RAZORPAY Payment Link that was created
   successfully is PENDING_OUTCOME — it becomes RECOVERED only when the Phase
   12 webhook path has persisted a verified, correlated recovery for that
   exact payment_link_id.
2. Simulated is not real. A SIMULATED intervention can reach EXECUTED and
   stops there. It never produces a recovered amount, because no provider
   observed any money.
3. Failed is not always retryable. When a real Payment Link attempt ended
   without a result RecoveryOS could interpret, the row reports the outcome as
   PROVIDER_RESULT_UNKNOWN and is not offered for execution again: the link may
   exist, and a retry could create a second real one.

Nothing here evaluates policy, ranks candidates, executes, or fabricates a
missing value: an absent record is reported as absent.
"""

from __future__ import annotations

from typing import Any, Iterable, Mapping, Sequence

from . import db
from .dashboard import rule_label
from .razorpay_client import marks_provider_result_unknown
from .selector import NO_ACTION

# Derived decision states, in pipeline order.
STATE_NOT_CLASSIFIED = "NOT_CLASSIFIED"
STATE_RECOMMENDED = "RECOMMENDED"
STATE_POLICY_ALLOWED = "POLICY_ALLOWED"
STATE_SELECTED = "SELECTED"
STATE_BLOCKED = "BLOCKED"
# Derived execution/outcome states.
STATE_EXECUTED = "EXECUTED"
STATE_PENDING_OUTCOME = "PENDING_OUTCOME"
STATE_RECOVERED = "RECOVERED"
STATE_FAILED = "FAILED"
# An outcome state only, never a lifecycle state: the row still reads FAILED
# because no recovery exists, but the execution cannot be attempted again.
STATE_PROVIDER_RESULT_UNKNOWN = "PROVIDER_RESULT_UNKNOWN"

LIFECYCLE_STATES: tuple[str, ...] = (
    STATE_NOT_CLASSIFIED,
    STATE_RECOMMENDED,
    STATE_POLICY_ALLOWED,
    STATE_SELECTED,
    STATE_BLOCKED,
    STATE_EXECUTED,
    STATE_PENDING_OUTCOME,
    STATE_RECOVERED,
    STATE_FAILED,
)

# Policy status as the operator reads it: the gate either authorized at least
# one candidate, refused every candidate, or has not run for this event yet.
POLICY_ALLOWED = "ALLOWED"
POLICY_BLOCKED = "BLOCKED"
POLICY_NOT_EVALUATED = "NOT_EVALUATED"

EXECUTION_NOT_EXECUTED = "NOT_EXECUTED"

SORT_NEWEST = "newest"
SORT_AMOUNT_DESC = "amount_desc"
SORT_EXPECTED_RECOVERY_DESC = "expected_recovery_desc"
SORT_OLDEST_PENDING_OUTCOME = "oldest_pending_outcome"
SORT_ORDERS: tuple[str, ...] = (
    SORT_NEWEST,
    SORT_AMOUNT_DESC,
    SORT_EXPECTED_RECOVERY_DESC,
    SORT_OLDEST_PENDING_OUTCOME,
)

DEFAULT_LIMIT = 50
MAX_LIMIT = 200
DEFAULT_SCAN_LIMIT = 500


class RecoveryQueueError(Exception):
    """A queue request could not be honoured as asked (fail-closed)."""


def _latest(records: Sequence[Mapping[str, Any]], key: str) -> Mapping[str, Any] | None:
    """Return the record with the greatest ``key``, or None for an empty set."""
    if not records:
        return None
    return max(records, key=lambda record: str(record.get(key) or ""))


def _policy_view(decisions: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """Summarize the authoritative policy decisions for one event.

    Only the decisions from the most recent evaluation are summarized, because
    an earlier evaluation describes a state of the world that has since moved
    on (history remains fully visible in the Event Decision Trace).
    """
    if not decisions:
        return {
            "status": POLICY_NOT_EVALUATED,
            "allowed_interventions": [],
            "denied_interventions": [],
            "denial_reason": None,
            "denial_rule_label": None,
            "evaluated_at": None,
        }
    latest_at = max(str(decision["evaluated_at"]) for decision in decisions)
    current = [d for d in decisions if str(d["evaluated_at"]) == latest_at]
    allowed = sorted(
        d["proposed_intervention"] for d in current if d["allowed"]
    )
    denied = sorted(
        d["proposed_intervention"] for d in current if not d["allowed"]
    )
    # The denial reason shown is the one the deterministic gate produced; when
    # several candidates were denied for different reasons the first in the
    # engine's own evaluation output is used, and the full set stays in the
    # trace. A blocked row always carries a reason — never an empty "blocked".
    denial_reason = next(
        (d["denial_reason"] for d in current if not d["allowed"]), None
    )
    return {
        "status": POLICY_ALLOWED if allowed else POLICY_BLOCKED,
        "allowed_interventions": allowed,
        "denied_interventions": denied,
        "denial_reason": denial_reason if not allowed else None,
        "denial_rule_label": rule_label(denial_reason) if not allowed else None,
        "evaluated_at": latest_at,
    }


def _selection_view(
    optimizer_decisions: Sequence[Mapping[str, Any]],
) -> dict[str, Any] | None:
    """Summarize the economic selection, copying the optimizer's own figures."""
    latest = _latest(optimizer_decisions, "decided_at")
    if latest is None:
        return None
    selected = latest["selected_intervention"]
    expected_value_paise = None
    for evaluation in latest.get("evaluations") or ():
        if evaluation.get("intervention") == selected:
            expected_value_paise = evaluation.get("expected_value_paise")
            break
    return {
        "selected_intervention": selected,
        "selection_reason": latest.get("selection_reason"),
        # The optimizer's own estimate for the candidate it chose. It is a
        # MODEL ESTIMATE, never a realized or benchmark figure, and it is
        # absent (None) for a no_action decision that evaluated nothing.
        "expected_value_paise": expected_value_paise,
        "decided_at": latest.get("decided_at"),
    }


def _execution_view(
    executions: Sequence[Mapping[str, Any]],
) -> dict[str, Any] | None:
    """Summarize the most recent bounded execution for one event."""
    latest = _latest(executions, "reported_at")
    if latest is None:
        return None
    return {
        "intervention": latest["intervention"],
        "execution_mode": latest["execution_mode"],
        "status": latest["status"],
        "payment_link_id": latest.get("payment_link_id"),
        "external_reference": latest.get("external_reference"),
        "detail": latest.get("detail"),
        "reported_at": latest["reported_at"],
    }


def _outcome_view(
    execution: Mapping[str, Any] | None,
    recoveries: Mapping[str, Mapping[str, Any]],
) -> dict[str, Any]:
    """Derive the honest payment outcome for the most recent execution.

    A verified recovery exists only for a real Payment Link the Phase 12
    webhook path correlated as paid. Everything else is reported as what it
    actually is: waiting, failed, not executed, or simulated with no provider
    outcome to observe at all.
    """
    empty = {
        "state": EXECUTION_NOT_EXECUTED,
        "recovered_amount_paise": None,
        "recovered_at": None,
        "payment_id": None,
        "note": "no execution is recorded for this payment",
    }
    if execution is None:
        return empty
    if execution["status"] != "SUCCESS":
        if execution["execution_mode"] == "REAL_RAZORPAY" and (
            marks_provider_result_unknown(execution.get("detail"))
        ):
            return {
                "state": STATE_PROVIDER_RESULT_UNKNOWN,
                "recovered_amount_paise": None,
                "recovered_at": None,
                "payment_id": None,
                "note": (
                    "the provider was called and did not return a result "
                    "RecoveryOS could interpret; a real Payment Link may exist, "
                    "so this action is not attempted again and no recovery is "
                    "claimed"
                ),
            }
        return {
            "state": STATE_FAILED,
            "recovered_amount_paise": None,
            "recovered_at": None,
            "payment_id": None,
            "note": "the execution attempt itself failed; no payment was requested",
        }
    if execution["execution_mode"] != "REAL_RAZORPAY":
        return {
            "state": STATE_EXECUTED,
            "recovered_amount_paise": None,
            "recovered_at": None,
            "payment_id": None,
            "note": (
                "SIMULATED intervention: no provider was contacted, so no "
                "payment outcome exists and no revenue is claimed"
            ),
        }
    payment_link_id = execution.get("payment_link_id")
    recovery = recoveries.get(payment_link_id) if payment_link_id else None
    if recovery is None:
        return {
            "state": STATE_PENDING_OUTCOME,
            "recovered_amount_paise": None,
            "recovered_at": None,
            "payment_id": None,
            "note": (
                "a real Razorpay Test Mode Payment Link exists and is waiting "
                "for payment; recovery is confirmed only by a verified webhook"
            ),
        }
    return {
        "state": STATE_RECOVERED,
        # The TRUSTED amount the provider reported as paid on the link, copied
        # from the verified recovery record — never the original event amount.
        "recovered_amount_paise": recovery.get("amount_paid_paise"),
        "recovered_at": recovery.get("recovered_at"),
        "payment_id": recovery.get("payment_id"),
        "note": "verified by a signed Razorpay webhook correlated to this link",
    }


def _lifecycle_state(
    classification: Mapping[str, Any] | None,
    policy: Mapping[str, Any],
    selection: Mapping[str, Any] | None,
    execution: Mapping[str, Any] | None,
    outcome: Mapping[str, Any],
) -> str:
    """Collapse the persisted evidence into one operational state.

    Strongest evidence wins: what actually happened outranks what was decided,
    and what was decided outranks what was merely recommended.
    """
    if execution is not None:
        if outcome["state"] == STATE_RECOVERED:
            return STATE_RECOVERED
        if outcome["state"] == STATE_PENDING_OUTCOME:
            return STATE_PENDING_OUTCOME
        if execution["status"] == "SUCCESS":
            return STATE_EXECUTED
        return STATE_FAILED
    if classification is None:
        return STATE_NOT_CLASSIFIED
    if policy["status"] == POLICY_BLOCKED:
        return STATE_BLOCKED
    if policy["status"] == POLICY_NOT_EVALUATED:
        return STATE_RECOMMENDED
    if selection is not None and selection["selected_intervention"] != NO_ACTION:
        return STATE_SELECTED
    return STATE_POLICY_ALLOWED


def build_queue_row(
    event: Mapping[str, Any],
    classification: Mapping[str, Any] | None,
    policy_decisions: Sequence[Mapping[str, Any]],
    optimizer_decisions: Sequence[Mapping[str, Any]],
    executions: Sequence[Mapping[str, Any]],
    recoveries: Mapping[str, Mapping[str, Any]],
) -> dict[str, Any]:
    """Project one payment event and its persisted evidence into a queue row.

    Pure: the same records always produce the same row, so the projection can
    be tested without a database and can never introduce hidden state.
    """
    policy = _policy_view(policy_decisions)
    selection = _selection_view(optimizer_decisions)
    execution = _execution_view(executions)
    outcome = _outcome_view(execution, recoveries)
    lifecycle_state = _lifecycle_state(
        classification, policy, selection, execution, outcome
    )
    diagnosis = None
    if classification is not None:
        diagnosis = {
            "root_cause_category": classification.get("root_cause_category"),
            "confidence": classification.get("confidence"),
            "reasoning": classification.get("reasoning"),
            "candidate_interventions": list(
                classification.get("candidate_interventions") or ()
            ),
        }
    return {
        "event_id": event["event_id"],
        "customer_id": event.get("customer_id"),
        "order_id": event.get("order_id"),
        "amount_paise": event.get("amount_paise"),
        "currency": event.get("currency"),
        "payment_method": event.get("payment_method"),
        "bank": event.get("bank"),
        "failure_reason": event.get("failure_reason"),
        "risk_flag": event.get("risk_flag"),
        "event_timestamp": event.get("timestamp"),
        "diagnosis": diagnosis,
        "policy": policy,
        "selection": selection,
        "execution": execution,
        "outcome": outcome,
        "lifecycle_state": lifecycle_state,
        # An operator can only act where the authoritative state leaves room to
        # act. This is a UI affordance derived from persisted evidence — it is
        # NOT an authorization: the server re-derives policy on every execute.
        # A failed attempt whose provider result is unknown is deliberately NOT
        # actionable: the durable claim would refuse a second execution anyway,
        # and offering the operator a button that cannot fire would be a lie.
        "actionable": lifecycle_state
        in (STATE_RECOMMENDED, STATE_POLICY_ALLOWED, STATE_SELECTED, STATE_FAILED)
        and outcome["state"] != STATE_PROVIDER_RESULT_UNKNOWN,
    }


def _matches(
    row: Mapping[str, Any],
    *,
    lifecycle_state: str | None,
    execution_mode: str | None,
    intervention: str | None,
    policy_status: str | None,
) -> bool:
    """Apply the derived-state filters that no SQL column can express."""
    if lifecycle_state and row["lifecycle_state"] != lifecycle_state:
        return False
    if execution_mode:
        execution = row["execution"]
        if execution is None or execution["execution_mode"] != execution_mode:
            return False
    if intervention:
        selected = (row["selection"] or {}).get("selected_intervention")
        executed = (row["execution"] or {}).get("intervention")
        if intervention not in (selected, executed):
            return False
    if policy_status and row["policy"]["status"] != policy_status:
        return False
    return True


def _sorted(rows: list[dict[str, Any]], order: str) -> list[dict[str, Any]]:
    """Order rows deterministically; event_id always breaks ties."""
    if order == SORT_AMOUNT_DESC:
        return sorted(
            rows, key=lambda row: (-int(row["amount_paise"] or 0), row["event_id"])
        )
    if order == SORT_EXPECTED_RECOVERY_DESC:
        return sorted(
            rows,
            key=lambda row: (
                -int((row["selection"] or {}).get("expected_value_paise") or 0),
                row["event_id"],
            ),
        )
    if order == SORT_OLDEST_PENDING_OUTCOME:
        # Pending real Payment Links first, oldest execution first: these are
        # the rows where money is genuinely in flight and waiting.
        return sorted(
            rows,
            key=lambda row: (
                0 if row["lifecycle_state"] == STATE_PENDING_OUTCOME else 1,
                str((row["execution"] or {}).get("reported_at") or ""),
                row["event_id"],
            ),
        )
    return sorted(
        rows,
        key=lambda row: (str(row["event_timestamp"] or ""), row["event_id"]),
        reverse=True,
    )


def build_recovery_queue(
    conn,
    *,
    lifecycle_state: str | None = None,
    execution_mode: str | None = None,
    risk_flag: str | None = None,
    failure_reason: str | None = None,
    intervention: str | None = None,
    policy_status: str | None = None,
    sort: str = SORT_NEWEST,
    limit: int = DEFAULT_LIMIT,
    scan_limit: int = DEFAULT_SCAN_LIMIT,
) -> dict[str, Any]:
    """Assemble the Recovery Operations queue from persisted state.

    Event-level filters are pushed into SQL; derived-state filters are applied
    to the projection, because a derived state is not a stored column. Sorting
    is total (event_id breaks every tie), so the same data always produces the
    same order.
    """
    if lifecycle_state is not None and lifecycle_state not in LIFECYCLE_STATES:
        raise RecoveryQueueError(
            f"lifecycle_state must be one of {list(LIFECYCLE_STATES)}"
        )
    if sort not in SORT_ORDERS:
        raise RecoveryQueueError(f"sort must be one of {list(SORT_ORDERS)}")
    limit = max(1, min(int(limit), MAX_LIMIT))

    events = db.list_events_for_recovery_queue(
        conn,
        risk_flag=risk_flag,
        failure_reason=failure_reason,
        scan_limit=scan_limit,
    )
    event_ids = [event["event_id"] for event in events]
    classifications = db.get_classification_results_for_events(conn, event_ids)
    policy_decisions = db.get_policy_decisions_for_events(conn, event_ids)
    optimizer_decisions = db.get_optimizer_decisions_for_events(conn, event_ids)
    executions = db.get_execution_outcomes_for_events(conn, event_ids)
    link_ids = sorted(
        {
            row["payment_link_id"]
            for rows in executions.values()
            for row in rows
            if row.get("payment_link_id")
        }
    )
    recoveries = db.get_webhook_recovery_outcomes_for_payment_links(conn, link_ids)

    rows = [
        build_queue_row(
            event,
            classifications.get(event["event_id"]),
            policy_decisions.get(event["event_id"], []),
            optimizer_decisions.get(event["event_id"], []),
            executions.get(event["event_id"], []),
            recoveries,
        )
        for event in events
    ]
    matched = [
        row
        for row in rows
        if _matches(
            row,
            lifecycle_state=lifecycle_state,
            execution_mode=execution_mode,
            intervention=intervention,
            policy_status=policy_status,
        )
    ]
    matched = _sorted(matched, sort)
    return {
        "count": len(matched[:limit]),
        "total_matched": len(matched),
        "scanned": len(rows),
        "scan_limit": scan_limit,
        "truncated_scan": len(rows) >= scan_limit,
        "state_counts": state_counts(rows),
        "filters": {
            "lifecycle_state": lifecycle_state,
            "execution_mode": execution_mode,
            "risk_flag": risk_flag,
            "failure_reason": failure_reason,
            "intervention": intervention,
            "policy_status": policy_status,
        },
        "sort": sort,
        "rows": matched[:limit],
    }


def state_counts(rows: Iterable[Mapping[str, Any]]) -> dict[str, int]:
    """Count scanned rows per lifecycle state (every state key is present)."""
    counts = {state: 0 for state in LIFECYCLE_STATES}
    for row in rows:
        counts[row["lifecycle_state"]] = counts.get(row["lifecycle_state"], 0) + 1
    return counts


def build_queue_row_for_event(conn, event_id: str) -> dict[str, Any] | None:
    """Project a single event, or None when the event does not exist.

    Used after an operator execution so the caller sees the authoritative new
    state of the row rather than an optimistic client-side guess.
    """
    event = db.get_payment_event(conn, event_id)
    if event is None:
        return None
    executions = db.get_execution_outcomes_for_event(conn, event_id)
    link_ids = sorted(
        {row["payment_link_id"] for row in executions if row.get("payment_link_id")}
    )
    classification = db.get_classification_result(conn, event_id)
    return build_queue_row(
        event.to_dict(),
        classification.to_dict() if classification is not None else None,
        db.get_policy_decisions_for_event(conn, event_id),
        db.get_optimizer_decisions_for_event(conn, event_id),
        executions,
        db.get_webhook_recovery_outcomes_for_payment_links(conn, link_ids),
    )

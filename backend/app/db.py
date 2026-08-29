"""RecoveryOS SQLite persistence layer.

Phase 2/3: initializes the payment_events table and provides the single
persistence boundary between the domain model and the application/service layer.
Phase 5: adds the classification_results table for advisory AI classifications,
correlated with payment_events by event_id. Phase 6: adds policy_decisions and
intervention_attempts so the deterministic policy gate derives every historical
fact from persisted state. Phase 7: adds execution_outcomes so every bounded
execution (simulated or REAL_RAZORPAY) is recorded and correlated with the
event. No raw SQL lives outside this module. Persistence stores facts; it
never makes business decisions.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Any

from .classification import ClassificationResult
from .config import get_database_path
from .models import PaymentEvent
from .executor import ExecutionOutcome
from .optimizer_audit import OptimizerDecisionRecord
from .policy import (
    InterventionAttempt,
    PolicyDecision,
    PolicyHistory,
    parse_aware_datetime,
)

_PAYMENT_EVENTS_DDL = """
CREATE TABLE IF NOT EXISTS payment_events (
    event_id        TEXT PRIMARY KEY,
    order_id        TEXT NOT NULL,
    payment_id      TEXT NOT NULL,
    customer_id     TEXT NOT NULL,
    amount_paise    INTEGER NOT NULL,
    currency        TEXT NOT NULL,
    payment_method  TEXT NOT NULL,
    failure_reason  TEXT NOT NULL,
    bank            TEXT NOT NULL,
    risk_flag       TEXT NOT NULL,
    customer_history TEXT NOT NULL,
    timestamp       TEXT NOT NULL
)
"""

_CLASSIFICATION_RESULTS_DDL = """
CREATE TABLE IF NOT EXISTS classification_results (
    event_id                TEXT PRIMARY KEY,
    root_cause_category     TEXT NOT NULL,
    confidence              REAL NOT NULL,
    reasoning               TEXT NOT NULL,
    candidate_interventions TEXT NOT NULL
)
"""

_POLICY_DECISIONS_DDL = """
CREATE TABLE IF NOT EXISTS policy_decisions (
    event_id                TEXT NOT NULL,
    proposed_intervention   TEXT NOT NULL,
    allowed                 INTEGER NOT NULL,
    denial_reason           TEXT,
    policy_rules_applied    TEXT NOT NULL,
    evaluated_at            TEXT NOT NULL,
    PRIMARY KEY (event_id, proposed_intervention, evaluated_at)
)
"""

_INTERVENTION_ATTEMPTS_DDL = """
CREATE TABLE IF NOT EXISTS intervention_attempts (
    event_id     TEXT NOT NULL,
    intervention TEXT NOT NULL,
    customer_id  TEXT NOT NULL,
    cost_paise   INTEGER NOT NULL,
    attempted_at TEXT NOT NULL,
    status       TEXT NOT NULL,
    PRIMARY KEY (event_id, intervention, attempted_at)
)
"""

_EXECUTION_OUTCOMES_DDL = """
CREATE TABLE IF NOT EXISTS execution_outcomes (
    event_id           TEXT NOT NULL,
    intervention       TEXT NOT NULL,
    execution_mode     TEXT NOT NULL,
    status             TEXT NOT NULL,
    external_reference TEXT,
    detail             TEXT,
    payment_link_id    TEXT,
    reported_at        TEXT NOT NULL,
    PRIMARY KEY (event_id, intervention, reported_at)
)
"""

# Phase 10: a read-only store for the latest persisted benchmark run summary so
# the Command Center can display real backend benchmark data. This table holds
# only the compact, already-computed summary (strategy results); it never
# stores hidden outcome probabilities or evaluation internals.
_BENCHMARK_RUNS_DDL = """
CREATE TABLE IF NOT EXISTS benchmark_runs (
    run_id           TEXT PRIMARY KEY,
    seed             INTEGER NOT NULL,
    event_count      INTEGER NOT NULL,
    model_seed       INTEGER NOT NULL,
    evaluation_time  TEXT NOT NULL,
    evaluation_mode  TEXT NOT NULL,
    saved_at         TEXT NOT NULL,
    summary_json     TEXT NOT NULL
)
"""

# Phase 12: a durable idempotency + audit store for verified Razorpay webhook
# deliveries. delivery_id is the canonical idempotency key (X-Razorpay-Event-Id)
# with a DB-level PRIMARY KEY so duplicates are rejected by SQLite, not by
# in-memory state. body_sha256 of the exact raw body enables explicit CONFLICT
# detection (same delivery id, different body). status advances through the
# closed-loop processing (claimed -> ignored/unmatched/processed). The webhook
# secret itself is never stored here.
_WEBHOOK_DELIVERIES_DDL = """
CREATE TABLE IF NOT EXISTS webhook_deliveries (
    delivery_id     TEXT PRIMARY KEY,
    body_sha256     TEXT NOT NULL,
    event_type      TEXT NOT NULL,
    payment_link_id TEXT,
    status          TEXT NOT NULL,
    received_at     TEXT NOT NULL
)
"""

# Phase 12 (S4): the durable store of VERIFIED, correlated recovery outcomes.
# A row is written only after a verified payment_link.paid webhook has been
# correlated (by payment_link_id) to a REAL_RAZORPAY execution outcome that
# created that link. delivery_id (X-Razorpay-Event-Id) is the PRIMARY KEY, so
# SQLite enforces durable uniqueness: an event can never yield a second
# recovery. The recovery amount is the TRUSTED amount_paid observed on the
# link (never the original event amount); if the provider did not report an
# amount it is recorded as NULL rather than fabricated. This is an OUTCOME
# store only — it never drives execution.
_WEBHOOK_RECOVERY_OUTCOMES_DDL = """
CREATE TABLE IF NOT EXISTS webhook_recovery_outcomes (
    delivery_id         TEXT PRIMARY KEY,
    payment_link_id     TEXT NOT NULL,
    referenced_event_id TEXT NOT NULL,
    amount_paid_paise   INTEGER,
    currency            TEXT,
    payment_id          TEXT,
    recovered_at        TEXT NOT NULL
)
"""


# Phase 18: the append-only audit record of the V2 economic optimizer's
# decision for one event. The row stores exactly what the optimizer produced —
# the candidate sets, the per-candidate estimated economics, the selection and
# its reason — so the decision can be reconstructed after the fact. It is
# written BEFORE execution is attempted, so a failed execution still leaves
# evidence of what RecoveryOS decided. It holds no benchmark ground truth.
_OPTIMIZER_DECISIONS_DDL = """
CREATE TABLE IF NOT EXISTS optimizer_decisions (
    event_id              TEXT NOT NULL,
    decided_at            TEXT NOT NULL,
    selected_intervention TEXT NOT NULL,
    selection_reason      TEXT NOT NULL,
    candidates_considered TEXT NOT NULL,
    allowed_candidates    TEXT NOT NULL,
    evaluations           TEXT NOT NULL,
    PRIMARY KEY (event_id, decided_at)
)
"""


def connect(path: str) -> sqlite3.Connection:
    """Open a SQLite connection to the given database path.

    ``check_same_thread=False`` lets an async FastAPI route use the per-request
    connection it received from a sync dependency even though the handler runs
    on the event-loop thread. Every connection is created per request and
    closed after use (never shared across requests), so disabling the
    thread-affinity guard is safe here and standard for FastAPI + SQLite.
    """
    conn = sqlite3.connect(path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


def connect_database() -> sqlite3.Connection:
    """Open a SQLite connection to the configured database path."""
    return connect(get_database_path())


def init_db(conn: sqlite3.Connection) -> None:
    """Create the tables if they do not already exist."""
    try:
        conn.execute(_PAYMENT_EVENTS_DDL)
        conn.execute(_CLASSIFICATION_RESULTS_DDL)
        conn.execute(_POLICY_DECISIONS_DDL)
        conn.execute(_INTERVENTION_ATTEMPTS_DDL)
        conn.execute(_EXECUTION_OUTCOMES_DDL)
        conn.execute(_BENCHMARK_RUNS_DDL)
        conn.execute(_WEBHOOK_DELIVERIES_DDL)
        conn.execute(_WEBHOOK_RECOVERY_OUTCOMES_DDL)
        conn.execute(_OPTIMIZER_DECISIONS_DDL)
        _migrate_execution_outcomes_payment_link_id(conn)
        conn.commit()
    except sqlite3.Error:
        conn.rollback()
        raise


_PAYMENT_LINK_ID_COLUMN = "payment_link_id"


def _migrate_execution_outcomes_payment_link_id(conn: sqlite3.Connection) -> None:
    """Idempotently add the nullable payment_link_id column to existing DBs.

    A database created before Phase 11 has an execution_outcomes table without
    the payment_link_id column. CREATE TABLE IF NOT EXISTS does not alter an
    existing table, so this adds the column exactly once when it is missing.
    """
    if _PAYMENT_LINK_ID_COLUMN in _execution_outcome_columns(conn):
        return
    conn.execute(
        f"ALTER TABLE execution_outcomes ADD COLUMN {_PAYMENT_LINK_ID_COLUMN} TEXT"
    )


def _execution_outcome_columns(conn: sqlite3.Connection) -> set[str]:
    rows = conn.execute("PRAGMA table_info(execution_outcomes)").fetchall()
    return {row["name"] for row in rows}


def insert_payment_event(conn: sqlite3.Connection, event: PaymentEvent) -> None:
    """Persist a PaymentEvent. Duplicate event_id is rejected (IntegrityError)."""
    try:
        conn.execute(
            """
            INSERT INTO payment_events (
                event_id, order_id, payment_id, customer_id, amount_paise,
                currency, payment_method, failure_reason, bank, risk_flag,
                customer_history, timestamp
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                event.event_id,
                event.order_id,
                event.payment_id,
                event.customer_id,
                event.amount_paise,
                event.currency,
                event.payment_method,
                event.failure_reason,
                event.bank,
                event.risk_flag,
                json.dumps(event.customer_history.to_dict()),
                event.timestamp,
            ),
        )
        conn.commit()
    except sqlite3.Error:
        conn.rollback()
        raise


def get_payment_event(conn: sqlite3.Connection, event_id: str) -> PaymentEvent | None:
    """Retrieve a PaymentEvent by event_id, or None if it does not exist."""
    row = conn.execute(
        "SELECT * FROM payment_events WHERE event_id = ?", (event_id,)
    ).fetchone()
    if row is None:
        return None
    return _row_to_event(row)


def _row_to_event(row: sqlite3.Row) -> PaymentEvent:
    """Reconstruct a PaymentEvent from a stored row."""
    data: dict[str, Any] = dict(row)
    data["customer_history"] = json.loads(data["customer_history"])
    return PaymentEvent.from_dict(data)


def insert_classification_result(
    conn: sqlite3.Connection, result: ClassificationResult
) -> None:
    """Persist a ClassificationResult, correlated with payment_events by event_id.

    Duplicate event_id is rejected (IntegrityError).
    """
    try:
        conn.execute(
            """
            INSERT INTO classification_results (
                event_id, root_cause_category, confidence, reasoning,
                candidate_interventions
            ) VALUES (?, ?, ?, ?, ?)
            """,
            (
                result.event_id,
                result.root_cause_category,
                result.confidence,
                result.reasoning,
                json.dumps(list(result.candidate_interventions)),
            ),
        )
        conn.commit()
    except sqlite3.Error:
        conn.rollback()
        raise


def get_classification_result(
    conn: sqlite3.Connection, event_id: str
) -> ClassificationResult | None:
    """Retrieve a ClassificationResult by event_id, or None if it does not exist."""
    row = conn.execute(
        "SELECT * FROM classification_results WHERE event_id = ?", (event_id,)
    ).fetchone()
    if row is None:
        return None
    data: dict[str, Any] = dict(row)
    data["candidate_interventions"] = json.loads(data["candidate_interventions"])
    return ClassificationResult.from_dict(data)


def insert_policy_decision(
    conn: sqlite3.Connection, decision: PolicyDecision
) -> None:
    """Persist a PolicyDecision, preserving the decision contract.

    A logically identical decision (same event, same proposed intervention,
    same evaluation time) is rejected as a duplicate (IntegrityError).
    """
    try:
        conn.execute(
            """
            INSERT INTO policy_decisions (
                event_id, proposed_intervention, allowed, denial_reason,
                policy_rules_applied, evaluated_at
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                decision.event_id,
                decision.proposed_intervention,
                1 if decision.allowed else 0,
                decision.denial_reason,
                json.dumps(list(decision.policy_rules_applied)),
                decision.evaluated_at,
            ),
        )
        conn.commit()
    except sqlite3.Error:
        conn.rollback()
        raise


def get_policy_decision(
    conn: sqlite3.Connection,
    event_id: str,
    proposed_intervention: str,
    evaluated_at: str,
) -> PolicyDecision | None:
    """Retrieve a PolicyDecision, or None if it does not exist."""
    row = conn.execute(
        """
        SELECT * FROM policy_decisions
        WHERE event_id = ? AND proposed_intervention = ? AND evaluated_at = ?
        """,
        (event_id, proposed_intervention, evaluated_at),
    ).fetchone()
    if row is None:
        return None
    data: dict[str, Any] = dict(row)
    data["allowed"] = bool(data["allowed"])
    data["policy_rules_applied"] = json.loads(data["policy_rules_applied"])
    return PolicyDecision.from_dict(data)


def insert_intervention_attempt(
    conn: sqlite3.Connection, attempt: InterventionAttempt
) -> None:
    """Persist an InterventionAttempt (the executor's history record).

    Duplicate (event_id, intervention, attempted_at) is rejected
    (IntegrityError). Phase 6 never writes this; it exists so the policy
    history facts are derived from persisted state and so tests may exercise
    the rules against concrete history.
    """
    try:
        conn.execute(
            """
            INSERT INTO intervention_attempts (
                event_id, intervention, customer_id, cost_paise,
                attempted_at, status
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                attempt.event_id,
                attempt.intervention,
                attempt.customer_id,
                attempt.cost_paise,
                attempt.attempted_at,
                attempt.status,
            ),
        )
        conn.commit()
    except sqlite3.Error:
        conn.rollback()
        raise


def get_intervention_attempt(
    conn: sqlite3.Connection, event_id: str, intervention: str, attempted_at: str
) -> InterventionAttempt | None:
    """Retrieve an InterventionAttempt, or None if it does not exist."""
    row = conn.execute(
        """
        SELECT * FROM intervention_attempts
        WHERE event_id = ? AND intervention = ? AND attempted_at = ?
        """,
        (event_id, intervention, attempted_at),
    ).fetchone()
    if row is None:
        return None
    return InterventionAttempt.from_dict(dict(row))


def insert_execution_outcome(
    conn: sqlite3.Connection, outcome: ExecutionOutcome
) -> None:
    """Persist an ExecutionOutcome, preserving the outcome contract.

    A logically identical outcome (same event, same intervention, same
    reported time) is rejected as a duplicate (IntegrityError). Historical
    outcomes are never overwritten or mutated.
    """
    try:
        conn.execute(
            """
            INSERT INTO execution_outcomes (
                event_id, intervention, execution_mode, status,
                external_reference, detail, reported_at, payment_link_id
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                outcome.event_id,
                outcome.intervention,
                outcome.execution_mode,
                outcome.status,
                outcome.external_reference,
                outcome.detail,
                outcome.reported_at,
                outcome.payment_link_id,
            ),
        )
        conn.commit()
    except sqlite3.Error:
        conn.rollback()
        raise


def get_execution_outcome(
    conn: sqlite3.Connection, event_id: str, intervention: str, reported_at: str
) -> ExecutionOutcome | None:
    """Retrieve an ExecutionOutcome, or None if it does not exist."""
    row = conn.execute(
        """
        SELECT * FROM execution_outcomes
        WHERE event_id = ? AND intervention = ? AND reported_at = ?
        """,
        (event_id, intervention, reported_at),
    ).fetchone()
    if row is None:
        return None
    return ExecutionOutcome(
        event_id=row["event_id"],
        intervention=row["intervention"],
        execution_mode=row["execution_mode"],
        status=row["status"],
        external_reference=row["external_reference"],
        detail=row["detail"],
        reported_at=row["reported_at"],
        payment_link_id=row["payment_link_id"],
    )


def get_execution_outcome_by_payment_link_id(
    conn: sqlite3.Connection, payment_link_id: str
) -> ExecutionOutcome | None:
    """Return the most recent REAL_RAZORPAY payment_link SUCCESS outcome that
    created the given Payment Link id, or None.

    A Payment Link id is only ever persisted on a REAL_RAZORPAY ``payment_link``
    SUCCESS outcome, so matching on the actual Razorpay Payment Link id (never
    amount/customer/email/URL) unambiguously identifies the Phase 11 execution
    that produced that link. This is the webhook correlation key.
    """
    row = conn.execute(
        """
        SELECT * FROM execution_outcomes
        WHERE payment_link_id = ?
          AND execution_mode = 'REAL_RAZORPAY'
          AND status = 'SUCCESS'
          AND intervention = 'payment_link'
        ORDER BY reported_at DESC
        LIMIT 1
        """,
        (payment_link_id,),
    ).fetchone()
    if row is None:
        return None
    return ExecutionOutcome(
        event_id=row["event_id"],
        intervention=row["intervention"],
        execution_mode=row["execution_mode"],
        status=row["status"],
        external_reference=row["external_reference"],
        detail=row["detail"],
        reported_at=row["reported_at"],
        payment_link_id=row["payment_link_id"],
    )


def get_policy_history(
    conn: sqlite3.Connection, event: PaymentEvent, evaluation_time: datetime
) -> PolicyHistory:
    """Derive the four historical policy facts from persisted state.

    The rolling 24h window is computed with actual datetime arithmetic against
    the supplied evaluation time; timestamps are never compared as strings.
    Facts are never fabricated: a missing attempt record simply means no
    attempt exists. Fail-closed: a stored timestamp that cannot be parsed or
    is timezone-naive surfaces as an explicit policy validation failure.
    """
    if not isinstance(evaluation_time, datetime) or evaluation_time.tzinfo is None:
        from .policy import PolicyValidationError

        raise PolicyValidationError(
            "evaluation_time must be a timezone-aware datetime"
        )
    window_start = evaluation_time - timedelta(hours=24)

    rows = conn.execute(
        """
        SELECT event_id, intervention, customer_id, cost_paise,
               attempted_at, status
        FROM intervention_attempts
        """
    ).fetchall()

    customer_count_24h = 0
    existing_daily_spend_paise = 0
    for row in rows:
        attempt = InterventionAttempt.from_dict(dict(row))
        attempted_at = parse_aware_datetime(attempt.attempted_at)
        if window_start <= attempted_at <= evaluation_time:
            if attempt.customer_id == event.customer_id:
                customer_count_24h += 1
            existing_daily_spend_paise += attempt.cost_paise

    most_recent: datetime | None = None
    has_successful_intervention = False
    for row in rows:
        attempt = InterventionAttempt.from_dict(dict(row))
        if attempt.event_id != event.event_id:
            continue
        attempted_at = parse_aware_datetime(attempt.attempted_at)
        if most_recent is None or attempted_at > most_recent:
            most_recent = attempted_at
        if attempt.status == "successful":
            has_successful_intervention = True

    return PolicyHistory(
        customer_intervention_count_24h=customer_count_24h,
        most_recent_event_intervention_time=most_recent,
        has_successful_intervention=has_successful_intervention,
        existing_daily_spend_paise=existing_daily_spend_paise,
    )


# ---------------------------------------------------------------------------
# Phase 10 read-only queries (Command Center + Event Decision Trace).
# These functions only READ persisted state so the operator dashboard can
# render it; they never make, change, or fabricate a decision.
# ---------------------------------------------------------------------------


def list_payment_events(
    conn: sqlite3.Connection,
    *,
    limit: int = 50,
    query: str | None = None,
    risk_flag: str | None = None,
) -> list[dict[str, Any]]:
    """Return event summaries (newest first), optionally filtered.

    ``query`` matches a substring against event/customer/order/payment ids.
    ``risk_flag`` filters on the locked risk_flag value set. Limit caps the
    response; the returned records are the full PaymentEvent contract so the
    Command Center can render real persisted events.
    """
    sql = "SELECT * FROM payment_events WHERE 1 = 1"
    params: list[Any] = []
    if query:
        like = f"%{query}%"
        sql += (
            " AND (event_id LIKE ? OR customer_id LIKE ? OR "
            "order_id LIKE ? OR payment_id LIKE ?)"
        )
        params.extend([like, like, like, like])
    if risk_flag:
        sql += " AND risk_flag = ?"
        params.append(risk_flag)
    sql += " ORDER BY timestamp DESC LIMIT ?"
    params.append(int(limit))
    rows = conn.execute(sql, params).fetchall()
    return [_row_to_event(row).to_dict() for row in rows]


def count_payment_events(conn: sqlite3.Connection) -> int:
    """Count persisted payment events."""
    row = conn.execute(
        "SELECT COUNT(*) AS c FROM payment_events"
    ).fetchone()
    return int(row["c"])


def sum_event_amount_paise(conn: sqlite3.Connection) -> int:
    """Total amount (paise) across persisted payment events (Revenue at Risk)."""
    row = conn.execute(
        "SELECT COALESCE(SUM(amount_paise), 0) AS s FROM payment_events"
    ).fetchone()
    return int(row["s"])


def get_policy_decisions_for_event(
    conn: sqlite3.Connection, event_id: str
) -> list[dict[str, Any]]:
    """Return every persisted policy decision for one event, newest last."""
    rows = conn.execute(
        """
        SELECT * FROM policy_decisions
        WHERE event_id = ?
        ORDER BY evaluated_at ASC
        """,
        (event_id,),
    ).fetchall()
    out: list[dict[str, Any]] = []
    for row in rows:
        data = dict(row)
        data["allowed"] = bool(data["allowed"])
        data["policy_rules_applied"] = json.loads(data["policy_rules_applied"])
        out.append(data)
    return out


def insert_optimizer_decision(
    conn: sqlite3.Connection, record: OptimizerDecisionRecord
) -> None:
    """Persist one economic optimizer decision (Phase 18), append-only.

    A logically identical decision (same event, same decision time) is
    rejected as a duplicate (IntegrityError). Recorded decisions are never
    overwritten or mutated, and the stored figures are the optimizer's own
    output — this function computes nothing.
    """
    if not isinstance(record, OptimizerDecisionRecord):
        raise TypeError("record must be an OptimizerDecisionRecord")
    try:
        conn.execute(
            """
            INSERT INTO optimizer_decisions (
                event_id, decided_at, selected_intervention, selection_reason,
                candidates_considered, allowed_candidates, evaluations
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                record.event_id,
                record.decided_at,
                record.selected_intervention,
                record.selection_reason,
                json.dumps(list(record.candidates_considered)),
                json.dumps(list(record.allowed_candidates)),
                json.dumps(
                    [evaluation.to_dict() for evaluation in record.evaluations]
                ),
            ),
        )
        conn.commit()
    except sqlite3.Error:
        conn.rollback()
        raise


def _row_to_optimizer_decision(row: sqlite3.Row) -> dict[str, Any]:
    data: dict[str, Any] = dict(row)
    for field in ("candidates_considered", "allowed_candidates", "evaluations"):
        data[field] = json.loads(data[field])
    return data


def get_optimizer_decision(
    conn: sqlite3.Connection, event_id: str, decided_at: str
) -> OptimizerDecisionRecord | None:
    """Retrieve one persisted optimizer decision, or None if it does not exist."""
    row = conn.execute(
        """
        SELECT * FROM optimizer_decisions
        WHERE event_id = ? AND decided_at = ?
        """,
        (event_id, decided_at),
    ).fetchone()
    if row is None:
        return None
    return OptimizerDecisionRecord.from_dict(_row_to_optimizer_decision(row))


def get_optimizer_decisions_for_event(
    conn: sqlite3.Connection, event_id: str
) -> list[dict[str, Any]]:
    """Return every persisted optimizer decision for one event, newest last."""
    rows = conn.execute(
        """
        SELECT * FROM optimizer_decisions
        WHERE event_id = ?
        ORDER BY decided_at ASC
        """,
        (event_id,),
    ).fetchall()
    return [_row_to_optimizer_decision(row) for row in rows]


def get_execution_outcomes_for_event(
    conn: sqlite3.Connection, event_id: str
) -> list[dict[str, Any]]:
    """Return every persisted execution outcome for one event, newest last."""
    rows = conn.execute(
        """
        SELECT * FROM execution_outcomes
        WHERE event_id = ?
        ORDER BY reported_at ASC
        """,
        (event_id,),
    ).fetchall()
    return [dict(row) for row in rows]


def get_intervention_attempts_for_event(
    conn: sqlite3.Connection, event_id: str
) -> list[dict[str, Any]]:
    """Return every persisted intervention attempt for one event, newest last."""
    rows = conn.execute(
        """
        SELECT * FROM intervention_attempts
        WHERE event_id = ?
        ORDER BY attempted_at ASC
        """,
        (event_id,),
    ).fetchall()
    return [dict(row) for row in rows]


def get_intervention_attempt_summary(
    conn: sqlite3.Connection, event_ids: list[str]
) -> dict[str, dict[str, Any]]:
    """Batched persisted-intervention evidence for a set of events (no N+1).

    Returns a mapping event_id -> {previous_attempts (int), last_intervention
    (intervention | None), last_attempt_status (status | None),
    last_attempted_at (attempted_at | None)}. Only reads persisted rows; no
    policy or outcome logic is recomputed here.
    """
    if not event_ids:
        return {}
    placeholders = ",".join("?" * len(event_ids))
    rows = conn.execute(
        f"""
        SELECT
            event_id,
            COUNT(*) AS previous_attempts,
            MAX(attempted_at) AS last_attempted_at
        FROM intervention_attempts
        WHERE event_id IN ({placeholders})
        GROUP BY event_id
        """,
        list(event_ids),
    ).fetchall()
    summary: dict[str, dict[str, Any]] = {}
    for row in rows:
        summary[row["event_id"]] = {
            "previous_attempts": int(row["previous_attempts"]),
            "last_attempted_at": row["last_attempted_at"],
            "last_intervention": None,
            "last_attempt_status": None,
        }
    if summary:
        latest_rows = conn.execute(
            f"""
            SELECT event_id, intervention, status, attempted_at
            FROM intervention_attempts
            WHERE event_id IN ({placeholders})
            ORDER BY attempted_at DESC
            """,
            list(event_ids),
        ).fetchall()
        seen: set[str] = set()
        for r in latest_rows:
            if r["event_id"] in seen:
                continue
            seen.add(r["event_id"])
            summary[r["event_id"]]["last_intervention"] = r["intervention"]
            summary[r["event_id"]]["last_attempt_status"] = r["status"]
    return summary


def get_policy_decision_stats(conn: sqlite3.Connection) -> dict[str, int]:
    """Total and denied persisted policy decisions."""
    row = conn.execute(
        """
        SELECT COUNT(*) AS total,
               COALESCE(SUM(CASE WHEN allowed = 0 THEN 1 ELSE 0 END), 0) AS denied
        FROM policy_decisions
        """
    ).fetchone()
    return {"total": int(row["total"]), "denied": int(row["denied"])}


def get_execution_outcome_stats(conn: sqlite3.Connection) -> dict[str, int]:
    """Total and SUCCESS persisted execution outcomes."""
    row = conn.execute(
        """
        SELECT COUNT(*) AS total,
               COALESCE(SUM(CASE WHEN status = 'SUCCESS' THEN 1 ELSE 0 END), 0) AS success
        FROM execution_outcomes
        """
    ).fetchone()
    return {"total": int(row["total"]), "success": int(row["success"])}


def count_denied_on_fraud_events(conn: sqlite3.Connection) -> int:
    """Count denied policy decisions whose event is fraud_suspect.

    This is the honest basis for the 'Fraud Actions Blocked' card: each denied
    decision on a fraud event is a fraudulent action the policy gate refused.
    """
    row = conn.execute(
        """
        SELECT COUNT(*) AS c
        FROM policy_decisions pd
        JOIN payment_events pe ON pe.event_id = pd.event_id
        WHERE pd.allowed = 0 AND pe.risk_flag = 'fraud_suspect'
        """
    ).fetchone()
    return int(row["c"])


def get_policy_blocked_event_amounts(conn: sqlite3.Connection) -> dict[str, int]:
    """Aggregate events that policy blocked (>=1 denied decision).

    Returns {count, amount_paise} over distinct blocked events. An event whose
    every actionable candidate was denied is a real blocked action the operator
    can see; this is the honest fraction of Revenue at Risk that RecoveryOS did
    not act on because the safety gate refused it.
    """
    rows = conn.execute(
        """
        SELECT pe.event_id, pe.amount_paise
        FROM payment_events pe
        WHERE EXISTS (
            SELECT 1 FROM policy_decisions pd
            WHERE pd.event_id = pe.event_id AND pd.allowed = 0
        )
        """
    ).fetchall()
    return {
        "count": len(rows),
        "amount_paise": sum(int(row["amount_paise"]) for row in rows),
    }


def get_unclassified_event_amounts(conn: sqlite3.Connection) -> dict[str, int]:
    """Aggregate events with no persisted classification (never diagnosed).

    Returns {count, amount_paise}. These events received no AI diagnosis, so
    no intervention was attempted for them.
    """
    rows = conn.execute(
        """
        SELECT pe.event_id, pe.amount_paise
        FROM payment_events pe
        WHERE NOT EXISTS (
            SELECT 1 FROM classification_results c
            WHERE c.event_id = pe.event_id
        )
        """
    ).fetchall()
    return {
        "count": len(rows),
        "amount_paise": sum(int(row["amount_paise"]) for row in rows),
    }


def get_blocked_policy_decisions(
    conn: sqlite3.Connection, *, limit: int = 100
) -> list[dict[str, Any]]:
    """Return denied policy decisions joined with their event context.

    Used by the Policy & Blocked Actions screen; an operator can see the
    event, customer, amount, the rule that blocked, and the denial reason.
    """
    rows = conn.execute(
        """
        SELECT pd.event_id, pd.proposed_intervention, pd.allowed, pd.denial_reason,
               pd.policy_rules_applied, pd.evaluated_at,
               pe.customer_id, pe.amount_paise, pe.currency, pe.risk_flag,
               pe.failure_reason, pe.timestamp
        FROM policy_decisions pd
        LEFT JOIN payment_events pe ON pe.event_id = pd.event_id
        WHERE pd.allowed = 0
        ORDER BY pd.evaluated_at DESC
        LIMIT ?
        """,
        (int(limit),),
    ).fetchall()
    out: list[dict[str, Any]] = []
    for row in rows:
        data = dict(row)
        data["allowed"] = bool(data["allowed"])
        data["policy_rules_applied"] = json.loads(data["policy_rules_applied"])
        out.append(data)
    return out


# ---------------------------------------------------------------------------
# Phase 10 benchmark persistence (read/write of the latest run summary only).
# The summary is already computed by the frozen Phase 9 module; these functions
# only store and retrieve it so the dashboard can display real backend data.
# No benchmark algorithm or metric definition lives here.
# ---------------------------------------------------------------------------


def upsert_benchmark_run(
    conn: sqlite3.Connection,
    *,
    run_id: str,
    seed: int,
    event_count: int,
    model_seed: int,
    evaluation_time: str,
    evaluation_mode: str,
    summary_json: str,
) -> None:
    """Persist (or replace) a benchmark run summary, keyed by run_id."""
    conn.execute(
        """
        INSERT OR REPLACE INTO benchmark_runs (
            run_id, seed, event_count, model_seed, evaluation_time,
            evaluation_mode, saved_at, summary_json
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            run_id,
            int(seed),
            int(event_count),
            int(model_seed),
            evaluation_time,
            evaluation_mode,
            datetime.now(timezone.utc).isoformat(),
            summary_json,
        ),
    )
    conn.commit()


def get_latest_benchmark_run(conn: sqlite3.Connection) -> dict[str, Any] | None:
    """Return the most recently saved benchmark run summary, or None."""
    row = conn.execute(
        """
        SELECT run_id, seed, event_count, model_seed, evaluation_time,
               evaluation_mode, saved_at, summary_json
        FROM benchmark_runs
        ORDER BY saved_at DESC
        LIMIT 1
        """
    ).fetchone()
    if row is None:
        return None
    return {
        "run_id": row["run_id"],
        "seed": row["seed"],
        "event_count": row["event_count"],
        "model_seed": row["model_seed"],
        "evaluation_time": row["evaluation_time"],
        "evaluation_mode": row["evaluation_mode"],
        "saved_at": row["saved_at"],
        "summary": json.loads(row["summary_json"]),
    }


# ---------------------------------------------------------------------------
# Phase 12 webhook delivery persistence (durable idempotency + audit).
# delivery_id (X-Razorpay-Event-Id) is the canonical idempotency key; SQLite's
# PRIMARY KEY enforces durable uniqueness. body_sha256 of the exact raw body
# enables explicit CONFLICT detection for the same id with a different body.
# Persistence stores delivered/processed facts; it never performs recovery.
# ---------------------------------------------------------------------------


def insert_webhook_delivery(
    conn: sqlite3.Connection,
    *,
    delivery_id: str,
    body_sha256: str,
    event_type: str,
    payment_link_id: str | None,
    status: str,
    received_at: str,
) -> None:
    """Persist a webhook delivery claim.

    ``delivery_id`` is a PRIMARY KEY, so a second delivery with the same id
    raises sqlite3.IntegrityError (durable DB-level uniqueness, never
    in-memory state). Other sqlite3.Error values propagate as persistence
    failures for the caller to surface as an error HTTP (so Razorpay retries).
    """
    try:
        conn.execute(
            """
            INSERT INTO webhook_deliveries (
                delivery_id, body_sha256, event_type, payment_link_id,
                status, received_at
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                delivery_id,
                body_sha256,
                event_type,
                payment_link_id,
                status,
                received_at,
            ),
        )
        conn.commit()
    except sqlite3.Error:
        conn.rollback()
        raise


def get_webhook_delivery(
    conn: sqlite3.Connection, delivery_id: str
) -> dict[str, Any] | None:
    """Retrieve a persisted webhook delivery by its idempotency key, or None."""
    row = conn.execute(
        "SELECT * FROM webhook_deliveries WHERE delivery_id = ?",
        (delivery_id,),
    ).fetchone()
    if row is None:
        return None
    return dict(row)


def update_webhook_delivery_status(
    conn: sqlite3.Connection, delivery_id: str, status: str
) -> None:
    """Advance a webhook delivery's closed-loop status in place."""
    try:
        conn.execute(
            "UPDATE webhook_deliveries SET status = ? WHERE delivery_id = ?",
            (status, delivery_id),
        )
        conn.commit()
    except sqlite3.Error:
        conn.rollback()
        raise


def insert_webhook_recovery_outcome(
    conn: sqlite3.Connection,
    *,
    delivery_id: str,
    payment_link_id: str,
    referenced_event_id: str,
    amount_paid_paise: int | None,
    currency: str | None,
    payment_id: str | None,
    recovered_at: str,
) -> bool:
    """Persist a verified, correlated recovery outcome (idempotent).

    ``delivery_id`` (X-Razorpay-Event-Id) is the PRIMARY KEY and equals the
    webhook delivery id already gated for uniqueness, so an event can never
    yield a second recovery. The amount is the TRUSTED amount_paid observed on
    the link; an absent amount is recorded as NULL rather than fabricated.

    Crash-safe: uses ``INSERT OR IGNORE`` so a retry (Razorpay redelivery after
    a crash between the recovery write and the delivery-status update) treats an
    already-present recovery as a no-op instead of a conflict — returning False
    when the row already existed, True when this call inserted it. sqlite3.Error
    (other than the ignored unique violation) propagates as a persistence failure.
    """
    try:
        cur = conn.execute(
            """
            INSERT OR IGNORE INTO webhook_recovery_outcomes (
                delivery_id, payment_link_id, referenced_event_id,
                amount_paid_paise, currency, payment_id, recovered_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                delivery_id,
                payment_link_id,
                referenced_event_id,
                amount_paid_paise,
                currency,
                payment_id,
                recovered_at,
            ),
        )
        conn.commit()
        return cur.rowcount > 0
    except sqlite3.Error:
        conn.rollback()
        raise


def get_webhook_recovery_outcome(
    conn: sqlite3.Connection, delivery_id: str
) -> dict[str, Any] | None:
    """Retrieve a persisted recovery outcome by its idempotency key, or None."""
    row = conn.execute(
        "SELECT * FROM webhook_recovery_outcomes WHERE delivery_id = ?",
        (delivery_id,),
    ).fetchone()
    if row is None:
        return None
    return dict(row)


def get_webhook_recovery_outcome_by_payment_link_id(
    conn: sqlite3.Connection, payment_link_id: str
) -> dict[str, Any] | None:
    """Retrieve the most recent verified recovery for a Payment Link, or None.

    Read-only dashboard/trace support: correlates a Payment Link created on the
    execution side to the verified recovery (if any) observed via the webhook.
    """
    row = conn.execute(
        """
        SELECT * FROM webhook_recovery_outcomes
        WHERE payment_link_id = ?
        ORDER BY recovered_at DESC
        LIMIT 1
        """,
        (payment_link_id,),
    ).fetchone()
    if row is None:
        return None
    return dict(row)

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
from datetime import datetime, timedelta
from typing import Any

from .classification import ClassificationResult
from .config import get_database_path
from .models import PaymentEvent
from .executor import ExecutionOutcome
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
    reported_at        TEXT NOT NULL,
    PRIMARY KEY (event_id, intervention, reported_at)
)
"""


def connect(path: str) -> sqlite3.Connection:
    """Open a SQLite connection to the given database path."""
    conn = sqlite3.connect(path)
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
        conn.commit()
    except sqlite3.Error:
        conn.rollback()
        raise


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
                external_reference, detail, reported_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                outcome.event_id,
                outcome.intervention,
                outcome.execution_mode,
                outcome.status,
                outcome.external_reference,
                outcome.detail,
                outcome.reported_at,
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

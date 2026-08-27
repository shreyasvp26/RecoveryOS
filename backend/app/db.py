"""RecoveryOS SQLite persistence layer.

Phase 2/3: initializes the payment_events table and provides the single
persistence boundary between the domain model and the application/service layer.
No raw SQL lives outside this module. Persistence stores facts; it never makes
business decisions.
"""

from __future__ import annotations

import json
import sqlite3
from typing import Any

from .config import get_database_path
from .models import PaymentEvent

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


def connect(path: str) -> sqlite3.Connection:
    """Open a SQLite connection to the given database path."""
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    return conn


def connect_database() -> sqlite3.Connection:
    """Open a SQLite connection to the configured database path."""
    return connect(get_database_path())


def init_db(conn: sqlite3.Connection) -> None:
    """Create the payment_events table if it does not already exist."""
    try:
        conn.execute(_PAYMENT_EVENTS_DDL)
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

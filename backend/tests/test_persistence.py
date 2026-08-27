"""Persistence and round-trip integrity tests for SQLite storage."""

from __future__ import annotations

import sqlite3

import pytest

from app.db import get_payment_event, init_db, insert_payment_event
from app.models import PaymentEvent


def valid_event(**overrides) -> dict:
    base = {
        "event_id": "evt_001",
        "order_id": "order_001",
        "payment_id": "pay_001",
        "customer_id": "cust_001",
        "amount_paise": 499900,
        "currency": "INR",
        "payment_method": "upi",
        "failure_reason": "bank_timeout",
        "bank": "HDFC",
        "risk_flag": "normal",
        "customer_history": {
            "prior_successful_payments": 4,
            "prior_failed_payments": 1,
            "has_active_subscription": True,
        },
        "timestamp": "2026-08-27T12:00:00+00:00",
    }
    base.update(overrides)
    return base


def test_database_initialization_creates_table(db_conn) -> None:
    rows = db_conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='payment_events'"
    ).fetchall()
    assert [row[0] for row in rows] == ["payment_events"]


def test_database_initialization_is_idempotent(db_conn) -> None:
    init_db(db_conn)
    init_db(db_conn)
    tables = db_conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='payment_events'"
    ).fetchall()
    assert len(tables) == 1


def test_persistence_and_retrieval(db_conn) -> None:
    event = PaymentEvent.from_dict(valid_event())
    insert_payment_event(db_conn, event)

    retrieved = get_payment_event(db_conn, event.event_id)
    assert retrieved is not None
    assert retrieved == event


def test_round_trip_preserves_locked_contract(db_conn) -> None:
    original = valid_event()
    event = PaymentEvent.from_dict(original)
    insert_payment_event(db_conn, event)

    retrieved = get_payment_event(db_conn, event.event_id)
    assert retrieved is not None
    assert retrieved.to_dict() == original


def test_amount_paise_remains_integer_through_round_trip(db_conn) -> None:
    event = PaymentEvent.from_dict(valid_event(amount_paise=499900))
    insert_payment_event(db_conn, event)

    retrieved = get_payment_event(db_conn, event.event_id)
    assert isinstance(retrieved.amount_paise, int)
    assert not isinstance(retrieved.amount_paise, float)
    assert retrieved.amount_paise == 499900


def test_customer_history_survives_persistence(db_conn) -> None:
    history = {
        "prior_successful_payments": 12,
        "prior_failed_payments": 3,
        "has_active_subscription": False,
    }
    event = PaymentEvent.from_dict(valid_event(customer_history=history))
    insert_payment_event(db_conn, event)

    retrieved = get_payment_event(db_conn, event.event_id)
    assert retrieved.customer_history.to_dict() == history


def test_get_missing_event_returns_none(db_conn) -> None:
    assert get_payment_event(db_conn, "does_not_exist") is None


def test_duplicate_event_id_is_rejected(db_conn) -> None:
    event = PaymentEvent.from_dict(valid_event())
    insert_payment_event(db_conn, event)
    with pytest.raises(sqlite3.IntegrityError):
        insert_payment_event(db_conn, event)


def test_multiple_events_persist_independently(db_conn) -> None:
    event_a = PaymentEvent.from_dict(valid_event(event_id="evt_a"))
    event_b = PaymentEvent.from_dict(
        valid_event(event_id="evt_b", amount_paise=12345, payment_method="card")
    )
    insert_payment_event(db_conn, event_a)
    insert_payment_event(db_conn, event_b)

    assert get_payment_event(db_conn, "evt_a") == event_a
    assert get_payment_event(db_conn, "evt_b") == event_b

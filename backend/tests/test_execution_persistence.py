"""Phase 7 persistence tests for execution outcomes."""

from __future__ import annotations

import sqlite3

import pytest

from app.db import get_execution_outcome, insert_execution_outcome
from app.executor import ExecutionOutcome

REPORTED_AT = "2026-08-27T13:00:00+00:00"


def _outcome(
    event_id: str = "evt_flow",
    intervention: str = "retry_delayed",
    **overrides,
) -> ExecutionOutcome:
    data = {
        "event_id": event_id,
        "intervention": intervention,
        "execution_mode": "SIMULATED",
        "status": "SUCCESS",
        "external_reference": None,
        "detail": None,
        "reported_at": REPORTED_AT,
    }
    data.update(overrides)
    return ExecutionOutcome(**data)


def test_init_db_creates_execution_outcomes_table(db_conn) -> None:
    outcome = _outcome()
    insert_execution_outcome(db_conn, outcome)
    assert get_execution_outcome(db_conn, "evt_flow", "retry_delayed", REPORTED_AT) == outcome


def test_execution_outcome_round_trip(db_conn) -> None:
    outcome = _outcome(
        intervention="payment_link",
        execution_mode="REAL_RAZORPAY",
        status="SUCCESS",
        external_reference="https://rzp.io/l/real123",
        detail=None,
    )
    insert_execution_outcome(db_conn, outcome)
    retrieved = get_execution_outcome(db_conn, "evt_flow", "payment_link", REPORTED_AT)
    assert retrieved == outcome
    assert retrieved.to_dict() == outcome.to_dict()


def test_failed_outcome_round_trip(db_conn) -> None:
    outcome = _outcome(
        intervention="payment_link",
        execution_mode="REAL_RAZORPAY",
        status="FAILED",
        external_reference=None,
        detail="razorpay_api_error: down",
    )
    insert_execution_outcome(db_conn, outcome)
    retrieved = get_execution_outcome(db_conn, "evt_flow", "payment_link", REPORTED_AT)
    assert retrieved == outcome


def test_duplicate_outcome_is_rejected(db_conn) -> None:
    insert_execution_outcome(db_conn, _outcome())
    with pytest.raises(sqlite3.IntegrityError):
        insert_execution_outcome(db_conn, _outcome())


def test_get_missing_outcome_returns_none(db_conn) -> None:
    assert get_execution_outcome(db_conn, "evt_ghost", "retry_delayed", REPORTED_AT) is None


def test_outcomes_correlated_by_event_id(db_conn) -> None:
    first = _outcome(event_id="evt_a", intervention="reminder")
    second = _outcome(event_id="evt_b", intervention="reminder")
    insert_execution_outcome(db_conn, first)
    insert_execution_outcome(db_conn, second)
    assert get_execution_outcome(db_conn, "evt_a", "reminder", REPORTED_AT) == first
    assert get_execution_outcome(db_conn, "evt_b", "reminder", REPORTED_AT) == second

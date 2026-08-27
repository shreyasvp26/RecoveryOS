"""Phase 4 tests for the thin event ingestion boundary."""

from __future__ import annotations

import pytest

from app.db import get_payment_event
from app.ingestion import IngestionStatus, ingest_event
from app.models import PaymentEvent


def payload(**overrides) -> dict:
    base = {
        "event_id": "evt_100",
        "order_id": "order_100",
        "payment_id": "pay_100",
        "customer_id": "cust_100",
        "amount_paise": 75000,
        "currency": "INR",
        "payment_method": "card",
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


def test_valid_event_is_ingested(db_conn) -> None:
    result = ingest_event(db_conn, payload())
    assert result.status is IngestionStatus.SUCCESS
    assert result.event_id == "evt_100"


def test_ingested_event_is_persisted_and_retrievable(db_conn) -> None:
    original = payload()
    result = ingest_event(db_conn, original)
    assert result.status is IngestionStatus.SUCCESS
    retrieved = get_payment_event(db_conn, original["event_id"])
    assert retrieved == PaymentEvent.from_dict(original)


def test_ingestion_accepts_payment_event_instance(db_conn) -> None:
    event = PaymentEvent.from_dict(payload())
    result = ingest_event(db_conn, event)
    assert result.status is IngestionStatus.SUCCESS
    assert get_payment_event(db_conn, event.event_id) == event


def test_duplicate_event_reported_deterministically(db_conn) -> None:
    first = ingest_event(db_conn, payload())
    second = ingest_event(db_conn, payload())
    assert first.status is IngestionStatus.SUCCESS
    assert second.status is IngestionStatus.DUPLICATE
    assert second.event_id == "evt_100"
    rows = db_conn.execute(
        "SELECT COUNT(*) FROM payment_events WHERE event_id = ?", ("evt_100",)
    ).fetchone()[0]
    assert rows == 1


def test_duplicate_detected_across_input_kinds(db_conn) -> None:
    ingest_event(db_conn, payload())
    result = ingest_event(db_conn, PaymentEvent.from_dict(payload()))
    assert result.status is IngestionStatus.DUPLICATE
    rows = db_conn.execute("SELECT COUNT(*) FROM payment_events").fetchone()[0]
    assert rows == 1


@pytest.mark.parametrize(
    "bad",
    [
        {},
        {"event_id": "evt_x"},
        payload(payment_method="crypto"),
        payload(amount_paise=-1),
        payload(risk_flag="risky"),
        payload(timestamp="not-a-date"),
        payload(customer_history={"prior_successful_payments": 1}),
        payload(customer_history={
            "prior_successful_payments": 1,
            "prior_failed_payments": 0,
            "has_active_subscription": True,
            "extra": "x",
        }),
        "not a dict",
        None,
        [1, 2, 3],
    ],
)
def test_invalid_events_are_rejected_and_not_persisted(db_conn, bad) -> None:
    result = ingest_event(db_conn, bad)
    assert result.status is IngestionStatus.INVALID
    rows = db_conn.execute("SELECT COUNT(*) FROM payment_events").fetchone()[0]
    assert rows == 0


def test_persistence_failure_surfaces_explicitly(db_conn) -> None:
    db_conn.execute("DROP TABLE payment_events")
    db_conn.commit()
    result = ingest_event(db_conn, payload())
    assert result.status is IngestionStatus.ERROR
    assert result.event_id == "evt_100"
    assert "persistence failure" in result.detail


def test_unexpected_ingestion_failure_is_explicit(db_conn) -> None:
    class BrokenConnection:
        def execute(self, *args, **kwargs) -> None:
            raise RuntimeError("boom")

    result = ingest_event(BrokenConnection(), payload())
    assert result.status is IngestionStatus.ERROR
    assert result.event_id == "evt_100"
    assert "unexpected ingestion failure" in result.detail

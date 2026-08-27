"""Thin event ingestion boundary.

Phase 4: validates a PaymentEvent (or its raw payload) through the locked
domain contract, then persists it through the Phase 3 SQLite layer. The
ingestion layer makes no recovery decisions. Every event results in an
explicit SUCCESS, DUPLICATE, INVALID, or ERROR outcome — failures are never
swallowed and fake success is never returned.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from enum import Enum
from typing import Any

from .db import get_payment_event, insert_payment_event
from .models import PaymentEvent


class IngestionStatus(Enum):
    """Explicit outcomes for a single ingested payment event."""

    SUCCESS = "success"
    DUPLICATE = "duplicate"
    INVALID = "invalid"
    ERROR = "error"


@dataclass(frozen=True)
class IngestionResult:
    """The explicit result of an ingestion attempt."""

    status: IngestionStatus
    event_id: str | None = None
    detail: str = ""


def ingest_event(conn: sqlite3.Connection, payload: Any) -> IngestionResult:
    """Validate and persist a single payment event.

    Accepts either a PaymentEvent instance or a raw payload dict matching the
    locked contract. Deterministic duplicate handling: an event whose event_id
    already exists is reported as a duplicate and never persisted twice.
    """
    if isinstance(payload, PaymentEvent):
        event = payload
    else:
        try:
            event = PaymentEvent.from_dict(payload)
        except (ValueError, TypeError) as exc:
            return IngestionResult(
                status=IngestionStatus.INVALID,
                detail=str(exc) or "invalid payment event payload",
            )

    try:
        if get_payment_event(conn, event.event_id) is not None:
            return IngestionResult(
                status=IngestionStatus.DUPLICATE,
                event_id=event.event_id,
                detail="payment event already ingested",
            )
        insert_payment_event(conn, event)
    except sqlite3.Error as exc:
        return IngestionResult(
            status=IngestionStatus.ERROR,
            event_id=event.event_id,
            detail=f"persistence failure: {exc}",
        )
    except Exception as exc:
        return IngestionResult(
            status=IngestionStatus.ERROR,
            event_id=event.event_id,
            detail=f"unexpected ingestion failure: {exc}",
        )
    return IngestionResult(status=IngestionStatus.SUCCESS, event_id=event.event_id)
"""Closed-loop webhook processing service (OUTCOME channel).

Phase 12: turns a *verified* Razorpay webhook event into an idempotent,
durably-recorded, correlated recovery outcome. This service is intentionally
separate from the execution channel: it never creates Payment Links, never
calls the executor, policy engine, selector, or any intervention. It only
records provider-observed payment outcomes and correlates them with the
Payment Link id a previous (Phase 11) execution persisted.

Idempotency is durable: the X-Razorpay-Event-Id delivery id is a SQLite
PRIMARY KEY, so duplicates are rejected by the database, and the same id with a
different body is an explicit CONFLICT (never overwritten, never a second
recovery). Persistent failures surface as errors so Razorpay retries.
"""

from __future__ import annotations

import hashlib
import sqlite3
from dataclasses import dataclass
from typing import Any

from . import db
from .razorpay_webhook import (
    SUPPORTED_WEBHOOK_EVENT,
    WebhookEvent,
    WebhookPayloadError,
)

# Closed-loop statuses surfaced to the HTTP layer.
S_DEDUPLICATED = "deduplicated"
S_CONFLICT = "conflict"
S_IGNORED = "ignored"
S_UNMATCHED = "unmatched"
S_PROCESSED = "processed"
S_PERSISTENCE_FAILURE = "persistence_failure"

# Persisted delivery-row statuses.
_DELIVERY_CLAIMED = "claimed"
_DELIVERY_IGNORED = "ignored"
_DELIVERY_PROCESSED = "processed"
_DELIVERY_UNMATCHED = "unmatched"


@dataclass(frozen=True)
class WebhookProcessResult:
    """The outcome of processing one verified webhook delivery."""

    status: str
    delivery_id: str
    event_type: str | None = None
    payment_link_id: str | None = None
    amount_paid_paise: int | None = None
    detail: str | None = None
    previous_status: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "delivery_id": self.delivery_id,
            "event": self.event_type,
            "payment_link_id": self.payment_link_id,
            "amount_paid_paise": self.amount_paid_paise,
            "detail": self.detail,
            "previous_status": self.previous_status,
        }


def _body_sha256(raw_body: bytes) -> str:
    return hashlib.sha256(raw_body).hexdigest()


def claim_webhook_delivery(
    conn: sqlite3.Connection, event: WebhookEvent, body_sha256: str, received_at: str
) -> str:
    """Durably claim a delivery id, returning 'claimed'/'deduplicated'/'conflict'.

    A fresh id is inserted and returns 'claimed'. A re-delivered id whose raw
    body hash matches returns 'deduplicated' (a 2xx no-op; the prior outcome is
    not reprocessed or double-counted). The same id with a DIFFERENT body hash
    returns 'conflict' — an explicit, never-overwritten state. sqlite3.Error
    (other than the unique-violation IntegrityError) propagates so the caller
    can surface it as an error HTTP and Razorpay will retry.
    """
    claim_status = _DELIVERY_CLAIMED
    try:
        db.insert_webhook_delivery(
            conn,
            delivery_id=event.delivery_id,
            body_sha256=body_sha256,
            event_type=event.event_type,
            payment_link_id=event.payment_link_id,
            status=claim_status,
            received_at=received_at,
        )
        return "claimed"
    except sqlite3.IntegrityError:
        existing = db.get_webhook_delivery(conn, event.delivery_id)
        if existing is None:
            raise
        if existing["body_sha256"] == body_sha256:
            return "deduplicated"
        return "conflict"


def process_webhook(
    conn: sqlite3.Connection,
    event: WebhookEvent,
    raw_body: bytes,
    received_at: str,
) -> WebhookProcessResult:
    """Idempotently process one verified webhook delivery end-to-end.

    This is the webhook path's single orchestration entry: idempotent claim,
    then (once the delivery is claimed) classify the event and correlate it
    with the persisted Payment Link outcome. Correlation of supported events
    into a verified recovery outcome/unmatched record is filled in by the
    correlation step; this function never invokes any intervention path.
    """
    body_sha256 = _body_sha256(raw_body)

    try:
        claim = claim_webhook_delivery(conn, event, body_sha256, received_at)
    except sqlite3.Error:
        return WebhookProcessResult(
            status=S_PERSISTENCE_FAILURE,
            delivery_id=event.delivery_id,
            event_type=event.event_type,
            detail="persistence_failure: could not record webhook delivery; "
            "returning error so Razorpay retries",
        )

    if claim == "deduplicated":
        return WebhookProcessResult(
            status=S_DEDUPLICATED,
            delivery_id=event.delivery_id,
            event_type=event.event_type,
            detail="duplicate delivery (same event id and body); already recorded",
        )
    if claim == "conflict":
        return WebhookProcessResult(
            status=S_CONFLICT,
            delivery_id=event.delivery_id,
            event_type=event.event_type,
            detail="same event id delivered with a different body; refusing to "
            "overwrite and refusing a second recovery",
        )

    # Freshly claimed. Unsupported events are explicitly recorded as ignored
    # and acknowledged; they are never executed or turned into a recovery.
    if event.event_type != SUPPORTED_WEBHOOK_EVENT:
        db.update_webhook_delivery_status(conn, event.delivery_id, _DELIVERY_IGNORED)
        return WebhookProcessResult(
            status=S_IGNORED,
            delivery_id=event.delivery_id,
            event_type=event.event_type,
            detail="unsupported event; recorded and ignored",
        )

    return _correlate_supported_event(conn, event, received_at)


def _correlate_supported_event(
    conn: sqlite3.Connection, event: WebhookEvent, received_at: str
) -> WebhookProcessResult:
    """Correlate a verified payment_link.paid delivery with persisted state.

    The payment link id must match the actual id of a REAL_RAZORPAY ``payment_link``
    SUCCESS outcome persisted on the execution side (Phase 11). A match yields a
    PROCESSED (verified, correlated) recovery outcome whose recovery amount is the
    TRUSTED ``amount_paid`` observed on the link — never the original event amount.
    A non-matching (or absent) link is recorded as an explicit UNMATCHED audit with
    NO fabricated recovery. This function never re-executes and never invokes any
    intervention path.
    """
    link_id = event.payment_link_id
    try:
        if link_id is None:
            db.update_webhook_delivery_status(conn, event.delivery_id, _DELIVERY_UNMATCHED)
            return WebhookProcessResult(
                status=S_UNMATCHED,
                delivery_id=event.delivery_id,
                event_type=event.event_type,
                detail="payment_link.paid event carried no payment link id; "
                "cannot correlate to a persisted recovery",
            )

        matched = db.get_execution_outcome_by_payment_link_id(conn, link_id)
        if matched is None:
            db.update_webhook_delivery_status(conn, event.delivery_id, _DELIVERY_UNMATCHED)
            return WebhookProcessResult(
                status=S_UNMATCHED,
                delivery_id=event.delivery_id,
                event_type=event.event_type,
                payment_link_id=link_id,
                detail="payment link id does not match any persisted REAL_RAZORPAY "
                "recovery; recorded as unmatched with no fabricated recovery",
            )

        # Persist the verified, correlated recovery outcome. delivery_id is the
        # PRIMARY KEY and was already gated for durable uniqueness at claim, so
        # this can never double-count; an IntegrityError here still refuses a
        # second recovery rather than overwriting.
        try:
            db.insert_webhook_recovery_outcome(
                conn,
                delivery_id=event.delivery_id,
                payment_link_id=link_id,
                referenced_event_id=matched.event_id,
                amount_paid_paise=event.amount_paid_paise,
                currency=event.currency,
                payment_id=event.payment_id,
                recovered_at=received_at,
            )
        except sqlite3.IntegrityError:
            return WebhookProcessResult(
                status=S_CONFLICT,
                delivery_id=event.delivery_id,
                event_type=event.event_type,
                payment_link_id=link_id,
                detail="a verified recovery outcome for this delivery already "
                "exists; refusing a second recovery",
            )

        db.update_webhook_delivery_status(conn, event.delivery_id, _DELIVERY_PROCESSED)
        return WebhookProcessResult(
            status=S_PROCESSED,
            delivery_id=event.delivery_id,
            event_type=event.event_type,
            payment_link_id=link_id,
            amount_paid_paise=event.amount_paid_paise,
            detail=f"payment link id {link_id!r} correlated to Phase 11 execution "
            f"outcome for event {matched.event_id!r}; trusted amount_paid on the "
            "link recorded as the verified recovery outcome",
        )
    except sqlite3.Error:
        return WebhookProcessResult(
            status=S_PERSISTENCE_FAILURE,
            delivery_id=event.delivery_id,
            event_type=event.event_type,
            payment_link_id=link_id,
            detail="persistence_failure during correlation; returning error so "
            "Razorpay retries",
        )

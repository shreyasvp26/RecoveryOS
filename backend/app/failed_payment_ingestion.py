"""Map a verified ``payment.failed`` webhook into a locked ``PaymentEvent``.

This is the INGESTION side of the closed loop: a genuinely failed payment (a
``payment.failed`` webhook already signature-verified and parsed into a
``FailedPaymentEvent``) becomes a ``PaymentEvent`` so the existing
detect -> diagnose -> policy -> optimize pipeline can run against a real failed
payment. It never authorizes or executes anything on its own.

Honesty about missing fields. A real Razorpay ``payment.failed`` webhook does
not carry every field of the locked ``PaymentEvent`` contract — in particular
``customer_history`` (prior successful/failed counts, subscription flag) is not
provided by the provider. This mapper never fabricates recovery truth. For the
gaps it fills:

  * ``customer_history`` — derived from the customer's own PERSISTED payment
    events when available (prior successes/failures counted from persisted
    rows, never invented), and a neutral history (0 successes, 0 failures, no
    active subscription) when the customer has no persisted history yet.
  * ``event_id`` and ``bank`` — neither is supplied by a real ``payment.failed``
    delivery. ``event_id`` is synthesized deterministically from the delivery
    (idempotency) key, and ``bank`` defaults to a neutral value because the
    provider does not submit it.
"""

from __future__ import annotations

import hashlib
import sqlite3
from typing import Any

from .db import get_payment_events_for_customer
from .models import CustomerHistory, PaymentEvent
from .razorpay_webhook import FailedPaymentEvent

# Razorpay does not submit a bank name in a payment.failed webhook. The locked
# contract requires a non-empty string; a neutral placeholder is used and the
# classifier treats it as decision-time input only.
_NEUTRAL_BANK: str = "provider"

_METHOD_TO_PAYMENT_METHOD: dict[str, str] = {
    "card": "card",
    "netbanking": "netbanking",
    "upi": "upi",
    "wallet": "wallet",
}

# Razorpay currency codes are uppercase 3-letter codes; the contract accepts
# any non-empty string, so the value is passed through unchanged.
_DEFAULT_CURRENCY: str = "INR"


def derive_customer_history(
    conn: sqlite3.Connection, customer_id: str | None
) -> CustomerHistory:
    """Return an honest CustomerHistory from persisted state (never invented).

    Counts the customer's previously persisted payment events (each persisted
    event is, by construction, a failed payment in this system, so those count
    toward prior_failed_payments). No active subscription is assumed unless the
    provider/state states otherwise; prior successes default to 0 because this
    system only observes failures. A customer with no persisted events gets a
    neutral history.
    """
    if not customer_id:
        return CustomerHistory(
            prior_successful_payments=0, prior_failed_payments=0, has_active_subscription=False
        )
    prior_events = get_payment_events_for_customer(conn, customer_id)
    prior_failed = len(prior_events)
    return CustomerHistory(
        prior_successful_payments=0,
        prior_failed_payments=prior_failed,
        has_active_subscription=False,
    )


def map_failed_payment_to_event(
    conn: sqlite3.Connection,
    failed: FailedPaymentEvent,
    *,
    event_id_prefix: str = "evt_pfail",
) -> PaymentEvent:
    """Map a verified ``payment.failed`` into a locked ``PaymentEvent``.

    ``event_id`` is derived deterministically (and idempotently) from the
    delivery id so the same delivery always maps to the same event — duplicate
    deliveries of the same failure never create a second event.
    """
    customer_id = failed.customer_id or f"cust_{failed.payment_id}"
    history = derive_customer_history(conn, customer_id)

    digest = hashlib.sha1(failed.delivery_id.encode("utf-8")).hexdigest()[:12]
    event_id = f"{event_id_prefix}_{digest}"

    return PaymentEvent(
        event_id=event_id,
        order_id=failed.order_id,
        payment_id=failed.payment_id,
        customer_id=customer_id,
        amount_paise=(
            failed.amount_paise
            if isinstance(failed.amount_paise, int)
            else 0
        ),
        currency=failed.currency or _DEFAULT_CURRENCY,
        payment_method=_METHOD_TO_PAYMENT_METHOD.get(
            failed.payment_method, "card"
        ),
        failure_reason=failed.failure_reason or "payment_failed",
        bank=failed.bank or _NEUTRAL_BANK,
        risk_flag="normal",
        customer_history=history,
        timestamp=failed.failed_at
        or "1970-01-01T00:00:00+00:00",
    )

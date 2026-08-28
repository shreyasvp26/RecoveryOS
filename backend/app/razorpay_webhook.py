"""Razorpay webhook signature verification and payload parsing boundary.

Phase 12: verifies the HMAC-SHA256 signature over the exact raw request body
and parses a *verified* Razorpay webhook into a structured ``WebhookEvent``.

This is an OUTCOME channel only. Nothing in this module — or anywhere on the
webhook path — creates Payment Links, calls the executor, the policy engine,
the selector, or any intervention. Cryptographic verification and payload
shape are isolated here so the HTTP route stays free of business logic.

Security invariants (non-negotiable):
  * The signature is HMAC-SHA256 computed over the UNPARSED raw request body;
    the JSON is never re-serialized or re-encoded before comparison.
  * Comparison is constant-time via ``hmac.compare_digest``.
  * A missing secret, missing signature, or mismatched signature fails closed.
"""

from __future__ import annotations

import hashlib
import hmac
import json
from dataclasses import dataclass
from typing import Any

# The Razorpay webhook event this closed loop consumes. Unsupported events are
# explicitly ignored/recorded further down the path, never executed.
SUPPORTED_WEBHOOK_EVENT: str = "payment_link.paid"


class WebhookSignatureError(Exception):
    """The webhook signature is missing or does not match the raw body.

    Raised before any payload parsing happens; callers must reject with a 4xx.
    """


class WebhookPayloadError(Exception):
    """A verified webhook payload is malformed or cannot be mapped.

    Raised only after signature verification succeeded, so the payload is
    trusted but structurally invalid.
    """


def verify_signature(raw_body: bytes, signature: str, secret: str) -> bool:
    """Return whether ``signature`` matches the HMAC-SHA256 of ``raw_body``.

    The digest is keyed by ``secret`` and computed over the exact raw bytes of
    the delivered request (never a re-serialized JSON string). Comparison is
    constant-time. Any missing/empty input fails closed to False.
    """
    if not isinstance(raw_body, bytes) or not raw_body:
        return False
    if not isinstance(signature, str) or not signature.strip():
        return False
    if not isinstance(secret, str) or not secret:
        return False
    expected = hmac.new(
        secret.encode("utf-8"), raw_body, hashlib.sha256
    ).hexdigest()
    return hmac.compare_digest(expected, signature.strip())


def require_valid_signature(raw_body: bytes, signature: str, secret: str) -> None:
    """Raise ``WebhookSignatureError`` unless the raw-body signature matches.

    This is the single gate the webhook route calls to authorize delivery; it
    raises (fail-closed) rather than returning a boolean so no caller can
    forget to handle a mismatched signature.
    """
    if not verify_signature(raw_body, signature, secret):
        raise WebhookSignatureError(
            "razorpay webhook signature is missing or does not match the "
            "raw request body"
        )


@dataclass(frozen=True)
class WebhookEvent:
    """A verified Razorpay webhook mapped to the fields the closed loop needs.

    ``delivery_id`` is the canonical idempotency key taken from the
    ``X-Razorpay-Event-Id`` delivery header (unique per event). ``payment_link_id``
    is the Phase 11-persisted correlation key. ``amount_paid_paise`` is the
    TRUSTED amount actually paid on the link (never the original event amount).
    """

    delivery_id: str
    event_type: str
    payment_link_id: str | None
    payment_link_status: str | None
    amount_paid_paise: int | None
    currency: str | None
    payment_id: str | None
    reference_id: str | None

    def __post_init__(self) -> None:
        if not isinstance(self.delivery_id, str) or not self.delivery_id.strip():
            raise WebhookPayloadError("delivery_id (X-Razorpay-Event-Id) is required")
        if not isinstance(self.event_type, str) or not self.event_type.strip():
            raise WebhookPayloadError("webhook event type is required")
        for name in (
            "payment_link_id",
            "payment_link_status",
            "currency",
            "payment_id",
            "reference_id",
        ):
            value = getattr(self, name)
            if value is not None and (
                not isinstance(value, str) or not value.strip()
            ):
                raise WebhookPayloadError(
                    f"{name} must be None or a non-empty string"
                )
        if self.amount_paid_paise is not None and not isinstance(
            self.amount_paid_paise, int
        ):
            raise WebhookPayloadError("amount_paid_paise must be None or an integer")


def _event_field(payload: dict[str, Any]) -> str:
    value = payload.get("event")
    if not isinstance(value, str) or not value.strip():
        raise WebhookPayloadError("webhook payload is missing a valid 'event' field")
    return value


def _validate_paid_shape(payload: dict[str, Any]) -> None:
    """Strictly validate the shape of a `payment_link.paid` event.

    A paid event is the only webhook event the closed loop consumes, so its
    shape is required to be complete and trustworthy: it must carry a real
    Payment Link id, report status ``paid``, and give the actual non-negative
    ``amount_paid``. Any structural gap raises ``WebhookPayloadError`` (mapped
    to a 4xx) so an under-specified paid event is never silently turned into an
    unmatched audit. Other event types stay structurally lenient because they
    are recorded-and-ignored, never processed.
    """
    link_entity = (payload.get("payload", {}).get("payment_link", {}).get("entity")) or {}
    if not isinstance(link_entity, dict) or not link_entity:
        raise WebhookPayloadError(
            "payment_link.paid event is missing the payment_link.entity"
        )
    link_id = link_entity.get("id")
    if not isinstance(link_id, str) or not link_id.strip():
        raise WebhookPayloadError(
            "payment_link.paid event is missing a payment link id"
        )
    status = link_entity.get("status")
    if status != "paid":
        raise WebhookPayloadError(
            f"payment_link.paid event must have status 'paid', got {status!r}"
        )
    amount_paid = link_entity.get("amount_paid")
    if (
        not isinstance(amount_paid, int)
        or isinstance(amount_paid, bool)
        or amount_paid < 0
    ):
        raise WebhookPayloadError(
            "payment_link.paid event must carry a non-negative integer amount_paid"
        )


def parse_webhook_payload(raw_body: bytes, delivery_id: str) -> WebhookEvent:
    """Parse and validate a verified webhook raw body into a ``WebhookEvent``.

    Assumes the signature has already been verified. Raises
    ``WebhookPayloadError`` for malformed/unsupported shape. It never says
    whether a payment_link matched a persisted outcome (that is correlation,
    done separately with the database).
    """
    if not isinstance(raw_body, bytes) or not raw_body:
        raise WebhookPayloadError("webhook request body is empty")
    try:
        data = json.loads(raw_body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise WebhookPayloadError(f"webhook body is not valid JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise WebhookPayloadError("webhook payload must be a JSON object")

    event_type = _event_field(data)

    if event_type == SUPPORTED_WEBHOOK_EVENT:
        _validate_paid_shape(data)

    link_entity = (
        data.get("payload", {}).get("payment_link", {}).get("entity")
        or {}
    )
    payment_entity = (
        data.get("payload", {}).get("payment", {}).get("entity") or {}
    )

    payment_link_id = link_entity.get("id")
    status = link_entity.get("status")
    amount_paid = link_entity.get("amount_paid")
    currency = link_entity.get("currency")
    payment_id = payment_entity.get("id")
    reference_id = link_entity.get("reference_id")

    return WebhookEvent(
        delivery_id=delivery_id,
        event_type=event_type,
        payment_link_id=payment_link_id,
        payment_link_status=status,
        amount_paid_paise=amount_paid,
        currency=currency,
        payment_id=payment_id,
        reference_id=reference_id,
    )

"""Razorpay webhook signature verification and payload parsing boundary.

Phase 12: verifies the HMAC-SHA256 signature over the exact raw request body
and parses a *verified* Razorpay webhook into a structured event.

Two architecturally distinct webhook channels live here:

  * ``payment_link.paid`` — an OUTCOME channel. This is the closed-loop
    recovery channel: it creates Payment Links, calls the executor, the policy
    engine, the selector, or any intervention NEVER. It only records verified
    recovery evidence. (Phase 12.)

  * ``payment.failed`` — an INGESTION channel. A genuine failed-payment event
    is the INPUT that feeds the recovery loop: once verified, it is mapped to
    a ``PaymentEvent`` so the existing detect->diagnose->policy->optimize
    pipeline can run against a real failed payment. It is advisory input only;
    it never authorizes or executes anything on its own.

Cryptographic verification and payload shape are isolated here so the HTTP
route stays free of business logic. The two channels SHARE the signature gate
(both verify before parsing) but are processed with distinct semantics further
down the path.

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
from datetime import datetime, timezone
from dataclasses import dataclass
from typing import Any

# The OUTCOME webhook event this recovered-revenue loop consumes (Phase 12).
SUPPORTED_WEBHOOK_EVENT: str = "payment_link.paid"

# The INGESTION webhook event that carries a genuinely failed payment into the
# recovery loop. It is semantically an INPUT channel, never an OUTCOME: it
# feeds a PaymentEvent into detection rather than recording recovery evidence.
INGESTION_WEBHOOK_EVENT: str = "payment.failed"


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


def _child_dict(container: dict[str, Any], key: str, path: str) -> dict[str, Any]:
    """Return ``container[key]`` as a dict, else raise ``WebhookPayloadError``.

    Guards each JSON-object level the parser accesses. A missing key is treated
    as an empty object (fields under it are optional), but a PRESENT value that
    is not a JSON object (e.g. ``[]``, a string, or ``null`` where an object is
    expected) is a signed-but-malformed shape and becomes a controlled 4xx —
    never an ``AttributeError``/500. ``path`` names the field for diagnostics.
    """
    value = container.get(key, {})
    if not isinstance(value, dict):
        raise WebhookPayloadError(f"webhook payload field {path!r} must be a JSON object")
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
    payload_obj = _child_dict(payload, "payload", "payload")
    link_obj = _child_dict(payload_obj, "payment_link", "payload.payment_link")
    link_entity = link_obj.get("entity") or {}
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

    payload_obj = _child_dict(data, "payload", "payload")
    link_obj = _child_dict(payload_obj, "payment_link", "payload.payment_link")
    link_entity = _child_dict(link_obj, "entity", "payload.payment_link.entity")
    payment_obj = _child_dict(payload_obj, "payment", "payload.payment")
    payment_entity = _child_dict(payment_obj, "entity", "payload.payment.entity")

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


@dataclass(frozen=True)
class FailedPaymentEvent:
    """A *verified* Razorpay ``payment.failed`` webhook mapped to the fields a
    ``PaymentEvent`` needs. This is an INGESTION-channel event: it feeds the
    recovery loop, never records an outcome.

    The amount, currency, payment method, order, customer and failure detail
    are taken from the trusted, signature-verified payload. ``failure_reason``
    is derived from Razorpay's ``error_code``/``error_description`` (never
    fabricated), and the customer/order identifiers come straight from the
    payment entity. ``delivery_id`` (X-Razorpay-Event-Id) is the canonical
    idempotency key so the same failure is never ingested twice.
    """

    delivery_id: str
    event_type: str
    payment_id: str
    order_id: str
    customer_id: str | None
    amount_paise: int | None
    currency: str | None
    payment_method: str | None
    failure_reason: str | None
    bank: str | None
    failed_at: str | None

    def __post_init__(self) -> None:
        if not isinstance(self.delivery_id, str) or not self.delivery_id.strip():
            raise WebhookPayloadError("delivery_id (X-Razorpay-Event-Id) is required")
        if not isinstance(self.event_type, str) or not self.event_type.strip():
            raise WebhookPayloadError("webhook event type is required")
        if not isinstance(self.payment_id, str) or not self.payment_id.strip():
            raise WebhookPayloadError(
                "payment.failed event must carry a payment id"
            )
        if not isinstance(self.order_id, str) or not self.order_id.strip():
            raise WebhookPayloadError(
                "payment.failed event must carry an order id"
            )
        for name in ("customer_id", "currency", "payment_method", "failure_reason", "bank", "failed_at"):
            value = getattr(self, name)
            if value is not None and (
                not isinstance(value, str) or not value.strip()
            ):
                raise WebhookPayloadError(
                    f"{name} must be None or a non-empty string"
                )
        if self.amount_paise is not None and not isinstance(self.amount_paise, int):
            raise WebhookPayloadError("amount_paise must be None or an integer")


def _validate_failed_shape(payload: dict[str, Any]) -> None:
    """Strictly validate the shape of a ``payment.failed`` event.

    A genuinely failed payment is the input to the recovery loop, so its shape
    must be complete enough to build a ``PaymentEvent``: it must carry a real
    payment entity whose status is ``failed``. Any structural gap raises
    ``WebhookPayloadError`` (mapped to a 4xx) so an under-specified failure is
    never silently turned into a fabricated event.
    """
    payload_obj = _child_dict(payload, "payload", "payload")
    payment_obj = _child_dict(payload_obj, "payment", "payload.payment")
    payment_entity = _child_dict(payment_obj, "entity", "payload.payment.entity")
    payment_id = payment_entity.get("id")
    if not isinstance(payment_id, str) or not payment_id.strip():
        raise WebhookPayloadError(
            "payment.failed event is missing a payment id"
        )
    status = payment_entity.get("status")
    if status != "failed":
        raise WebhookPayloadError(
            f"payment.failed event must have status 'failed', got {status!r}"
        )


def parse_payment_failed_payload(raw_body: bytes, delivery_id: str) -> FailedPaymentEvent:
    """Parse and validate a verified ``payment.failed`` webhook raw body.

    Assumes the signature has already been verified. Raises
    ``WebhookPayloadError`` for malformed/unsupported shape. This is the
    INGESTION path: it produces the fields for a ``PaymentEvent`` and never
    records an outcome or authorizes anything.
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
    if event_type != INGESTION_WEBHOOK_EVENT:
        raise WebhookPayloadError(
            f"expected {INGESTION_WEBHOOK_EVENT!r}, got {event_type!r}"
        )
    _validate_failed_shape(data)

    payload_obj = _child_dict(data, "payload", "payload")
    payment_obj = _child_dict(payload_obj, "payment", "payload.payment")
    payment_entity = _child_dict(payment_obj, "entity", "payload.payment.entity")

    payment_id = payment_entity.get("id")
    order_id = payment_entity.get("order_id") or ""
    customer_id = payment_entity.get("customer_id")
    amount = payment_entity.get("amount")
    currency = payment_entity.get("currency")
    method = payment_entity.get("method")
    error_code = payment_entity.get("error_code")
    error_desc = payment_entity.get("error_description")
    created_at = payment_entity.get("created_at")

    # Razorpay reports created_at as a Unix epoch (seconds). The locked
    # PaymentEvent contract needs an ISO8601 date-time string, so convert the
    # epoch to one. A missing or non-integer created_at falls back to None and
    # the mapper supplies a deterministic placeholder rather than inventing a
    # wall-clock time.
    failed_at: str | None = None
    if isinstance(created_at, int) and not isinstance(created_at, bool):
        failed_at = datetime.fromtimestamp(
            created_at, tz=timezone.utc
        ).isoformat()

    # Derive a decision-time failure_reason from Razorpay's provider-reported
    # error code/description. This is never fabricated — it comes from the
    # signed payload. If neither is present we fall back to a generic reason.
    failure_reason: str | None = None
    if isinstance(error_code, str) and error_code.strip():
        failure_reason = error_code.strip()
    elif isinstance(error_desc, str) and error_desc.strip():
        failure_reason = error_desc.strip()
    if not failure_reason:
        failure_reason = "payment_failed"

    return FailedPaymentEvent(
        delivery_id=delivery_id,
        event_type=event_type,
        payment_id=payment_id,
        order_id=order_id,
        customer_id=customer_id,
        amount_paise=amount,
        currency=currency,
        payment_method=method,
        failure_reason=failure_reason,
        bank=None,
        failed_at=failed_at,
    )

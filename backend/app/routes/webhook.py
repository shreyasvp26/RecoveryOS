"""Razorpay webhook HTTP boundary (closed-loop OUTCOME + INGESTION channels).

Phase 12: receives verified Razorpay delivery events for Payment Links (the
OUTCOME channel). Later: receives verified ``payment.failed`` deliveries as
the INGESTION channel that feeds genuinely failed payments into the recovery
loop. Both channels SHARE the raw-body HMAC-SHA256 signature gate and NEVER
grant the LLM authority, but they are processed with distinct semantics:

  * ``payment_link.paid`` (OUTCOME) — never creates Payment Links, never calls
    the executor, policy engine, selector, or any intervention. It verifies the
    signature over the exact raw request body, parses the payload, and hands it
    onward for idempotent/correlated recovery record.
  * ``payment.failed`` (INGESTION) — verified signature over the exact raw
    request body, then maps the genuinely failed payment to a PaymentEvent and
    ingests it so the existing detect->diagnose->policy->optimize loop can run.
    It never authorizes or executes anything on its own.

Routes hold no business logic and no SQL; they only wire HTTP to the webhook
boundary and service.
"""

from __future__ import annotations

import logging
import sqlite3
from collections.abc import Iterator
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Request, status
from fastapi.responses import JSONResponse

from ..config import get_razorpay_webhook_secret
from ..db import connect_database, init_db
from ..razorpay_webhook import (
    INGESTION_WEBHOOK_EVENT,
    WebhookPayloadError,
    WebhookSignatureError,
    parse_payment_failed_payload,
    parse_webhook_payload,
    require_valid_signature,
)
from ..webhook_service import (
    S_CONFLICT,
    S_PERSISTENCE_FAILURE,
    process_payment_failed,
    process_webhook,
)

router = APIRouter(tags=["webhook"])

logger = logging.getLogger("uvicorn.webhook")

_SIGNATURE_HEADER = "X-Razorpay-Signature"
_DELIVERY_ID_HEADER = "X-Razorpay-Event-Id"


def get_db() -> Iterator[sqlite3.Connection]:
    """FastAPI dependency: provide a connection to the configured SQLite DB."""
    conn = connect_database()
    init_db(conn)
    try:
        yield conn
    finally:
        conn.close()


def _map_http_result(result, delivery_id: str) -> JSONResponse:
    """Map a processing result's status to an HTTP response for the shared path."""
    if result.status == S_CONFLICT:
        return JSONResponse(
            status_code=status.HTTP_409_CONFLICT,
            content=result.to_dict(),
        )
    if result.status == S_PERSISTENCE_FAILURE:
        return JSONResponse(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            content=result.to_dict(),
        )
    # S_DEDUPLICATED, S_IGNORED, S_UNMATCHED, S_PROCESSED, S_INGESTED,
    # S_DUPLICATE_EVENT are 2xx.
    return JSONResponse(status_code=status.HTTP_200_OK, content=result.to_dict())


@router.post("/webhook/razorpay")
async def razorpay_webhook(
    request: Request,
    conn: sqlite3.Connection = Depends(get_db),
) -> JSONResponse:
    """Verify, parse, and acknowledge a Razorpay webhook delivery.

    Signature verification runs against the exact raw request body BEFORE any
    payload parsing; a missing or invalid signature is rejected with a 4xx and
    the body is never trusted. Verified deliveries are routed by event type:

      * ``payment_link.paid`` (OUTCOME) — parsed for the closed-loop recovery
        channel and processed idempotently/correlated.
      * ``payment.failed`` (INGESTION) — parsed and mapped to an ingested
        PaymentEvent so the recovery loop can run on a genuinely failed payment.
      * any other event — acknowledged (2xx) and ignored; never executed.
    """
    raw_body = await request.body()
    signature = request.headers.get(_SIGNATURE_HEADER, "")
    delivery_id = request.headers.get(_DELIVERY_ID_HEADER, "")
    secret = get_razorpay_webhook_secret()

    logger.info(
        "webhook delivery received: delivery_id=%s signature_present=%s bytes=%d",
        delivery_id,
        bool(signature),
        len(raw_body),
    )

    if not signature:
        return JSONResponse(
            status_code=status.HTTP_400_BAD_REQUEST,
            content={"status": "missing_signature", "delivery_id": delivery_id},
        )
    try:
        require_valid_signature(raw_body, signature, secret)
    except WebhookSignatureError as exc:
        # Rejection happens before parsing: an unauthenticated body is never
        # trusted or processed. Includes the delivery_id so operators can
        # correlate with Razorpay logs when troubleshooting.
        return JSONResponse(
            status_code=status.HTTP_401_UNAUTHORIZED,
            content={
                "status": "invalid_signature",
                "delivery_id": delivery_id,
                "detail": str(exc),
            },
        )

    # Signature verified against the exact raw body — the payload is now
    # trusted and may be parsed.
    try:
        parsed = parse_webhook_payload(raw_body, delivery_id)
        event_type = parsed.event_type
        if event_type == INGESTION_WEBHOOK_EVENT:
            failed = parse_payment_failed_payload(raw_body, delivery_id)
            result = process_payment_failed(
                conn, failed, raw_body, datetime.now(timezone.utc).isoformat()
            )
        else:
            result = process_webhook(
                conn, parsed, raw_body, datetime.now(timezone.utc).isoformat()
            )
        logger.info(
            "webhook processed: delivery_id=%s event_type=%s status=%s",
            delivery_id,
            event_type,
            result.status,
        )
        return _map_http_result(result, delivery_id)
    except WebhookPayloadError as exc:
        return JSONResponse(
            status_code=status.HTTP_400_BAD_REQUEST,
            content={
                "status": "invalid_payload",
                "delivery_id": delivery_id,
                "detail": str(exc),
            },
        )

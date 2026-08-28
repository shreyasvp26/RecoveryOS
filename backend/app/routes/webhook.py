"""Razorpay webhook HTTP boundary (closed-loop OUTCOME channel).

Phase 12: receives verified Razorpay delivery events for Payment Links. This
route is an OUTCOME channel and is architecturally separate from the execution
channel: it NEVER creates Payment Links, never calls the executor, policy
engine, selector, or any intervention. It verifies the HMAC-SHA256 signature
over the exact raw request body, parses the payload, and hands the verified
event onward for idempotent/correlated handling. Routes hold no business logic
and no SQL; they only wire HTTP to the webhook boundary and service.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Request, status
from fastapi.responses import JSONResponse

from ..config import get_razorpay_webhook_secret
from ..db import connect_database, init_db
from ..razorpay_webhook import (
    WebhookPayloadError,
    WebhookSignatureError,
    parse_webhook_payload,
    require_valid_signature,
)
from ..webhook_service import (
    S_CONFLICT,
    S_DEDUPLICATED,
    S_IGNORED,
    S_PERSISTENCE_FAILURE,
    S_UNMATCHED,
    S_VALID,
    process_webhook,
)

router = APIRouter(tags=["webhook"])

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


@router.post("/webhook/razorpay")
async def razorpay_webhook(
    request: Request,
    conn: sqlite3.Connection = Depends(get_db),
) -> JSONResponse:
    """Verify, parse, and acknowledge a Razorpay webhook delivery.

    Signature verification runs against the exact raw request body BEFORE any
    payload parsing; a missing or invalid signature is rejected with a 4xx and
    the body is never trusted. Unsupported events are acknowledged (2xx) and
    ignored; supported ``payment_link.paid`` events are parsed for the closed
    loop. Persistence/idempotency/correlation are protected by the webhook
    service (later slices).
    """
    raw_body = await request.body()
    signature = request.headers.get(_SIGNATURE_HEADER, "")
    delivery_id = request.headers.get(_DELIVERY_ID_HEADER, "")
    secret = get_razorpay_webhook_secret()

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
        event = parse_webhook_payload(raw_body, delivery_id)
    except WebhookPayloadError as exc:
        return JSONResponse(
            status_code=status.HTTP_400_BAD_REQUEST,
            content={
                "status": "invalid_payload",
                "delivery_id": delivery_id,
                "detail": str(exc),
            },
        )

    result = process_webhook(
        conn, event, raw_body, datetime.now(timezone.utc).isoformat()
    )

    # Map the closed-loop process status to an HTTP response. Duplicates are a
    # 2xx no-op (no double count), conflicts are explicit 409 (never
    # overwritten), unsupported events are acknowledged, persistence failures
    # surface as an error so Razorpay retries, and validated/known events are
    # acknowledged (correlation to a recovery/unmatched outcome happens in the
    # service and is surfaced by the same status contract).
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
    # S_VALID, S_DEDUPLICATED, S_IGNORED, S_UNMATCHED, S_PROCESSED are 2xx.
    return JSONResponse(status_code=status.HTTP_200_OK, content=result.to_dict())

"""Operator authentication boundary (hardening).

Every operator/data endpoint is protected by a single shared API key supplied
as ``Authorization: Bearer <key>``. The dependency:

  * reads the expected key from configuration at REQUEST time (never cached), so
    a key rotation takes effect immediately;
  * compares the presented token in constant time (``hmac.compare_digest``);
  * fails closed — an unconfigured key (HTTP 503) or a missing/invalid/absent
    credential (HTTP 401) is refused, never silently opened.

Public boundaries are deliberately excluded: ``/health``, ``/health/ready`` and
the signed Razorpay webhook endpoint keep their own, purpose-specific gates and
must not require an operator key.
"""

from __future__ import annotations

import hmac

from fastapi import Header, HTTPException, status

from .config import get_operator_api_key

_UNAUTHORIZED_CHALLENGE = {"WWW-Authenticate": "Bearer"}


def require_operator(
    authorization: str | None = Header(default=None),
) -> None:
    """FastAPI dependency: require a valid operator bearer credential."""
    expected = get_operator_api_key()
    if not expected:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=(
                "operator API key is not configured; operator endpoints are "
                "unavailable (fail-closed). Set RECOVERYOS_OPERATOR_API_KEY."
            ),
        )

    if not authorization:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="missing operator credentials",
            headers=_UNAUTHORIZED_CHALLENGE,
        )

    parts = authorization.split(None, 1)
    if len(parts) != 2 or parts[0].lower() != "bearer" or not parts[1].strip():
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="operator credentials must use the Bearer scheme",
            headers=_UNAUTHORIZED_CHALLENGE,
        )

    presented = parts[1].strip()
    if not hmac.compare_digest(presented.encode("utf-8"), expected.encode("utf-8")):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="invalid operator credentials",
            headers=_UNAUTHORIZED_CHALLENGE,
        )
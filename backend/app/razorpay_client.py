"""Razorpay Payment Link client boundary (Test Mode only).

Phase 7: isolates every Razorpay SDK interaction behind this single boundary.
The bounded executor calls this client; nothing else talks to the SDK. The
client creates Payment Links only through Razorpay TEST MODE credentials from
the environment. It never fabricates a Payment Link, never retries
unboundedly (no automatic retries at all), and maps every controlled failure —
missing configuration, API/network failure, or an unexpected response — to an
explicit error. The client answers only "did the provider-side operation
succeed?"; it never estimates revenue recovery.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


class RazorpayError(Exception):
    """Base class for all explicit Razorpay boundary failures."""


class RazorpayConfigurationError(RazorpayError):
    """Razorpay credentials are missing or otherwise unusable for execution."""


class RazorpayExecutionError(RazorpayError):
    """The Razorpay provider rejected or failed the Payment Link request."""


class RazorpayUnexpectedResponseError(RazorpayError):
    """The provider response did not contain the required Payment Link data."""


@dataclass(frozen=True)
class PaymentLinkResult:
    """A genuine Payment Link created provider-side in Razorpay Test Mode."""

    id: str
    short_url: str

    def __post_init__(self) -> None:
        for name in ("id", "short_url"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip():
                raise RazorpayUnexpectedResponseError(
                    f"payment link {name} must be a non-empty string"
                )


def reference_id_from(event_id: str) -> str:
    """Build a Razorpay-compatible unique reference from an event_id.

    Razorpay reference ids allow alphanumeric characters only (max 40); event
    ids contain underscores, so non-alphanumeric characters are stripped.
    """
    if not isinstance(event_id, str) or not event_id.strip():
        raise ValueError("event_id must be a non-empty string")
    reference = "".join(char for char in event_id if char.isalnum())[:40]
    if not reference:
        raise ValueError("event_id produced an empty razorpay reference id")
    return reference


# Razorpay key ids are prefixed by mode. Execution is Test Mode only; a live
# or unrecognized key id is rejected before any SDK call. Only the mode
# prefixes are referenced here — credentials themselves are never hardcoded.
_TEST_MODE_KEY_ID_PREFIX = "rzp_test_"
_LIVE_MODE_KEY_ID_PREFIX = "rzp_live_"


class RazorpayPaymentLinkClient:
    """The single boundary wrapping the Razorpay Python SDK (Test Mode only).

    The SDK is imported lazily so that importing this module never requires
    the razorpay package; tests inject a fake sdk object. No credentials are
    ever hardcoded. Construction enforces the Test Mode invariant:
    a missing, live (``rzp_live_``), or unrecognized key is rejected with an
    explicit ``RazorpayConfigurationError``, so REAL_RAZORPAY execution can
    never run against the production Razorpay environment.
    """

    def __init__(
        self,
        key_id: str,
        key_secret: str,
        sdk: Any = None,
    ) -> None:
        if not key_id or not key_secret:
            raise RazorpayConfigurationError(
                "razorpay credentials are not configured; cannot execute "
                "payment_link in REAL_RAZORPAY mode"
            )
        if key_id.startswith(_LIVE_MODE_KEY_ID_PREFIX):
            raise RazorpayConfigurationError(
                "rzp_live_ credentials are forbidden: execution is Razorpay "
                "TEST MODE only and live-key Payment Links are never created"
            )
        if not key_id.startswith(_TEST_MODE_KEY_ID_PREFIX):
            raise RazorpayConfigurationError(
                "unrecognized razorpay key id: only Razorpay Test Mode "
                "key ids starting with 'rzp_test_' are permitted"
            )
        if sdk is None:
            import razorpay  # lazy so imports/tests never require the SDK

            sdk = razorpay.Client(auth=(key_id, key_secret))
        self._sdk = sdk

    def create_payment_link(
        self,
        *,
        amount_paise: int,
        currency: str,
        reference_id: str,
        description: str,
    ) -> PaymentLinkResult:
        """Create a Payment Link in Razorpay Test Mode and return its details.

        A failure — invalid request, authentication failure, provider
        rejection, network error, or an unexpected response — always surfaces
        as an explicit RazorpayError and never as a fabricated success.
        """
        if not isinstance(amount_paise, int) or isinstance(amount_paise, bool):
            raise RazorpayExecutionError("amount_paise must be an integer")
        if amount_paise < 0:
            raise RazorpayExecutionError("amount_paise must be non-negative")
        for name, value in {
            "currency": currency,
            "reference_id": reference_id,
            "description": description,
        }.items():
            if not isinstance(value, str) or not value.strip():
                raise RazorpayExecutionError(f"{name} must be a non-empty string")

        data: dict[str, Any] = {
            "amount": amount_paise,
            "currency": currency,
            "reference_id": reference_id,
            "description": description,
        }
        try:
            response = self._sdk.payment_link.create(data)
        except RazorpayError:
            raise
        except Exception as exc:
            raise RazorpayExecutionError(f"razorpay_api_error: {exc}") from exc

        if not isinstance(response, dict):
            raise RazorpayUnexpectedResponseError(
                "razorpay_api_unexpected_response: response was not an object"
            )
        link_id = response.get("id")
        short_url = response.get("short_url")
        if not isinstance(link_id, str) or not link_id.strip():
            raise RazorpayUnexpectedResponseError(
                "razorpay_api_unexpected_response: payment link id missing"
            )
        if not isinstance(short_url, str) or not short_url.strip():
            raise RazorpayUnexpectedResponseError(
                "razorpay_api_unexpected_response: payment link short_url missing"
            )
        # The URL is returned exactly as provided by Razorpay; it is never
        # generated, guessed, or cached on the client side.
        return PaymentLinkResult(id=link_id, short_url=short_url)

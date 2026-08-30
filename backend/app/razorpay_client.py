"""Razorpay Payment Link client boundary (Test Mode only).

Phase 7: isolates every Razorpay SDK interaction behind this single boundary.
The bounded executor calls this client; nothing else talks to the SDK. The
client creates Payment Links only through Razorpay TEST MODE credentials from
the environment. It never fabricates a Payment Link, never retries
unboundedly (no automatic retries at all), and maps every controlled failure —
missing configuration, API/network failure, or an unexpected response — to an
explicit error. The client answers only "did the provider-side operation
succeed?"; it never estimates revenue recovery.

A failure is additionally classified on the only axis that can duplicate real
money movement: a proven provider refusal (no Payment Link exists) versus a
result RecoveryOS cannot determine (a Payment Link may exist). The caller uses
that distinction to decide whether the action may be attempted again.
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


# Marks a failure after which RecoveryOS cannot say whether the provider
# performed the side effect. It is carried in the error message (and therefore
# in the ExecutionOutcome detail, which is how this codebase already conveys
# machine-readable failure identifiers such as "configuration_missing") so the
# distinction survives persistence without a schema change.
PROVIDER_RESULT_UNKNOWN = "provider_result_unknown"


class RazorpayResultUnknownError(RazorpayExecutionError):
    """The request may have reached the provider and its result is unknown.

    A subclass of RazorpayExecutionError so every existing caller keeps
    treating it as an explicit provider failure; what it adds is the admission
    that a Payment Link may nevertheless exist. Retrying such a request could
    create a second real one, so the caller must not release its claim.
    """

    def __init__(self, message: str = "") -> None:
        text = str(message).strip()
        if not text:
            text = PROVIDER_RESULT_UNKNOWN
        elif not text.startswith(PROVIDER_RESULT_UNKNOWN):
            text = f"{PROVIDER_RESULT_UNKNOWN}: {text}"
        super().__init__(text)


class RazorpayUnexpectedResponseError(RazorpayResultUnknownError):
    """The provider response did not contain the required Payment Link data.

    The provider answered, so it may well have created the link; RecoveryOS
    just cannot read which one. That is an unknown result, not a rejection.
    """


def marks_provider_result_unknown(detail: str | None) -> bool:
    """Whether a persisted failure detail records an unknown provider result."""
    return isinstance(detail, str) and detail.strip().startswith(
        PROVIDER_RESULT_UNKNOWN
    )


def _is_definitive_provider_rejection(exc: BaseException) -> bool:
    """Whether the provider is known to have refused the request.

    The Razorpay SDK raises ``BadRequestError`` only after receiving an HTTP
    response carrying Razorpay's own BAD_REQUEST_ERROR code: the request was
    evaluated and refused, so no Payment Link exists. Every other SDK error
    (GatewayError, ServerError) and every transport error (requests'
    ConnectionError/Timeout, which the SDK re-raises unchanged) leaves open the
    possibility that the request was processed. The class is matched by name
    across the MRO so this module still never imports the SDK eagerly.
    """
    return any(
        klass.__name__ == "BadRequestError"
        and klass.__module__.split(".")[0] == "razorpay"
        for klass in type(exc).__mro__
    )


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


@dataclass(frozen=True)
class PaymentLinkStatus:
    """The provider-observed status of one Payment Link (Test Mode read).

    Phase 23: the boundary answers only "what did the provider say this link's
    status is?". It does not interpret the status into a recovery outcome — that
    mapping lives in the calibration module where the terminal contract is
    owned. ``status`` is the raw Razorpay status string (e.g. ``paid``,
    ``expired``, ``created``, ``partially_paid``, ``cancelled``); ``link_id``
    echoes the requested id so a caller can never misattribute a response.
    """

    link_id: str
    status: str

    def __post_init__(self) -> None:
        for name in ("link_id", "status"):
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
            # Control the externally visible detail: a stable identifier, never
            # arbitrary provider exception text (which must not become a
            # user/audit-facing data channel). The original exception is kept on
            # the chain (from exc) for internal debugging, but its text is not
            # surfaced in the message.
            #
            # Fail conservatively on the one axis that moves money: only a
            # proven refusal is reported as a plain failure. Anything else —
            # timeout, connection reset, 5xx, an exception this boundary does
            # not recognize — may have created a real Payment Link.
            if _is_definitive_provider_rejection(exc):
                raise RazorpayExecutionError("razorpay_api_error") from exc
            raise RazorpayResultUnknownError("razorpay_api_error") from exc

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

    def get_payment_link(self, link_id: str) -> PaymentLinkStatus:
        """Read the provider-observed status of one Payment Link (Test Mode).

        Phase 23 evidence read. This is a STRICTLY read-only provider call: it
        creates nothing, changes nothing provider-side, and its result is only
        ever used to observe what happened to a link RecoveryOS already created.
        It never authorizes and never executes an intervention.

        A failure — invalid id, network error, provider rejection, or an
        unexpected response — surfaces as an explicit ``RazorpayError`` and
        never as a fabricated status. The caller is responsible for mapping any
        raised error to an unknown outcome.
        """
        if not isinstance(link_id, str) or not link_id.strip():
            raise RazorpayExecutionError("payment link id must be a non-empty string")

        try:
            response = self._sdk.payment_link.fetch(link_id)
        except RazorpayError:
            raise
        except Exception as exc:
            # Fail conservatively on the same axis as creation: only a proven
            # refusal is a plain failure; anything unreadable is an unknown
            # provider result, because the link may exist in a state RecoveryOS
            # could not read.
            if _is_definitive_provider_rejection(exc):
                raise RazorpayExecutionError("razorpay_api_error") from exc
            raise RazorpayResultUnknownError("razorpay_api_error") from exc

        if not isinstance(response, dict):
            raise RazorpayUnexpectedResponseError(
                "razorpay_api_unexpected_response: response was not an object"
            )
        status = response.get("status")
        if not isinstance(status, str) or not status.strip():
            raise RazorpayUnexpectedResponseError(
                "razorpay_api_unexpected_response: payment link status missing"
            )
        # The id in the response is never trusted over the requested id: the
        # request names the link, and the response is attributed to it.
        return PaymentLinkStatus(link_id=link_id, status=status)

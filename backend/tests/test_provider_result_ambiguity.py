"""Phase 21 hardening: a Payment Link attempt whose result RecoveryOS cannot read.

The dangerous case is not a provider that says no. It is a provider that may
have said yes into a socket that died: Razorpay creates the Payment Link, the
response never arrives, and RecoveryOS is left holding a failure it cannot
attribute. Recording that as a plain FAILED and releasing the claim would let
the next click create a SECOND real Payment Link for the same payment.

These tests pin the distinction the boundary now makes:

    proven refusal        -> FAILED, claim released, retry still possible
    unreadable result     -> FAILED, claim retained, no second attempt

and prove that the retained claim is what actually stops the duplicate, by
counting side effects on the provider side of the boundary.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
import requests
from razorpay.errors import BadRequestError, GatewayError, ServerError

from app.classification import ClassificationResult
from app.db import (
    get_execution_claim,
    insert_classification_result,
    insert_payment_event,
)
from app.execution_service import (
    CLAIM_STATUS_PROVIDER_RESULT_UNKNOWN,
    STATUS_EXECUTION_FAILED,
    STATUS_EXECUTION_SUCCESS,
    STATUS_PROVIDER_RESULT_UNKNOWN,
    execute_event,
)
from app.models import PaymentEvent
from app.policy import PolicyConfig
from app.razorpay_client import (
    PROVIDER_RESULT_UNKNOWN,
    RazorpayExecutionError,
    RazorpayPaymentLinkClient,
    RazorpayResultUnknownError,
    RazorpayUnexpectedResponseError,
    marks_provider_result_unknown,
)
from app.recovery_operations import (
    STATE_FAILED,
    STATE_PROVIDER_RESULT_UNKNOWN,
    build_queue_row_for_event,
)

NOW = datetime(2026, 8, 29, 9, 0, tzinfo=timezone.utc)
CONFIG = PolicyConfig()

TEST_MODE_KEY_ID = "rzp_test_abcdefghijklmn"
TEST_MODE_KEY_SECRET = "test_key_secret"


def _event(event_id: str = "evt_ambiguous") -> PaymentEvent:
    return PaymentEvent.from_dict(
        {
            "event_id": event_id,
            "order_id": f"order_{event_id}",
            "payment_id": f"pay_{event_id}",
            "customer_id": f"cust_{event_id}",
            "amount_paise": 90_000,
            "currency": "INR",
            "payment_method": "card",
            "failure_reason": "bank_timeout",
            "bank": "HDFC",
            "risk_flag": "normal",
            "customer_history": {
                "prior_successful_payments": 3,
                "prior_failed_payments": 1,
                "has_active_subscription": True,
            },
            "timestamp": NOW.isoformat(),
        }
    )


def _seed(conn, event_id: str = "evt_ambiguous", candidates=("payment_link",)) -> None:
    insert_payment_event(conn, _event(event_id))
    insert_classification_result(
        conn,
        ClassificationResult(
            event_id=event_id,
            root_cause_category="transient",
            confidence=0.9,
            reasoning="transient bank timeout",
            candidate_interventions=tuple(candidates),
        ),
    )


class ProviderSideSdk:
    """A fake Razorpay SDK that records what happened on the PROVIDER side.

    ``created`` is the ground truth a real Razorpay account would hold. It is
    deliberately separate from what RecoveryOS learns, so a test can model "the
    link exists but the caller never found out".
    """

    def __init__(self, *, error: Exception | None = None, create_first: bool = False):
        self.created: list[str] = []
        self.calls: list[dict] = []
        self._error = error
        self._create_first = create_first

    @property
    def payment_link(self) -> "ProviderSideSdk":
        return self

    def create(self, data: dict) -> dict:
        self.calls.append(data)
        if self._create_first:
            # The provider-side side effect happens BEFORE the failure: this is
            # the response-lost-after-creation case.
            self.created.append(f"plink_real_{len(self.created)}")
        if self._error is not None:
            raise self._error
        index = len(self.created)
        self.created.append(f"plink_real_{index}")
        return {
            "id": f"plink_real_{index}",
            "short_url": f"https://rzp.io/l/real{index}",
        }


def _client(sdk: ProviderSideSdk) -> RazorpayPaymentLinkClient:
    return RazorpayPaymentLinkClient(TEST_MODE_KEY_ID, TEST_MODE_KEY_SECRET, sdk=sdk)


# ---------------------------------------------------------------------------
# The client boundary: which failures are provable, and which are not
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "error",
    [
        requests.exceptions.Timeout("read timed out"),
        requests.exceptions.ConnectionError("connection reset by peer"),
        requests.exceptions.ChunkedEncodingError("response truncated"),
        ServerError("we are having trouble"),
        GatewayError("gateway timeout"),
        RuntimeError("something this boundary has never seen"),
    ],
)
def test_a_failure_that_may_have_reached_razorpay_is_reported_as_unknown(error) -> None:
    sdk = ProviderSideSdk(error=error)
    with pytest.raises(RazorpayResultUnknownError) as exc_info:
        _client(sdk).create_payment_link(
            amount_paise=100, currency="INR", reference_id="evt1", description="d"
        )
    assert marks_provider_result_unknown(str(exc_info.value))
    # The stable identifier and the no-leak rule both survive the new prefix.
    assert "razorpay_api_error" in str(exc_info.value)
    assert "connection reset by peer" not in str(exc_info.value)


def test_an_authoritative_razorpay_rejection_stays_a_plain_failure() -> None:
    """BadRequestError means Razorpay read the request and refused it."""
    sdk = ProviderSideSdk(error=BadRequestError("amount must be at least INR 1"))
    with pytest.raises(RazorpayExecutionError) as exc_info:
        _client(sdk).create_payment_link(
            amount_paise=100, currency="INR", reference_id="evt2", description="d"
        )
    assert not isinstance(exc_info.value, RazorpayResultUnknownError)
    assert not marks_provider_result_unknown(str(exc_info.value))


def test_a_validation_failure_before_the_sdk_call_stays_a_plain_failure() -> None:
    """Nothing was sent, so nothing can exist provider-side."""
    sdk = ProviderSideSdk()
    with pytest.raises(RazorpayExecutionError) as exc_info:
        _client(sdk).create_payment_link(
            amount_paise=-1, currency="INR", reference_id="evt3", description="d"
        )
    assert not isinstance(exc_info.value, RazorpayResultUnknownError)
    assert sdk.calls == []


def test_an_unreadable_response_is_unknown_because_the_link_may_exist() -> None:
    sdk = ProviderSideSdk()
    sdk.create = lambda data: {"status": "created"}  # type: ignore[assignment]
    with pytest.raises(RazorpayUnexpectedResponseError) as exc_info:
        _client(sdk).create_payment_link(
            amount_paise=100, currency="INR", reference_id="evt4", description="d"
        )
    assert isinstance(exc_info.value, RazorpayResultUnknownError)
    assert marks_provider_result_unknown(str(exc_info.value))
    assert "razorpay_api_unexpected_response" in str(exc_info.value)


# ---------------------------------------------------------------------------
# TEST 1 / TEST 3 — the claim is what stops the second real Payment Link
# ---------------------------------------------------------------------------


def test_an_ambiguous_timeout_retains_the_claim_and_blocks_the_retry(db_conn) -> None:
    _seed(db_conn)
    sdk = ProviderSideSdk(error=requests.exceptions.Timeout("read timed out"))
    provider = _client(sdk)

    first = execute_event(db_conn, "evt_ambiguous", NOW, CONFIG, provider)
    assert first.status == STATUS_EXECUTION_FAILED
    assert first.outcome.status == "FAILED"
    assert marks_provider_result_unknown(first.outcome.detail)
    assert first.outcome.payment_link_id is None
    assert first.outcome.external_reference is None

    claim = get_execution_claim(db_conn, "evt_ambiguous", "payment_link")
    assert claim is not None
    assert claim["status"] == CLAIM_STATUS_PROVIDER_RESULT_UNKNOWN

    # A retry an hour later — a fresh evaluation time, so nothing else blocks it.
    second = execute_event(
        db_conn, "evt_ambiguous", NOW + timedelta(hours=1), CONFIG, provider
    )
    assert second.status == STATUS_PROVIDER_RESULT_UNKNOWN
    assert len(sdk.calls) == 1


def test_a_response_lost_after_creation_never_creates_a_second_link(db_conn) -> None:
    """The real failure mode: Razorpay created the link, RecoveryOS never knew.

    ``sdk.created`` is the provider-side ground truth. It must stay at one, no
    matter how many times the action is requested afterwards.
    """
    _seed(db_conn)
    sdk = ProviderSideSdk(
        error=requests.exceptions.ConnectionError("connection reset by peer"),
        create_first=True,
    )
    provider = _client(sdk)

    first = execute_event(db_conn, "evt_ambiguous", NOW, CONFIG, provider)
    assert len(sdk.created) == 1
    assert first.status == STATUS_EXECUTION_FAILED
    # RecoveryOS neither knows the link nor claims it does not exist.
    assert first.outcome.payment_link_id is None
    assert marks_provider_result_unknown(first.outcome.detail)

    for hour in (1, 2, 3):
        repeat = execute_event(
            db_conn, "evt_ambiguous", NOW + timedelta(hours=hour), CONFIG, provider
        )
        assert repeat.status == STATUS_PROVIDER_RESULT_UNKNOWN

    assert len(sdk.created) == 1, "a second real Payment Link was created"
    assert len(sdk.calls) == 1


# ---------------------------------------------------------------------------
# TEST 2 / TEST 6 — known failures keep their Phase 11 retry semantics
# ---------------------------------------------------------------------------


def test_a_rejected_request_releases_the_claim_and_can_be_retried(db_conn) -> None:
    _seed(db_conn)
    rejecting = _client(ProviderSideSdk(error=BadRequestError("amount is invalid")))
    first = execute_event(db_conn, "evt_ambiguous", NOW, CONFIG, rejecting)
    assert first.status == STATUS_EXECUTION_FAILED
    assert not marks_provider_result_unknown(first.outcome.detail)
    assert get_execution_claim(db_conn, "evt_ambiguous", "payment_link") is None

    working_sdk = ProviderSideSdk()
    retry = execute_event(
        db_conn, "evt_ambiguous", NOW + timedelta(hours=1), CONFIG, _client(working_sdk)
    )
    assert retry.status == STATUS_EXECUTION_SUCCESS
    assert len(working_sdk.created) == 1


def test_a_known_simulated_failure_keeps_its_existing_retry_semantics(db_conn) -> None:
    """Simulated execution never touches a provider, so it is never ambiguous."""
    _seed(db_conn, candidates=("reminder",))
    result = execute_event(db_conn, "evt_ambiguous", NOW, CONFIG, razorpay_client=None)
    assert result.status == STATUS_EXECUTION_SUCCESS
    assert result.outcome.execution_mode == "SIMULATED"

    # The one simulated path that can fail is a missing provider configuration
    # for payment_link, which is proven to have contacted nobody.
    _seed(db_conn, event_id="evt_unconfigured", candidates=("payment_link",))
    failed = execute_event(db_conn, "evt_unconfigured", NOW, CONFIG, razorpay_client=None)
    assert failed.status == STATUS_EXECUTION_FAILED
    assert not marks_provider_result_unknown(failed.outcome.detail)
    assert get_execution_claim(db_conn, "evt_unconfigured", "payment_link") is None


# ---------------------------------------------------------------------------
# What the operator is shown
# ---------------------------------------------------------------------------


def test_the_queue_row_reports_uncertainty_and_offers_no_execution(db_conn) -> None:
    _seed(db_conn)
    sdk = ProviderSideSdk(
        error=requests.exceptions.Timeout("read timed out"), create_first=True
    )
    execute_event(db_conn, "evt_ambiguous", NOW, CONFIG, _client(sdk))

    row = build_queue_row_for_event(db_conn, "evt_ambiguous")
    assert row["outcome"]["state"] == STATE_PROVIDER_RESULT_UNKNOWN
    assert row["outcome"]["recovered_amount_paise"] is None
    assert row["lifecycle_state"] == STATE_FAILED
    assert row["actionable"] is False
    assert "may exist" in row["outcome"]["note"]


def test_a_plain_provider_rejection_remains_actionable_in_the_queue(db_conn) -> None:
    _seed(db_conn)
    execute_event(
        db_conn,
        "evt_ambiguous",
        NOW,
        CONFIG,
        _client(ProviderSideSdk(error=BadRequestError("rejected"))),
    )
    row = build_queue_row_for_event(db_conn, "evt_ambiguous")
    assert row["outcome"]["state"] == STATE_FAILED
    assert row["lifecycle_state"] == STATE_FAILED
    assert row["actionable"] is True


# ---------------------------------------------------------------------------
# TEST 8 — a later webhook, and the honest limit of what can be correlated
# ---------------------------------------------------------------------------


def test_a_webhook_for_an_unknown_link_is_unmatched_and_recovers_nothing(db_conn) -> None:
    """The documented limitation, asserted rather than assumed.

    Phase 12 correlates a paid webhook to an execution outcome BY payment link
    id. An ambiguous attempt never learned the id, so a webhook for the link
    Razorpay really created cannot be attached to this event. RecoveryOS records
    it as unmatched and fabricates no recovery — it does not guess.
    """
    from app.razorpay_webhook import WebhookEvent
    from app.webhook_service import S_UNMATCHED, process_webhook

    _seed(db_conn)
    sdk = ProviderSideSdk(
        error=requests.exceptions.ConnectionError("reset"), create_first=True
    )
    execute_event(db_conn, "evt_ambiguous", NOW, CONFIG, _client(sdk))
    orphan_link_id = sdk.created[0]

    result = process_webhook(
        db_conn,
        WebhookEvent(
            delivery_id="dlv_orphan",
            event_type="payment_link.paid",
            payment_link_id=orphan_link_id,
            payment_link_status="paid",
            amount_paid_paise=90_000,
            currency="INR",
            payment_id="pay_orphan",
            reference_id="evtambiguous",
        ),
        raw_body=b'{"event":"payment_link.paid"}',
        received_at=NOW.isoformat(),
    )
    assert result.status == S_UNMATCHED

    row = build_queue_row_for_event(db_conn, "evt_ambiguous")
    assert row["outcome"]["state"] == STATE_PROVIDER_RESULT_UNKNOWN
    assert row["outcome"]["recovered_amount_paise"] is None


def test_the_unknown_marker_is_a_single_stable_identifier() -> None:
    assert PROVIDER_RESULT_UNKNOWN == "provider_result_unknown"
    assert marks_provider_result_unknown("provider_result_unknown: razorpay_api_error")
    assert not marks_provider_result_unknown("razorpay_api_error")
    assert not marks_provider_result_unknown(None)

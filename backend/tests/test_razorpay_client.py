"""Phase 7 Razorpay client boundary tests (mocked SDK, no live credentials)."""

from __future__ import annotations

from typing import Any

import pytest

from app.razorpay_client import (
    PaymentLinkResult,
    RazorpayConfigurationError,
    RazorpayExecutionError,
    RazorpayPaymentLinkClient,
    RazorpayUnexpectedResponseError,
    reference_id_from,
)


class FakeSdk:
    """A stand-in for razorpay.Client whose behavior is fully controlled."""

    def __init__(self, responses: list[Any] | None = None, raises: Exception | None = None):
        self.responses = list(responses or [])
        self.raises = raises
        self.calls: list[dict[str, Any]] = []

    @property
    def payment_link(self) -> "FakeSdk":
        return self

    def create(self, data: dict[str, Any]) -> Any:
        self.calls.append(data)
        if self.raises is not None:
            raise self.raises
        if self.responses:
            response = self.responses.pop(0)
            if response is not ...:
                return response
        return self.responses

    def __call__(self, data: dict[str, Any]) -> Any:
        return self.create(data)


TEST_MODE_KEY_ID = "rzp_test_abcdefghijklmn"
TEST_MODE_KEY_SECRET = "test_key_secret"


def _client(sdk: FakeSdk) -> RazorpayPaymentLinkClient:
    return RazorpayPaymentLinkClient(TEST_MODE_KEY_ID, TEST_MODE_KEY_SECRET, sdk=sdk)


def test_valid_test_mode_key_id_is_accepted() -> None:
    client = RazorpayPaymentLinkClient(
        "rzp_test_0123456789abcdef", "secret", sdk=FakeSdk(responses=[VALID_RESPONSE])
    )
    result = client.create_payment_link(
        amount_paise=100, currency="INR", reference_id="evt0", description="d"
    )
    assert isinstance(result, PaymentLinkResult)
    assert result.id == "plink_XYZ123"


def test_live_rzp_key_id_is_explicitly_rejected() -> None:
    with pytest.raises(RazorpayConfigurationError) as exc_info:
        RazorpayPaymentLinkClient("rzp_live_9876543210abcdef", "some_secret")
    assert "rzp_live_" in str(exc_info.value)
    assert "TEST MODE" in str(exc_info.value)


def test_non_test_mode_key_id_is_rejected() -> None:
    with pytest.raises(RazorpayConfigurationError):
        RazorpayPaymentLinkClient("key_id", "key_secret")


def test_missing_credentials_remain_an_explicit_configuration_failure() -> None:
    with pytest.raises(RazorpayConfigurationError):
        RazorpayPaymentLinkClient("", "")
    with pytest.raises(RazorpayConfigurationError):
        RazorpayPaymentLinkClient(TEST_MODE_KEY_ID, "")
    with pytest.raises(RazorpayConfigurationError):
        RazorpayPaymentLinkClient("", TEST_MODE_KEY_SECRET)


VALID_RESPONSE = {
    "id": "plink_XYZ123",
    "short_url": "https://rzp.io/l/abc123",
    "status": "created",
    "amount": 75000,
    "currency": "INR",
}


def test_successful_payment_link_creation() -> None:
    sdk = FakeSdk(responses=[VALID_RESPONSE])
    client = _client(sdk)
    result = client.create_payment_link(
        amount_paise=75000,
        currency="INR",
        reference_id="evt1",
        description="RecoveryOS payment link for order order_exec",
    )
    assert isinstance(result, PaymentLinkResult)
    assert result.id == "plink_XYZ123"
    assert result.short_url == "https://rzp.io/l/abc123"
    assert sdk.calls[0] == {
        "amount": 75000,
        "currency": "INR",
        "reference_id": "evt1",
        "description": "RecoveryOS payment link for order order_exec",
    }


def test_no_fabricated_url_on_success() -> None:
    sdk = FakeSdk(responses=[VALID_RESPONSE])
    client = _client(sdk)
    result = client.create_payment_link(
        amount_paise=100, currency="INR", reference_id="evt2", description="d"
    )
    assert result.short_url == "https://rzp.io/l/abc123"


def test_razorpay_api_failure_is_explicit() -> None:
    sdk = FakeSdk(raises=RuntimeError("connection reset"))
    client = _client(sdk)
    with pytest.raises(RazorpayExecutionError) as exc_info:
        client.create_payment_link(
            amount_paise=100, currency="INR", reference_id="evt3", description="d"
        )
    assert "razorpay_api_error" in str(exc_info.value)
    assert not sdk.calls[-1].get("fake_url")


def test_unexpected_response_not_an_object() -> None:
    sdk = FakeSdk(responses=[None])
    with pytest.raises(RazorpayUnexpectedResponseError):
        _client(sdk).create_payment_link(
            amount_paise=100, currency="INR", reference_id="evt4", description="d"
        )


def test_unexpected_response_missing_short_url() -> None:
    sdk = FakeSdk(responses=[{"id": "plink_X", "status": "created"}])
    with pytest.raises(RazorpayUnexpectedResponseError):
        _client(sdk).create_payment_link(
            amount_paise=100, currency="INR", reference_id="evt5", description="d"
        )


def test_unexpected_response_empty_short_url() -> None:
    sdk = FakeSdk(responses=[{"id": "plink_X", "short_url": ""}])
    with pytest.raises(RazorpayUnexpectedResponseError):
        _client(sdk).create_payment_link(
            amount_paise=100, currency="INR", reference_id="evt6", description="d"
        )


def test_unexpected_response_missing_id_no_fabricated_payment_link_id() -> None:
    sdk = FakeSdk(responses=[{"short_url": "https://rzp.io/l/abc", "status": "created"}])
    with pytest.raises(RazorpayUnexpectedResponseError):
        _client(sdk).create_payment_link(
            amount_paise=100, currency="INR", reference_id="evt6", description="d"
        )


def test_unexpected_response_empty_id_no_fabricated_payment_link_id() -> None:
    sdk = FakeSdk(responses=[{"id": "", "short_url": "https://rzp.io/l/abc"}])
    with pytest.raises(RazorpayUnexpectedResponseError):
        _client(sdk).create_payment_link(
            amount_paise=100, currency="INR", reference_id="evt6", description="d"
        )


def test_failed_provider_operation_never_returns_success() -> None:
    sdk = FakeSdk(raises=Exception("provider rejected"))
    with pytest.raises(RazorpayExecutionError):
        _client(sdk).create_payment_link(
            amount_paise=100, currency="INR", reference_id="evt7", description="d"
        )


def test_reference_id_sanitized_for_razorpay() -> None:
    assert reference_id_from("evt_policy_1") == "evtpolicy1"
    assert reference_id_from("evt_policy_1").isalnum()
    long_id = "evt_" + "x" * 60
    assert len(reference_id_from(long_id)) == 40
    with pytest.raises(ValueError):
        reference_id_from("_")

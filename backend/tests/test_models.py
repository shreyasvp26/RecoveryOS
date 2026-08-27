"""Domain model validation tests for the locked PaymentEvent contract."""

from __future__ import annotations

import pytest

from app.models import CustomerHistory, PaymentEvent


def valid_event(**overrides) -> dict:
    base = {
        "event_id": "evt_001",
        "order_id": "order_001",
        "payment_id": "pay_001",
        "customer_id": "cust_001",
        "amount_paise": 499900,
        "currency": "INR",
        "payment_method": "upi",
        "failure_reason": "bank_timeout",
        "bank": "HDFC",
        "risk_flag": "normal",
        "customer_history": {
            "prior_successful_payments": 4,
            "prior_failed_payments": 1,
            "has_active_subscription": True,
        },
        "timestamp": "2026-08-27T12:00:00+00:00",
    }
    base.update(overrides)
    return base


def test_valid_payment_event_builds() -> None:
    event = PaymentEvent.from_dict(valid_event())
    assert event.event_id == "evt_001"
    assert isinstance(event.amount_paise, int)


@pytest.mark.parametrize(
    "field,value",
    [
        ("event_id", ""),
        ("order_id", "   "),
        ("payment_id", None),
        ("customer_id", ""),
    ],
)
def test_required_identifiers_must_be_non_empty(field, value) -> None:
    with pytest.raises(ValueError):
        PaymentEvent.from_dict(valid_event(**{field: value}))


@pytest.mark.parametrize(
    "value",
    [499900.0, "499900", True, -1],
)
def test_amount_paise_must_be_non_negative_integer(value) -> None:
    with pytest.raises(ValueError):
        PaymentEvent.from_dict(valid_event(amount_paise=value))


@pytest.mark.parametrize(
    "value",
    ["netflix", "crypto", "", None, "card "] ,
)
def test_payment_method_must_be_locked_set(value) -> None:
    with pytest.raises(ValueError):
        PaymentEvent.from_dict(valid_event(payment_method=value))


def test_valid_payment_methods_accepted() -> None:
    for method in ("upi", "card", "netbanking", "wallet"):
        event = PaymentEvent.from_dict(valid_event(payment_method=method))
        assert event.payment_method == method


@pytest.mark.parametrize(
    "value",
    ["risky", "fraud", "", None, "NORMAL"],
)
def test_risk_flag_must_be_locked_set(value) -> None:
    with pytest.raises(ValueError):
        PaymentEvent.from_dict(valid_event(risk_flag=value))


def test_valid_risk_flags_accepted() -> None:
    for flag in ("normal", "fraud_suspect"):
        event = PaymentEvent.from_dict(valid_event(risk_flag=flag))
        assert event.risk_flag == flag


@pytest.mark.parametrize(
    "value",
    ["not-a-date", "2026-13-99", "", None, "2026-08-27T25:00:00", "2026-08-27"],
)
def test_timestamp_must_be_valid_iso8601_date_time(value) -> None:
    with pytest.raises(ValueError):
        PaymentEvent.from_dict(valid_event(timestamp=value))


@pytest.mark.parametrize(
    "value",
    [
        "2026-08-27T12:00:00+00:00",
        "2026-08-27T12:34:56.123456+00:00",
    ],
)
def test_valid_iso8601_date_time_is_accepted(value) -> None:
    event = PaymentEvent.from_dict(valid_event(timestamp=value))
    assert event.timestamp == value


def test_customer_history_missing_field_rejected() -> None:
    history = {
        "prior_successful_payments": 4,
        "prior_failed_payments": 1,
    }
    with pytest.raises(ValueError):
        PaymentEvent.from_dict(valid_event(customer_history=history))


def test_customer_history_extra_field_rejected() -> None:
    history = {
        "prior_successful_payments": 4,
        "prior_failed_payments": 1,
        "has_active_subscription": True,
        "extra": "x",
    }
    with pytest.raises(ValueError):
        PaymentEvent.from_dict(valid_event(customer_history=history))


@pytest.mark.parametrize(
    "key,value",
    [
        ("prior_successful_payments", -1),
        ("prior_failed_payments", 1.5),
        ("has_active_subscription", "yes"),
    ],
)
def test_customer_history_value_types_validated(key, value) -> None:
    history = valid_event()["customer_history"]
    history[key] = value
    with pytest.raises(ValueError):
        PaymentEvent.from_dict(valid_event(customer_history=history))


def test_round_trip_via_to_dict_preserves_locked_contract() -> None:
    original = valid_event()
    event = PaymentEvent.from_dict(original)
    assert event.to_dict() == original


def test_unexpected_top_level_field_is_rejected() -> None:
    data = valid_event(extra_field="x")
    with pytest.raises(ValueError):
        PaymentEvent.from_dict(data)

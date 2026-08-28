"""Phase 12 webhook boundary & security tests (raw body + signature)."""

from __future__ import annotations

import hashlib
import hmac
import json

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.razorpay_webhook import (
    WebhookPayloadError,
    WebhookSignatureError,
    parse_webhook_payload,
    require_valid_signature,
    verify_signature,
)

client = TestClient(app)

TEST_WEBHOOK_SECRET = "test-webhook-secret"

SIGNATURE_HEADER = "X-Razorpay-Signature"
DELIVERY_ID_HEADER = "X-Razorpay-Event-Id"

DELIVERY_ID = "evt_hook_delivery_1"
PAYMENT_LINK_ID = "plink_webhook_test"
PAYMENT_ID = "pay_webhook_test"

PAID_EVENT = {
    "entity": "event",
    "account_id": "acc_test",
    "event": "payment_link.paid",
    "contains": ["payment_link", "order", "payment"],
    "payload": {
        "payment_link": {
            "entity": {
                "id": PAYMENT_LINK_ID,
                "status": "paid",
                "amount": 75000,
                "amount_paid": 75000,
                "currency": "INR",
                "short_url": "https://rzp.io/rzp/abc",
            }
        },
        "payment": {"entity": {"id": PAYMENT_ID, "status": "captured"}},
        "order": {"entity": {"id": "order_webhook_test", "amount_paid": 75000}},
    },
}


def _sign(raw_body: bytes, secret: str = TEST_WEBHOOK_SECRET) -> str:
    return hmac.new(
        secret.encode("utf-8"), raw_body, hashlib.sha256
    ).hexdigest()


def _raw(payload: dict | None = None) -> bytes:
    return json.dumps(payload if payload is not None else PAID_EVENT).encode("utf-8")


def _post_webhook(
    monkeypatch, tmp_path, *, raw_body=None, signature=None, delivery_id=None
):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'wh.db'}")
    monkeypatch.setenv("RAZORPAY_WEBHOOK_SECRET", TEST_WEBHOOK_SECRET)
    body = _raw() if raw_body is None else raw_body
    headers = {}
    if signature is not None:
        headers[SIGNATURE_HEADER] = signature
    if delivery_id is not None:
        headers[DELIVERY_ID_HEADER] = delivery_id
    return client.post("/webhook/razorpay", content=body, headers=headers)


# ---------------------------------------------------------------------------
# verify_signature / require_valid_signature (pure boundary)
# ---------------------------------------------------------------------------


def test_verify_signature_rejects_non_bytes_body() -> None:
    assert verify_signature(b"", "abc", TEST_WEBHOOK_SECRET) is False
    assert verify_signature("not bytes", "abc", TEST_WEBHOOK_SECRET) is False


def test_verify_signature_rejects_empty_signature_or_secret() -> None:
    raw = _raw()
    good = _sign(raw)
    assert verify_signature(raw, "", TEST_WEBHOOK_SECRET) is False
    assert verify_signature(raw, "  ", TEST_WEBHOOK_SECRET) is False
    assert verify_signature(raw, good, "") is False


def test_verify_signature_matches_valid_secret_and_mismatches_wrong_secret() -> None:
    raw = _raw()
    assert verify_signature(raw, _sign(raw, "right"), "right") is True
    assert verify_signature(raw, _sign(raw, "right"), "wrong") is False


def test_verify_signature_is_over_raw_body_not_reserialized() -> None:
    # A signature computed over the exact raw bytes must NOT match a
    # semantically-identical body that has been re-encoded (e.g. with
    # different whitespace/ordering), proving we verify the delivered bytes.
    original = _raw()
    reserialized = json.dumps(json.loads(original), indent=2).encode("utf-8")
    sig = _sign(original)
    assert verify_signature(original, sig, TEST_WEBHOOK_SECRET) is True
    assert verify_signature(reserialized, sig, TEST_WEBHOOK_SECRET) is False


def test_require_valid_signature_raises_on_mismatch() -> None:
    raw = _raw()
    require_valid_signature(raw, _sign(raw), TEST_WEBHOOK_SECRET)  # no raise
    with pytest.raises(WebhookSignatureError):
        require_valid_signature(raw, "deadbeef", TEST_WEBHOOK_SECRET)


# ---------------------------------------------------------------------------
# parse_webhook_payload (a verified payload maps to the closed-loop fields)
# ---------------------------------------------------------------------------


def test_parse_paid_event_extracts_correlation_fields() -> None:
    event = parse_webhook_payload(_raw(), DELIVERY_ID)
    assert event.delivery_id == DELIVERY_ID
    assert event.event_type == "payment_link.paid"
    assert event.payment_link_id == PAYMENT_LINK_ID
    assert event.payment_link_status == "paid"
    assert event.amount_paid_paise == 75000
    assert event.currency == "INR"
    assert event.payment_id == PAYMENT_ID


def test_parse_missing_delivery_id_is_rejected() -> None:
    with pytest.raises(WebhookPayloadError):
        parse_webhook_payload(_raw(), "")


def test_parse_malformed_json_is_rejected() -> None:
    with pytest.raises(WebhookPayloadError):
        parse_webhook_payload(b"{not json", DELIVERY_ID)


def test_parse_missing_event_field_is_rejected() -> None:
    bad = dict(PAID_EVENT)
    bad.pop("event")
    with pytest.raises(WebhookPayloadError):
        parse_webhook_payload(json.dumps(bad).encode("utf-8"), DELIVERY_ID)


def test_parse_tolerates_event_without_payment_link_entity() -> None:
    payload = {"entity": "event", "event": "payment_link.cancelled", "payload": {}}
    event = parse_webhook_payload(json.dumps(payload).encode("utf-8"), DELIVERY_ID)
    assert event.payment_link_id is None
    assert event.amount_paid_paise is None


# ---------------------------------------------------------------------------
# HTTP route: signature gate (tests A)
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _reset_dependency_overrides():
    yield
    app.dependency_overrides.clear()


def test_valid_signature_is_acknowledged(monkeypatch, tmp_path) -> None:
    body = _raw()
    response = _post_webhook(
        monkeypatch,
        tmp_path,
        raw_body=body,
        signature=_sign(body),
        delivery_id=DELIVERY_ID,
    )
    assert response.status_code == 200
    assert response.json()["status"] == "verified"
    assert response.json()["event"] == "payment_link.paid"


def test_missing_signature_header_is_rejected_before_parsing(
    monkeypatch, tmp_path
) -> None:
    response = _post_webhook(
        monkeypatch, tmp_path, raw_body=_raw(), signature=None, delivery_id=DELIVERY_ID
    )
    assert response.status_code == 400
    assert response.json()["status"] == "missing_signature"


def test_invalid_signature_is_rejected(monkeypatch, tmp_path) -> None:
    response = _post_webhook(
        monkeypatch,
        tmp_path,
        raw_body=_raw(),
        signature="deadbeef" * 8,
        delivery_id=DELIVERY_ID,
    )
    assert response.status_code == 401
    assert response.json()["status"] == "invalid_signature"


def test_signature_verified_over_exact_body_rejects_tampered_body(
    monkeypatch, tmp_path
) -> None:
    body = _raw()
    # Signature is valid for the original body, but the delivered body is
    # modified (amount changed) -> must be rejected as unauthorized.
    tampered = json.dumps(
        {
            **PAID_EVENT,
            "payload": {
                **PAID_EVENT["payload"],
                "payment_link": {
                    **PAID_EVENT["payload"]["payment_link"]["entity"],
                    "amount_paid": 99999,
                },
            },
        }
    ).encode("utf-8")
    response = _post_webhook(
        monkeypatch,
        tmp_path,
        raw_body=tampered,
        signature=_sign(body),
        delivery_id=DELIVERY_ID,
    )
    assert response.status_code == 401
    assert response.json()["status"] == "invalid_signature"


def test_unconfigured_webhook_secret_fails_closed(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'wh.db'}")
    monkeypatch.setenv("RAZORPAY_WEBHOOK_SECRET", "")
    body = _raw()
    response = client.post(
        "/webhook/razorpay",
        content=body,
        headers={SIGNATURE_HEADER: _sign(body), DELIVERY_ID_HEADER: DELIVERY_ID},
    )
    assert response.status_code == 401
    assert response.json()["status"] == "invalid_signature"


def test_unsupported_event_is_ignored_and_acknowledged(monkeypatch, tmp_path) -> None:
    payload = {
        "entity": "event",
        "event": "payment_link.expired",
        "payload": {"payment_link": {"entity": {"id": PAYMENT_LINK_ID}}},
    }
    body = json.dumps(payload).encode("utf-8")
    response = _post_webhook(
        monkeypatch,
        tmp_path,
        raw_body=body,
        signature=_sign(body),
        delivery_id=DELIVERY_ID,
    )
    assert response.status_code == 200
    assert response.json()["status"] == "ignored_unsupported_event"
    assert response.json()["event"] == "payment_link.expired"


def test_malformed_payload_after_valid_signature_is_rejected(
    monkeypatch, tmp_path
) -> None:
    body = b"{not valid json"
    response = _post_webhook(
        monkeypatch,
        tmp_path,
        raw_body=body,
        signature=_sign(body),
        delivery_id=DELIVERY_ID,
    )
    assert response.status_code == 400
    assert response.json()["status"] == "invalid_payload"


def test_missing_event_id_after_valid_signature_is_rejected(monkeypatch, tmp_path) -> None:
    body = _raw()
    # Valid signature, but no X-Razorpay-Event-Id delivery header.
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'wh.db'}")
    monkeypatch.setenv("RAZORPAY_WEBHOOK_SECRET", TEST_WEBHOOK_SECRET)
    response = client.post(
        "/webhook/razorpay",
        content=body,
        headers={SIGNATURE_HEADER: _sign(body)},
    )
    assert response.status_code == 400
    assert response.json()["status"] == "invalid_payload"

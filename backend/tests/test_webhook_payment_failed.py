"""Phase-time: payment.failed INGESTION webhook path.

A genuinely failed payment (a real Razorpay ``payment.failed`` delivery,
signature-verified over the exact raw body) is mapped to an ingested
``PaymentEvent`` so the existing detect->diagnose->policy->optimize loop can run
against it. These tests lock the contract: HMAC gate is still fail-closed,
duplicate deliveries are durable no-ops, malformed failures are 4xx, the
customer_history derivation is honest (persisted state only, never invented),
and the INGESTION path never invokes the executor/policy/selector or creates a
Payment Link.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import sqlite3

from fastapi.testclient import TestClient

from app.db import connect, init_db, get_classification_result, get_payment_event
from app.failed_payment_ingestion import map_failed_payment_to_event
from app.main import app
from app.models import CustomerHistory, PaymentEvent
from app.razorpay_webhook import (
    FailedPaymentEvent,
    parse_payment_failed_payload,
)
import app.webhook_service as webhook_service

client = TestClient(app)

TEST_WEBHOOK_SECRET = "test-webhook-secret"
SIGNATURE_HEADER = "X-Razorpay-Signature"
DELIVERY_ID_HEADER = "X-Razorpay-Event-Id"

DELIVERY_ID = "delivery_pfail_1"

FAILED_EVENT = {
    "entity": "event",
    "account_id": "acc_live",
    "event": "payment.failed",
    "contains": ["payment"],
    "payload": {
        "payment": {
            "entity": {
                "id": "pay_fail_001",
                "order_id": "order_fail_001",
                "amount": 499900,
                "currency": "INR",
                "status": "failed",
                "method": "card",
                "error_code": "BAD_REQUEST_ERROR",
                "error_description": "The bank has declined the transaction",
                "customer_id": "cust_live_01",
                "created_at": 1700000000,
            }
        }
    },
}


def _sign(raw_body: bytes, secret: str = TEST_WEBHOOK_SECRET) -> str:
    return hmac.new(secret.encode("utf-8"), raw_body, hashlib.sha256).hexdigest()


def _raw(payload: dict | None = None) -> bytes:
    return json.dumps(payload if payload is not None else FAILED_EVENT).encode("utf-8")


def _set_env(monkeypatch, tmp_path):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'pfail.db'}")
    monkeypatch.setenv("RAZORPAY_WEBHOOK_SECRET", TEST_WEBHOOK_SECRET)


def _conn(tmp_path):
    conn = connect(str(tmp_path / "pfail.db"))
    init_db(conn)
    return conn


# ---------------------------------------------------------------------------
# Successful ingestion
# ---------------------------------------------------------------------------


def test_payment_failed_is_ingested_as_event(monkeypatch, tmp_path) -> None:
    _set_env(monkeypatch, tmp_path)
    body = _raw()
    response = client.post(
        "/webhook/razorpay",
        content=body,
        headers={SIGNATURE_HEADER: _sign(body), DELIVERY_ID_HEADER: DELIVERY_ID},
    )
    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "ingested"
    assert "pay_fail_001" in data["detail"]

    # The failure is a real persisted PaymentEvent mapped from the delivery.
    failed = parse_payment_failed_payload(body, DELIVERY_ID)
    conn = _conn(tmp_path)
    try:
        event = get_payment_event(conn, map_failed_payment_to_event(conn, failed).event_id)
        assert event is not None
        assert event.payment_id == "pay_fail_001"
        assert event.order_id == "order_fail_001"
        assert event.amount_paise == 499900
        assert event.currency == "INR"
        assert event.payment_method == "card"
        assert event.customer_id == "cust_live_01"
        assert event.failure_reason == "BAD_REQUEST_ERROR"
        # Honest neutral history: no persisted events for this customer.
        assert event.customer_history.prior_failed_payments == 0
        assert event.customer_history.prior_successful_payments == 0
        # webhook delivery row recorded as ingested
        row = conn.execute(
            "SELECT status FROM webhook_deliveries WHERE delivery_id = ?",
            (DELIVERY_ID,),
        ).fetchone()
        assert row is not None
        assert row["status"] == "ingested"
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Signature gate (fail-closed) still enforced before any ingestion
# ---------------------------------------------------------------------------


def test_payment_failed_bad_signature_is_rejected(monkeypatch, tmp_path) -> None:
    _set_env(monkeypatch, tmp_path)
    body = _raw()
    response = client.post(
        "/webhook/razorpay",
        content=body,
        headers={
            SIGNATURE_HEADER: _sign(body, "wrong-secret"),
            DELIVERY_ID_HEADER: DELIVERY_ID,
        },
    )
    assert response.status_code == 401
    assert response.json()["status"] == "invalid_signature"
    # Nothing ingested.
    conn = _conn(tmp_path)
    try:
        failed = parse_payment_failed_payload(body, DELIVERY_ID)
        assert get_payment_event(conn, map_failed_payment_to_event(conn, failed).event_id) is None
    finally:
        conn.close()


def test_payment_failed_missing_signature_is_400(monkeypatch, tmp_path) -> None:
    _set_env(monkeypatch, tmp_path)
    body = _raw()
    response = client.post(
        "/webhook/razorpay",
        content=body,
        headers={DELIVERY_ID_HEADER: DELIVERY_ID},
    )
    assert response.status_code == 400
    assert response.json()["status"] == "missing_signature"


# ---------------------------------------------------------------------------
# Durable idempotency
# ---------------------------------------------------------------------------


def test_duplicate_payment_failed_is_a_noop(monkeypatch, tmp_path) -> None:
    _set_env(monkeypatch, tmp_path)
    body = _raw()
    headers = {SIGNATURE_HEADER: _sign(body), DELIVERY_ID_HEADER: DELIVERY_ID}

    first = client.post("/webhook/razorpay", content=body, headers=headers)
    assert first.status_code == 200
    assert first.json()["status"] == "ingested"

    second = client.post("/webhook/razorpay", content=body, headers=headers)
    assert second.status_code == 200
    assert second.json()["status"] == "deduplicated"

    # Exactly one event persisted for the derived event id, despite two deliveries.
    conn = _conn(tmp_path)
    try:
        failed = parse_payment_failed_payload(body, DELIVERY_ID)
        event_id = map_failed_payment_to_event(conn, failed).event_id
        rows = conn.execute(
            "SELECT COUNT(*) AS c FROM payment_events WHERE event_id = ?",
            (event_id,),
        ).fetchone()
        assert rows["c"] == 1
    finally:
        conn.close()


def test_same_delivery_id_different_body_is_conflict(monkeypatch, tmp_path) -> None:
    _set_env(monkeypatch, tmp_path)
    body = _raw()
    headers = {SIGNATURE_HEADER: _sign(body), DELIVERY_ID_HEADER: DELIVERY_ID}
    assert client.post("/webhook/razorpay", content=body, headers=headers).status_code == 200

    # Same delivery id, different (still valid) body -> 409, never overwritten.
    tampered = json.dumps(
        {
            **FAILED_EVENT,
            "payload": {
                "payment": {
                    "entity": {
                        **FAILED_EVENT["payload"]["payment"]["entity"],
                        "amount": 111111,
                    }
                }
            },
        }
    ).encode("utf-8")
    conflict = client.post(
        "/webhook/razorpay",
        content=tampered,
        headers={SIGNATURE_HEADER: _sign(tampered), DELIVERY_ID_HEADER: DELIVERY_ID},
    )
    assert conflict.status_code == 409
    assert conflict.json()["status"] == "conflict"


# ---------------------------------------------------------------------------
# Malformed / unsupported shape
# ---------------------------------------------------------------------------


def test_payment_failed_non_failed_status_is_400(monkeypatch, tmp_path) -> None:
    _set_env(monkeypatch, tmp_path)
    bad = json.dumps(
        {
            **FAILED_EVENT,
            "payload": {
                "payment": {
                    "entity": {
                        **FAILED_EVENT["payload"]["payment"]["entity"],
                        "status": "authorized",
                    }
                }
            },
        }
    ).encode("utf-8")
    response = client.post(
        "/webhook/razorpay",
        content=bad,
        headers={SIGNATURE_HEADER: _sign(bad), DELIVERY_ID_HEADER: DELIVERY_ID},
    )
    assert response.status_code == 400
    assert response.json()["status"] == "invalid_payload"


def test_payment_failed_missing_payment_id_is_400(monkeypatch, tmp_path) -> None:
    _set_env(monkeypatch, tmp_path)
    bad = json.dumps(
        {
            **FAILED_EVENT,
            "payload": {"payment": {"entity": {"status": "failed", "amount": 5000}}},
        }
    ).encode("utf-8")
    response = client.post(
        "/webhook/razorpay",
        content=bad,
        headers={SIGNATURE_HEADER: _sign(bad), DELIVERY_ID_HEADER: DELIVERY_ID},
    )
    assert response.status_code == 400


# ---------------------------------------------------------------------------
# Mapper: customer_history derives from persisted state only (never invented)
# ---------------------------------------------------------------------------


def test_mapper_derives_history_from_persisted_events(tmp_path) -> None:
    conn = _conn(tmp_path)
    try:
        # A prior persisted event for this customer counts toward prior failures.
        prior = PaymentEvent(
            event_id="evt_prior_1",
            order_id="order_prior",
            payment_id="pay_prior",
            customer_id="cust_live_01",
            amount_paise=1000,
            currency="INR",
            payment_method="card",
            failure_reason="insufficient_funds",
            bank="HDFC",
            risk_flag="normal",
            customer_history=CustomerHistory(0, 0, False),
            timestamp="2026-08-01T09:30:00+00:00",
        )
        conn.execute(
            "INSERT INTO payment_events (event_id, order_id, payment_id, customer_id, "
            "amount_paise, currency, payment_method, failure_reason, bank, risk_flag, "
            "customer_history, timestamp) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                prior.event_id, prior.order_id, prior.payment_id, prior.customer_id,
                prior.amount_paise, prior.currency, prior.payment_method,
                prior.failure_reason, prior.bank, prior.risk_flag,
                json.dumps(prior.customer_history.to_dict()), prior.timestamp,
            ),
        )
        conn.commit()

        failed = parse_payment_failed_payload(_raw(), DELIVERY_ID)
        event = map_failed_payment_to_event(conn, failed)
        assert event.customer_history.prior_failed_payments == 1
        assert event.customer_history.prior_successful_payments == 0
    finally:
        conn.close()


def test_mapper_is_deterministic_for_same_delivery(tmp_path) -> None:
    conn = _conn(tmp_path)
    try:
        failed = parse_payment_failed_payload(_raw(), DELIVERY_ID)
        assert (
            map_failed_payment_to_event(conn, failed).event_id
            == map_failed_payment_to_event(conn, failed).event_id
        )
        # Different delivery -> different event id.
        other = parse_payment_failed_payload(_raw(), "delivery_pfail_OTHER")
        assert (
            map_failed_payment_to_event(conn, failed).event_id
            != map_failed_payment_to_event(conn, other).event_id
        )
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Integrity: INGESTION never invokes executor, policy, selector, or link creation
# ---------------------------------------------------------------------------


def test_payment_failed_parses_without_outcome_machinery(tmp_path) -> None:
    """The parsed FailedPaymentEvent carries only input fields, never an outcome."""
    body = _raw()
    failed = parse_payment_failed_payload(body, DELIVERY_ID)
    assert isinstance(failed, FailedPaymentEvent)
    assert not hasattr(failed, "amount_paid_paise")
    assert not hasattr(failed, "payment_link_id")


def test_ingestion_path_writes_no_recovery_outcome(monkeypatch, tmp_path) -> None:
    """Ingesting a failure must never create a recovery/recovery-outcome row."""
    _set_env(monkeypatch, tmp_path)
    body = _raw()
    response = client.post(
        "/webhook/razorpay",
        content=body,
        headers={SIGNATURE_HEADER: _sign(body), DELIVERY_ID_HEADER: DELIVERY_ID},
    )
    assert response.status_code == 200
    conn = _conn(tmp_path)
    try:
        recs = conn.execute("SELECT COUNT(*) AS c FROM webhook_recovery_outcomes").fetchone()
        assert recs["c"] == 0
        execs = conn.execute("SELECT COUNT(*) AS c FROM execution_outcomes").fetchone()
        assert execs["c"] == 0
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Auto-Diagnose: ingestion kicks off the advisory AI diagnosis best-effort
# ---------------------------------------------------------------------------


class _StubAdapter:
    """Duck-typed classifier adapter controlling generate()/close()."""

    def __init__(self, *responses):
        self._responses = list(responses)
        self.closed = False

    def generate(self, prompt: str) -> str:
        if not self._responses:
            raise RuntimeError("stub adapter exhausted")
        return self._responses.pop(0)

    def close(self) -> None:
        self.closed = True


def _stub_classifier(monkeypatch, *responses):
    stub = _StubAdapter(*responses)
    monkeypatch.setattr(webhook_service, "build_omniroute_adapter", lambda: stub)
    return stub


def test_ingestion_auto_runs_ai_diagnosis(monkeypatch, tmp_path) -> None:
    """After ingesting a payment.failed, the advisory diagnosis runs and persists."""
    response_json = json.dumps(
        {
            "event_id": "evt_pfail_ea320c78aa7a",
            "root_cause_category": "terminal",
            "confidence": 0.85,
            "reasoning": "bad request error on an unsupported card",
            "candidate_interventions": ["no_action"],
        }
    )
    stub = _stub_classifier(monkeypatch, response_json)

    _set_env(monkeypatch, tmp_path)
    body = _raw()
    response = client.post(
        "/webhook/razorpay",
        content=body,
        headers={SIGNATURE_HEADER: _sign(body), DELIVERY_ID_HEADER: DELIVERY_ID},
    )
    assert response.status_code == 200
    assert response.json()["status"] == "ingested"
    assert stub.closed is True  # adapter lifecycle is not leaked

    # The diagnosis was persisted for the derived event id.
    conn = _conn(tmp_path)
    try:
        failed = parse_payment_failed_payload(body, DELIVERY_ID)
        event_id = map_failed_payment_to_event(conn, failed).event_id
        persisted = get_classification_result(conn, event_id)
        assert persisted is not None
        assert persisted.root_cause_category == "terminal"
        assert list(persisted.candidate_interventions) == ["no_action"]
    finally:
        conn.close()


def test_ingestion_survives_classifier_failure(monkeypatch, tmp_path) -> None:
    """A classifier outage is best-effort: ingestion still succeeds as 'ingested'."""
    _stub_classifier(monkeypatch)  # generate() raises immediately

    _set_env(monkeypatch, tmp_path)
    body = _raw()
    response = client.post(
        "/webhook/razorpay",
        content=body,
        headers={SIGNATURE_HEADER: _sign(body), DELIVERY_ID_HEADER: DELIVERY_ID},
    )
    # The webhook must be acknowledged as ingested; a model failure must NOT
    # fail ingestion or trigger a Razorpay retry.
    assert response.status_code == 200
    assert response.json()["status"] == "ingested"

    conn = _conn(tmp_path)
    try:
        failed = parse_payment_failed_payload(body, DELIVERY_ID)
        event_id = map_failed_payment_to_event(conn, failed).event_id
        # Event still ingested.
        assert get_payment_event(conn, event_id) is not None
        # No (failed) classification persisted.
        assert get_classification_result(conn, event_id) is None
    finally:
        conn.close()

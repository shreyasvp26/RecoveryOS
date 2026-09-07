"""Phase 25 adversarial verification.

Attempts to break every hardening fix from every listed angle:

  * duplicate webhook deliveries — sequential (same id + body -> deduplicated;
    same id + DIFFERENT body -> 409 conflict, never overwritten) and
    concurrent (two threads/connections racing the same delivery id);
  * concurrent recovery outcomes for one Payment Link — two threads racing
    under separate delivery ids collapse to exactly one recovery row;
  * classifier crashes at every point — adapter build, model call,
    persistence, clear, and even failure-recording itself must never fail the
    webhook, and durable failure records must stay honest;
  * worker/process crashes at each transaction boundary — a crash between the
    committed event/recovery insert and the delivery's terminal status write
    must converge to exactly-once on Razorpay's redelivery;
  * retries after partial success on the manual classify endpoint;
  * unauthorized access to every operator endpoint — the webhook signature
    gate, the client-ignored execute contract, the live-key guard, and the
    existing-event requirements all hold.

Any assertion here that a real run would violate is a defect, not a flake.
"""

from __future__ import annotations
from conftest import TEST_OPERATOR_HEADERS


import hashlib
import hmac
import json
import sqlite3
import threading

import pytest
from fastapi.testclient import TestClient

from app.classification import ClassificationResult
from app.classifier import OmniRouteError
from app.db import (
    connect,
    get_classification_failure,
    get_classification_result,
    init_db,
    insert_classification_result,
    insert_execution_outcome,
)
from app.executor import ExecutionOutcome
from app.failed_payment_ingestion import map_failed_payment_to_event
from app.main import app
from app.razorpay_client import RazorpayConfigurationError, RazorpayPaymentLinkClient
from app.razorpay_webhook import (
    parse_payment_failed_payload,
    parse_webhook_payload,
)
import app.webhook_service as webhook_service

client = TestClient(app, headers=TEST_OPERATOR_HEADERS)

TEST_WEBHOOK_SECRET = "test-webhook-secret"
SIGNATURE_HEADER = "X-Razorpay-Signature"
DELIVERY_ID_HEADER = "X-Razorpay-Event-Id"

NOW = "2026-08-28T12:00:00+00:00"

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

EVENT_PAYLOAD = {
    "event_id": "evt_adv_exec",
    "order_id": "order_adv",
    "payment_id": "pay_adv",
    "customer_id": "cust_adv",
    "amount_paise": 50_000,
    "currency": "INR",
    "payment_method": "card",
    "failure_reason": "bank_timeout",
    "bank": "HDFC",
    "risk_flag": "normal",
    "customer_history": {
        "prior_successful_payments": 1,
        "prior_failed_payments": 1,
        "has_active_subscription": False,
    },
    "timestamp": NOW,
}


def _classify_json(event_id: str) -> dict:
    return {
        "event_id": event_id,
        "root_cause_category": "terminal",
        "confidence": 0.9,
        "reasoning": "card declined once, terminal",
        "candidate_interventions": ["no_action"],
    }


def _sign(raw_body: bytes, secret: str = TEST_WEBHOOK_SECRET) -> str:
    return hmac.new(secret.encode("utf-8"), raw_body, hashlib.sha256).hexdigest()


def _raw(payload: dict | None = None) -> bytes:
    return json.dumps(payload if payload is not None else FAILED_EVENT).encode("utf-8")


def _paid_payload(*, link_id: str, payment_id: str, amount_paise: int = 75000) -> dict:
    return {
        "entity": "event",
        "account_id": "acc_test",
        "event": "payment_link.paid",
        "contains": ["payment_link", "order", "payment"],
        "payload": {
            "payment_link": {
                "entity": {
                    "id": link_id,
                    "status": "paid",
                    "amount": amount_paise,
                    "amount_paid": amount_paise,
                    "currency": "INR",
                    "short_url": "https://rzp.io/rzp/abc",
                }
            },
            "payment": {"entity": {"id": payment_id, "status": "captured"}},
            "order": {"entity": {"id": "order_adv", "amount_paid": amount_paise}},
        },
    }


def _set_env(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'adv.db'}")
    monkeypatch.setenv("RAZORPAY_WEBHOOK_SECRET", TEST_WEBHOOK_SECRET)


def _conn(tmp_path):
    conn = connect(str(tmp_path / "adv.db"))
    init_db(conn)
    return conn


class _GoodAdapter:
    """Adapter that always returns a valid classification for one event."""

    def __init__(self, event_id: str) -> None:
        self.event_id = event_id
        self.closed = False

    def generate(self, prompt: str) -> str:
        return json.dumps(_classify_json(self.event_id))

    def close(self) -> None:
        self.closed = True


# ---------------------------------------------------------------------------
# Duplicate webhook deliveries
# ---------------------------------------------------------------------------


def test_duplicate_delivery_is_acknowledged_exactly_once(monkeypatch, tmp_path) -> None:
    """The same verified delivery redelivered is a 2xx no-op, never a second
    event and never a second processing."""
    _set_env(monkeypatch, tmp_path)
    body = _raw()
    headers = {SIGNATURE_HEADER: _sign(body), DELIVERY_ID_HEADER: "dup_seq_a"}

    first = client.post("/webhook/razorpay", content=body, headers=headers)
    assert first.status_code == 200
    assert first.json()["status"] == "ingested"

    second = client.post("/webhook/razorpay", content=body, headers=headers)
    assert second.status_code == 200
    assert second.json()["status"] == "deduplicated"

    conn = _conn(tmp_path)
    try:
        deliveries = conn.execute(
            "SELECT COUNT(*) AS c FROM webhook_deliveries WHERE delivery_id = ?",
            ("dup_seq_a",),
        ).fetchone()
        assert deliveries["c"] == 1
        events = conn.execute("SELECT COUNT(*) AS c FROM payment_events").fetchone()
        assert events["c"] == 1
    finally:
        conn.close()


def test_same_delivery_id_with_different_body_is_a_conflict_never_overwrite(
    monkeypatch, tmp_path
) -> None:
    """A delivery id replayed under a DIFFERENT body is refused outright; the
    original event is never overwritten and no second event appears."""
    _set_env(monkeypatch, tmp_path)
    body1 = _raw()
    body2 = _raw(
        {
            **FAILED_EVENT,
            "payload": {
                "payment": {
                    "entity": {
                        **FAILED_EVENT["payload"]["payment"]["entity"],
                        "id": "pay_fail_002",
                    }
                }
            },
        }
    )
    headers1 = {SIGNATURE_HEADER: _sign(body1), DELIVERY_ID_HEADER: "dup_conflict"}
    headers2 = {SIGNATURE_HEADER: _sign(body2), DELIVERY_ID_HEADER: "dup_conflict"}

    first = client.post("/webhook/razorpay", content=body1, headers=headers1)
    assert first.status_code == 200
    assert first.json()["status"] == "ingested"

    second = client.post("/webhook/razorpay", content=body2, headers=headers2)
    assert second.status_code == 409
    assert second.json()["status"] == "conflict"

    conn = _conn(tmp_path)
    try:
        events = conn.execute("SELECT COUNT(*) AS c FROM payment_events").fetchone()
        assert events["c"] == 1
    finally:
        conn.close()


def test_concurrent_duplicate_delivery_records_exactly_once(tmp_path) -> None:
    """Two processes racing the SAME delivery id claim one row, ingest one
    event, and leave one delivery that reaches a terminal status (or an
    in-flight one a retry can complete)."""
    path = tmp_path / "conc_same.db"
    body = _raw()
    delivery_id = "conc_same_delivery"
    results: list[str] = []
    results_lock = threading.Lock()
    barrier = threading.Barrier(2)

    def worker() -> None:
        conn = connect(str(path))
        init_db(conn)
        try:
            failed = parse_payment_failed_payload(body, delivery_id)
            barrier.wait(timeout=10)
            result = webhook_service.process_payment_failed(conn, failed, body, NOW)
            outcome = result.status
        except Exception as exc:  # collect, do not kill the thread
            outcome = f"raised:{type(exc).__name__}"
        finally:
            with results_lock:
                results.append(outcome)
            conn.close()

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=60)
    assert not any(t.is_alive() for t in threads)

    conn = connect(str(path))
    init_db(conn)
    try:
        events = conn.execute("SELECT COUNT(*) AS c FROM payment_events").fetchone()
        assert events["c"] == 1
        deliveries = conn.execute(
            "SELECT COUNT(*) AS c FROM webhook_deliveries WHERE delivery_id = ?",
            (delivery_id,),
        ).fetchone()
        assert deliveries["c"] == 1
        status = conn.execute(
            "SELECT status FROM webhook_deliveries WHERE delivery_id = ?",
            (delivery_id,),
        ).fetchone()["status"]
        assert status in {"claimed", "ingested"}
        allowed = {"ingested", "duplicate_event", "deduplicated", "persistence_failure"}
        assert set(results) <= allowed
        if status == "claimed":
            # The racing process crashed before the terminal write; a
            # redelivery must complete it without a second event.
            conn2 = connect(str(path))
            init_db(conn2)
            try:
                retry = webhook_service.process_payment_failed(
                    conn2, parse_payment_failed_payload(body, delivery_id), body, NOW
                )
                assert retry.status == "duplicate_event"
                events2 = conn2.execute(
                    "SELECT COUNT(*) AS c FROM payment_events"
                ).fetchone()
                assert events2["c"] == 1
            finally:
                conn2.close()
    finally:
        conn.close()


def test_concurrent_recovery_outcomes_for_same_link_collapse_to_one(tmp_path) -> None:
    """Two processes racing the same Payment Link under DIFFERENT delivery ids
    can each run the correlation path, but the unique index keeps exactly one
    recovery row for the link."""
    path = tmp_path / "conc_out.db"
    conn = connect(str(path))
    init_db(conn)
    insert_execution_outcome(
        conn,
        ExecutionOutcome(
            event_id="evt_conc_out",
            intervention="payment_link",
            execution_mode="REAL_RAZORPAY",
            status="SUCCESS",
            external_reference="https://rzp.io/l/c",
            detail=None,
            reported_at=NOW,
            payment_link_id="plink_conc_out",
        ),
    )
    conn.close()

    results: list[str] = []
    results_lock = threading.Lock()
    barrier = threading.Barrier(2)
    delivery_ids = ("conc_out_1", "conc_out_2")

    def worker(delivery_id: str) -> None:
        raw_body = json.dumps(
            _paid_payload(link_id="plink_conc_out", payment_id=f"pay_{delivery_id}")
        ).encode("utf-8")
        conn = connect(str(path))
        init_db(conn)
        try:
            event = parse_webhook_payload(raw_body, delivery_id)
            barrier.wait(timeout=10)
            result = webhook_service.process_webhook(conn, event, raw_body, NOW)
            outcome = result.status
        except Exception as exc:  # collect, do not kill the thread
            outcome = f"raised:{type(exc).__name__}"
        finally:
            with results_lock:
                results.append(outcome)
            conn.close()

    threads = [
        threading.Thread(target=worker, args=(delivery_id,))
        for delivery_id in delivery_ids
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=60)
    assert not any(t.is_alive() for t in threads)

    assert set(results) <= {"processed", "persistence_failure"}
    conn = connect(str(path))
    init_db(conn)
    try:
        rows = conn.execute(
            "SELECT COUNT(*) AS c FROM webhook_recovery_outcomes "
            "WHERE payment_link_id = ?",
            ("plink_conc_out",),
        ).fetchone()
        assert rows["c"] == 1
        for delivery_id in delivery_ids:
            status = conn.execute(
                "SELECT status FROM webhook_deliveries WHERE delivery_id = ?",
                (delivery_id,),
            ).fetchone()["status"]
            assert status == "processed"
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Classifier crashes at every point (Fix 3)
# ---------------------------------------------------------------------------


def test_auto_classify_persist_crash_is_a_durable_failure(
    monkeypatch, tmp_path
) -> None:
    """The model succeeds but the classification cannot be persisted: the
    webhook still ingests, and the failure is durable, not a lost log line."""
    conn = _conn(tmp_path)
    body = _raw()
    delivery_id = "cls_persist_crash"
    failed = parse_payment_failed_payload(body, delivery_id)
    event_id = map_failed_payment_to_event(conn, failed).event_id

    monkeypatch.setattr(
        webhook_service, "build_omniroute_adapter", lambda: _GoodAdapter(event_id)
    )

    def raise_persist(c, result, **kwargs):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(webhook_service.db, "insert_classification_result", raise_persist)

    result = webhook_service.process_payment_failed(conn, failed, body, NOW)
    assert result.status == "ingested"
    assert get_classification_result(conn, event_id) is None
    record = get_classification_failure(conn, event_id)
    assert record is not None
    assert "persistence_failed" in record["last_error"]
    conn.close()


def test_auto_classify_clear_crash_does_not_fabricate_a_failure(
    monkeypatch, tmp_path
) -> None:
    """A crash while clearing the failure record AFTER a successful
    persistence must not fabricate a classification failure: the classification
    is durable and the row stays honest."""
    conn = _conn(tmp_path)
    body = _raw()
    delivery_id = "cls_clear_crash"
    failed = parse_payment_failed_payload(body, delivery_id)
    event_id = map_failed_payment_to_event(conn, failed).event_id

    monkeypatch.setattr(
        webhook_service, "build_omniroute_adapter", lambda: _GoodAdapter(event_id)
    )

    def raise_clear(c, event_id, **kwargs):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(webhook_service.db, "clear_classification_failure", raise_clear)

    result = webhook_service.process_payment_failed(conn, failed, body, NOW)
    assert result.status == "ingested"
    assert get_classification_result(conn, event_id) is not None
    assert get_classification_failure(conn, event_id) is None
    conn.close()


def test_auto_classify_crash_recording_failure_still_ingests(
    monkeypatch, tmp_path
) -> None:
    """Even the failure recorder itself failing must never fail the webhook;
    ingestion completes regardless."""
    conn = _conn(tmp_path)
    body = _raw()
    failed = parse_payment_failed_payload(body, "cls_record_crash")

    def adapter_unavailable():
        raise RuntimeError("model provider timeout")

    def raise_record(c, **kwargs):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(webhook_service, "build_omniroute_adapter", adapter_unavailable)
    monkeypatch.setattr(webhook_service.db, "record_classification_failure", raise_record)

    result = webhook_service.process_payment_failed(conn, failed, body, NOW)
    assert result.status == "ingested"
    assert get_payment_event_count(conn) == 1
    conn.close()


# ---------------------------------------------------------------------------
# Worker/process crashes at each transaction boundary (Fix 1 + Fix 2)
# ---------------------------------------------------------------------------


def test_crash_between_event_insert_and_status_update_converges_exactly_once(
    monkeypatch, tmp_path
) -> None:
    """A process crash after the event insert commits but before the delivery
    reaches its terminal status: the delivery stays claimed and Razorpay's
    redelivery converges to exactly one event."""
    conn = _conn(tmp_path)
    body = _raw()
    delivery_id = "crash_boundary_event"
    failed = parse_payment_failed_payload(body, delivery_id)

    real_update = webhook_service.db.update_webhook_delivery_status

    def crash_update(c, did, status, **kwargs):
        raise sqlite3.OperationalError("simulated crash before status write")

    monkeypatch.setattr(
        webhook_service.db, "update_webhook_delivery_status", crash_update
    )

    with pytest.raises(sqlite3.OperationalError):
        webhook_service.process_payment_failed(conn, failed, body, NOW)

    status = conn.execute(
        "SELECT status FROM webhook_deliveries WHERE delivery_id = ?",
        (delivery_id,),
    ).fetchone()["status"]
    assert status == "claimed"
    assert get_payment_event_count(conn) == 1

    monkeypatch.setattr(
        webhook_service.db, "update_webhook_delivery_status", real_update
    )
    retry = webhook_service.process_payment_failed(
        conn, parse_payment_failed_payload(body, delivery_id), body, NOW
    )
    assert retry.status == "duplicate_event"
    assert get_payment_event_count(conn) == 1
    status = conn.execute(
        "SELECT status FROM webhook_deliveries WHERE delivery_id = ?",
        (delivery_id,),
    ).fetchone()["status"]
    assert status == "ingested"
    conn.close()


def test_crash_between_recovery_insert_and_status_update_converges_exactly_once(
    monkeypatch, tmp_path
) -> None:
    """A crash after the verified recovery commits but before the delivery is
    marked processed: the redelivery re-correlates as an idempotent no-op and
    exactly one recovery row survives."""
    conn = _conn(tmp_path)
    insert_execution_outcome(
        conn,
        ExecutionOutcome(
            event_id="evt_crash_rec",
            intervention="payment_link",
            execution_mode="REAL_RAZORPAY",
            status="SUCCESS",
            external_reference="https://rzp.io/l/cr",
            detail=None,
            reported_at=NOW,
            payment_link_id="plink_crash_rec",
        ),
    )
    body = _paid_payload(link_id="plink_crash_rec", payment_id="pay_crash_rec")
    raw_body = json.dumps(body).encode("utf-8")
    delivery_id = "crash_boundary_recovery"
    event = parse_webhook_payload(raw_body, delivery_id)

    real_update = webhook_service.db.update_webhook_delivery_status

    def crash_update(c, did, status, **kwargs):
        raise sqlite3.OperationalError("simulated crash before status write")

    monkeypatch.setattr(
        webhook_service.db, "update_webhook_delivery_status", crash_update
    )

    result = webhook_service.process_webhook(conn, event, raw_body, NOW)
    assert result.status == "persistence_failure"

    recovery = conn.execute(
        "SELECT COUNT(*) AS c FROM webhook_recovery_outcomes "
        "WHERE payment_link_id = ?",
        ("plink_crash_rec",),
    ).fetchone()
    assert recovery["c"] == 1
    status = conn.execute(
        "SELECT status FROM webhook_deliveries WHERE delivery_id = ?",
        (delivery_id,),
    ).fetchone()["status"]
    assert status == "claimed"

    monkeypatch.setattr(
        webhook_service.db, "update_webhook_delivery_status", real_update
    )
    retry = webhook_service.process_webhook(
        conn, parse_webhook_payload(raw_body, delivery_id), raw_body, NOW
    )
    assert retry.status == "processed"
    recovery = conn.execute(
        "SELECT COUNT(*) AS c FROM webhook_recovery_outcomes "
        "WHERE payment_link_id = ?",
        ("plink_crash_rec",),
    ).fetchone()
    assert recovery["c"] == 1
    conn.close()


def test_crash_at_status_write_is_a_canonical_http_500_then_redelivery_succeeds(
    monkeypatch, tmp_path
) -> None:
    """End to end: a sqlite error at the status-update boundary (after the
    event insert committed) answers the canonical persistence_failure 500, and
    the redelivery converges to one event and a terminal delivery."""
    _set_env(monkeypatch, tmp_path)
    body = _raw()
    headers = {SIGNATURE_HEADER: _sign(body), DELIVERY_ID_HEADER: "crash_boundary_e2e"}

    real_update = webhook_service.db.update_webhook_delivery_status

    def crash_update(c, did, status, **kwargs):
        raise sqlite3.OperationalError("simulated crash before status write")

    monkeypatch.setattr(
        webhook_service.db, "update_webhook_delivery_status", crash_update
    )

    first = client.post("/webhook/razorpay", content=body, headers=headers)
    assert first.status_code == 500
    assert first.json()["status"] == "persistence_failure"

    conn = _conn(tmp_path)
    status = conn.execute(
        "SELECT status FROM webhook_deliveries WHERE delivery_id = ?",
        ("crash_boundary_e2e",),
    ).fetchone()["status"]
    assert status == "claimed"
    assert get_payment_event_count(conn) == 1
    conn.close()

    monkeypatch.setattr(
        webhook_service.db, "update_webhook_delivery_status", real_update
    )
    second = client.post("/webhook/razorpay", content=body, headers=headers)
    assert second.status_code == 200
    assert second.json()["status"] == "duplicate_event"

    conn = _conn(tmp_path)
    try:
        assert get_payment_event_count(conn) == 1
        status = conn.execute(
            "SELECT status FROM webhook_deliveries WHERE delivery_id = ?",
            ("crash_boundary_e2e",),
        ).fetchone()["status"]
        assert status == "ingested"
    finally:
        conn.close()


def test_claim_persistence_failure_returns_error_and_retry_completes(
    monkeypatch, tmp_path
) -> None:
    """If the claim write itself fails, nothing is recorded, an error is
    returned so Razorpay retries, and the retry completes normally."""
    conn = _conn(tmp_path)
    body = _raw()
    delivery_id = "crash_boundary_claim"

    real_insert = webhook_service.db.insert_webhook_delivery

    def crash_claim(c, **kwargs):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(webhook_service.db, "insert_webhook_delivery", crash_claim)

    result = webhook_service.process_payment_failed(
        conn, parse_payment_failed_payload(body, delivery_id), body, NOW
    )
    assert result.status == "persistence_failure"
    rows = conn.execute(
        "SELECT COUNT(*) AS c FROM webhook_deliveries WHERE delivery_id = ?",
        (delivery_id,),
    ).fetchone()
    assert rows["c"] == 0
    assert get_payment_event_count(conn) == 0

    monkeypatch.setattr(webhook_service.db, "insert_webhook_delivery", real_insert)
    retry = webhook_service.process_payment_failed(
        conn, parse_payment_failed_payload(body, delivery_id), body, NOW
    )
    assert retry.status == "ingested"
    assert get_payment_event_count(conn) == 1
    conn.close()


# ---------------------------------------------------------------------------
# Retries after partial success (manual classify endpoint)
# ---------------------------------------------------------------------------


def test_manual_classify_partial_success_retry_recovers_not_500_forever(
    monkeypatch, tmp_path
) -> None:
    """A prior classify attempt persisted the classification but crashed before
    its 200. The retry must not loop on IntegrityError: it confirms the
    persisted classification and answers success."""
    _set_env(monkeypatch, tmp_path)
    assert client.post("/events", json=EVENT_PAYLOAD).status_code == 201
    event_id = EVENT_PAYLOAD["event_id"]

    conn = _conn(tmp_path)
    insert_classification_result(
        conn, ClassificationResult.from_dict(_classify_json(event_id))
    )
    conn.close()
    record_classification_failure_for(tmp_path / "adv.db", event_id)

    from app.routes import events as events_routes

    app.dependency_overrides[events_routes.get_classifier] = lambda: _GoodAdapter(
        event_id
    )
    monkeypatch.setattr(
        events_routes, "insert_classification_result", _raise_duplicate_on_insert
    )
    try:
        response = client.post(f"/events/{event_id}/classify")
    finally:
        app.dependency_overrides.clear()
    assert response.status_code == 200
    assert response.json()["status"] == "classification_success"
    conn = _conn(tmp_path)
    try:
        assert get_classification_result(conn, event_id) is not None
        assert get_classification_failure(conn, event_id) is None
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Unauthorized access to operator endpoints
# ---------------------------------------------------------------------------


def test_webhook_rejects_missing_and_forged_signatures(monkeypatch, tmp_path) -> None:
    """No signature, a forged signature, and a wrong-secret signature are all
    rejected before any processing: no delivery row and no event."""
    _set_env(monkeypatch, tmp_path)
    body = _raw()

    no_sig = client.post(
        "/webhook/razorpay",
        content=body,
        headers={DELIVERY_ID_HEADER: "unauth_no_sig"},
    )
    assert no_sig.status_code == 400
    assert no_sig.json()["status"] == "missing_signature"

    bad_sig = client.post(
        "/webhook/razorpay",
        content=body,
        headers={
            SIGNATURE_HEADER: "deadbeef" * 8,
            DELIVERY_ID_HEADER: "unauth_bad_sig",
        },
    )
    assert bad_sig.status_code == 401
    assert bad_sig.json()["status"] == "invalid_signature"

    wrong_secret = client.post(
        "/webhook/razorpay",
        content=body,
        headers={
            SIGNATURE_HEADER: _sign(body, "wrong-secret"),
            DELIVERY_ID_HEADER: "unauth_wrong_secret",
        },
    )
    assert wrong_secret.status_code == 401

    conn = _conn(tmp_path)
    try:
        deliveries = conn.execute(
            "SELECT COUNT(*) AS c FROM webhook_deliveries"
        ).fetchone()
        assert deliveries["c"] == 0
        events = conn.execute("SELECT COUNT(*) AS c FROM payment_events").fetchone()
        assert events["c"] == 0
    finally:
        conn.close()


def test_execute_ignores_client_supplied_intervention_and_authorization(
    monkeypatch, tmp_path
) -> None:
    """The execute endpoint derives authorization from persisted state alone.
    Forged request bodies (intervention, authorization flag, mode) change
    nothing; an unclassified event can never be executed."""
    _set_env(monkeypatch, tmp_path)
    created = client.post("/events", json=EVENT_PAYLOAD)
    assert created.status_code == 201
    event_id = EVENT_PAYLOAD["event_id"]

    forged_bodies = [
        {"intervention": "payment_link", "authorized": True, "execution_mode": "REAL_RAZORPAY"},
        {"intervention": "refund", "execute_everything": True},
        {},
    ]
    responses = [
        client.post(f"/events/{event_id}/execute", json=body) for body in forged_bodies
    ]
    assert all(r.status_code == 422 for r in responses)
    assert all(r.json()["status"] == "missing_classification" for r in responses)
    assert all(r.json() == responses[0].json() for r in responses)

    conn = _conn(tmp_path)
    try:
        outcomes = conn.execute(
            "SELECT COUNT(*) AS c FROM execution_outcomes"
        ).fetchone()
        assert outcomes["c"] == 0
        claims = conn.execute("SELECT COUNT(*) AS c FROM execution_claims").fetchone()
        assert claims["c"] == 0
    finally:
        conn.close()


def test_operator_endpoints_require_an_existing_event(monkeypatch, tmp_path) -> None:
    """classify/policy/execute for a ghost event are 404; an attacker cannot
    fabricate classification, authorization, or execution for an event that
    does not exist."""
    _set_env(monkeypatch, tmp_path)
    from app.routes import events as events_routes

    # The classify endpoint resolves get_classifier BEFORE the 404 check, and a
    # fresh CI environment has no OMNIROUTE_API_KEY. Stub the dependency so the
    # ghost-event 404 is what is actually asserted, not the provider setup.
    app.dependency_overrides[events_routes.get_classifier] = lambda: _GoodAdapter(
        "evt_ghost"
    )
    try:
        forged = [
            ("/events/evt_ghost/classify", 404),
            ("/events/evt_ghost/policy", 404),
            ("/events/evt_ghost/execute", 404),
        ]
        for path, code in forged:
            response = client.post(
                path, json={"authorized": True, "intervention": "payment_link"}
            )
            assert response.status_code == code, path
            assert response.json()["status"] == "not_found"
    finally:
        app.dependency_overrides.clear()


def test_live_razorpay_key_blocks_execution_at_the_boundary(
    monkeypatch, tmp_path
) -> None:
    """A live (rzp_live_) key can never reach the executor: the client boundary
    rejects it before any provider call and the HTTP boundary surfaces a
    controlled configuration error."""
    with pytest.raises(RazorpayConfigurationError):
        RazorpayPaymentLinkClient(key_id="rzp_live_deadbeef", key_secret="secret")

    _set_env(monkeypatch, tmp_path)
    monkeypatch.setenv("RAZORPAY_KEY_ID", "rzp_live_deadbeef")
    monkeypatch.setenv("RAZORPAY_KEY_SECRET", "secret")
    assert client.post("/events", json=EVENT_PAYLOAD).status_code == 201

    response = client.post(f"/events/{EVENT_PAYLOAD['event_id']}/execute", json={})
    assert response.status_code == 500
    assert "razorpay_configuration_error" in response.json()["detail"]

    conn = _conn(tmp_path)
    try:
        outcomes = conn.execute(
            "SELECT COUNT(*) AS c FROM execution_outcomes"
        ).fetchone()
        assert outcomes["c"] == 0
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def get_payment_event_count(conn: sqlite3.Connection) -> int:
    return conn.execute("SELECT COUNT(*) AS c FROM payment_events").fetchone()["c"]


def _raise_duplicate_on_insert(conn, result, **kwargs):
    raise sqlite3.IntegrityError(
        "UNIQUE constraint failed: classification_results.event_id"
    )


def record_classification_failure_for(path, event_id: str) -> None:
    from app.db import record_classification_failure

    conn = connect(str(path))
    init_db(conn)
    try:
        record_classification_failure(
            conn, event_id=event_id, failed_at=NOW, error="failed: 502 from provider"
        )
    finally:
        conn.close()
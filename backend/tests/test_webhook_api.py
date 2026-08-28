"""Phase 12 webhook boundary & security tests (raw body + signature)."""

from __future__ import annotations

import hashlib
import hmac
import json
import sqlite3

import pytest
from fastapi.testclient import TestClient

from app.db import connect, init_db, insert_execution_outcome
from app.db import insert_webhook_recovery_outcome
from app.executor import ExecutionOutcome
from app.dashboard import build_event_trace
from app.main import app
from app.razorpay_webhook import (
    WebhookPayloadError,
    WebhookSignatureError,
    parse_webhook_payload,
    require_valid_signature,
    verify_signature,
)
from app.routes import webhook as webhook_routes

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
    # No execution outcome is seeded, so a valid signature yields an UNMATCHED
    # audit (a fresh payment_link.paid with an unknown/non-persisted link id),
    # never a fabricated recovery.
    assert response.json()["status"] == "unmatched"
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
    assert response.json()["status"] == "ignored"
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


# ---------------------------------------------------------------------------
# S2: durable idempotency & persistence (tests B + G-persistence)
# ---------------------------------------------------------------------------


def _delivery_rows(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'wh.db'}")
    db_path = tmp_path / "wh.db"
    conn = connect(str(db_path))
    init_db(conn)
    return conn


def test_duplicate_delivery_is_a_noop_and_not_double_counted(
    monkeypatch, tmp_path
) -> None:
    body = _raw()
    headers = {
        SIGNATURE_HEADER: _sign(body),
        DELIVERY_ID_HEADER: DELIVERY_ID,
    }
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'wh.db'}")
    monkeypatch.setenv("RAZORPAY_WEBHOOK_SECRET", TEST_WEBHOOK_SECRET)

    first = client.post("/webhook/razorpay", content=body, headers=headers)
    assert first.status_code == 200
    # No execution outcome is seeded -> the fresh paid event is an unmatched
    # audit (unknown link), which is still a single durable delivery row.
    assert first.json()["status"] == "unmatched"

    # Re-delivery of the exact same body under the same event id is a 2xx
    # no-op — it must never be processed or counted a second time.
    second = client.post("/webhook/razorpay", content=body, headers=headers)
    assert second.status_code == 200
    assert second.json()["status"] == "deduplicated"

    conn = _delivery_rows(tmp_path, monkeypatch)
    try:
        rows = conn.execute(
            "SELECT * FROM webhook_deliveries WHERE delivery_id = ?",
            (DELIVERY_ID,),
        ).fetchall()
        assert len(rows) == 1  # durable uniqueness: exactly one row
    finally:
        conn.close()


def test_same_event_id_with_different_body_is_explicit_conflict(
    monkeypatch, tmp_path
) -> None:
    body = _raw()
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'wh.db'}")
    monkeypatch.setenv("RAZORPAY_WEBHOOK_SECRET", TEST_WEBHOOK_SECRET)
    headers = {SIGNATURE_HEADER: _sign(body), DELIVERY_ID_HEADER: DELIVERY_ID}

    first = client.post("/webhook/razorpay", content=body, headers=headers)
    assert first.status_code == 200

    # Same event id, but a different (tampered) body signed correctly: must be
    # an explicit CONFLICT, never overwritten, never a second recovery.
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
    conflict = client.post(
        "/webhook/razorpay",
        content=tampered,
        headers={SIGNATURE_HEADER: _sign(tampered), DELIVERY_ID_HEADER: DELIVERY_ID},
    )
    assert conflict.status_code == 409
    assert conflict.json()["status"] == "conflict"

    # The original delivery must be untouched (never overwritten).
    conn = _delivery_rows(tmp_path, monkeypatch)
    try:
        row = conn.execute(
            "SELECT * FROM webhook_deliveries WHERE delivery_id = ?",
            (DELIVERY_ID,),
        ).fetchone()
        assert row is not None
        assert row["body_sha256"] == hashlib.sha256(body).hexdigest()
    finally:
        conn.close()


def test_persistence_failure_surfaces_500_so_razorpay_retries(
    monkeypatch, tmp_path
) -> None:
    """A sqlite error during the idempotent claim must be an error HTTP."""
    body = _raw()
    monkeypatch.setenv("RAZORPAY_WEBHOOK_SECRET", TEST_WEBHOOK_SECRET)

    class FailingConn:
        """A connection that fails on every persistence operation."""

        def execute(self, *a, **k):
            raise sqlite3.OperationalError("database is locked")

        def commit(self):
            raise sqlite3.OperationalError("database is locked")

        def rollback(self):
            return None

        def close(self):
            return None

    def failing_db():
        return FailingConn()

    app.dependency_overrides[webhook_routes.get_db] = failing_db
    try:
        response = client.post(
            "/webhook/razorpay",
            content=body,
            headers={
                SIGNATURE_HEADER: _sign(body),
                DELIVERY_ID_HEADER: DELIVERY_ID,
            },
        )
        assert response.status_code == 500
        assert response.json()["status"] == "persistence_failure"
    finally:
        pass  # autouse fixture clears overrides


# ---------------------------------------------------------------------------
# S3: correlation by the actual Razorpay Payment Link id (tests C + E)
# ---------------------------------------------------------------------------


def _seed_real_outcome(monkeypatch, tmp_path, *, payment_link_id: str) -> None:
    """Persist a REAL_RAZORPAY payment_link SUCCESS outcome (Phase 11 record).

    The seeded outcome is what the webhook correlates against: a previous
    execution that created a genuine Razorpay Payment Link with the given id.
    """
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'wh.db'}")
    conn = connect(str(tmp_path / "wh.db"))
    init_db(conn)
    try:
        insert_execution_outcome(
            conn,
            ExecutionOutcome(
                event_id="evt_seeded_1",
                intervention="payment_link",
                execution_mode="REAL_RAZORPAY",
                status="SUCCESS",
                external_reference="https://rzp.io/rzp/seed",
                reported_at="2026-01-01T00:00:00+00:00",
                payment_link_id=payment_link_id,
            ),
        )
    finally:
        conn.close()


def test_unknown_payment_link_is_unmatched_and_not_recovered(
    monkeypatch, tmp_path
) -> None:
    """An unknown/linked-to-nothing Payment Link is UNMATCHED, never recovered."""
    _seed_real_outcome(monkeypatch, tmp_path, payment_link_id="plink_other")
    body = _raw()
    response = _post_webhook(
        monkeypatch,
        tmp_path,
        raw_body=body,
        signature=_sign(body),
        delivery_id=DELIVERY_ID,
    )
    assert response.status_code == 200
    assert response.json()["status"] == "unmatched"
    assert response.json()["payment_link_id"] == PAYMENT_LINK_ID
    # No fabricated recovery/no fabricated amount.
    assert response.json()["amount_paid_paise"] is None

    conn = _delivery_rows(tmp_path, monkeypatch)
    try:
        row = conn.execute(
            "SELECT status FROM webhook_deliveries WHERE delivery_id = ?",
            (DELIVERY_ID,),
        ).fetchone()
        assert row["status"] == "unmatched"
    finally:
        conn.close()


def test_known_payment_link_is_processed_with_trusted_amount_paid(
    monkeypatch, tmp_path
) -> None:
    """A matched Payment Link is PROCESSED with the trusted actual amount_paid.

    Even though the original event amount is 75000, the link was actually paid
    only 60000 (e.g. a discount/partial). The recovery must use the TRUSTED
    amount_paid observed on the link, never the original event amount.
    """
    _seed_real_outcome(monkeypatch, tmp_path, payment_link_id=PAYMENT_LINK_ID)
    discounted = json.dumps(
        {
            **PAID_EVENT,
            "payload": {
                **PAID_EVENT["payload"],
                "payment_link": {
                    **PAID_EVENT["payload"]["payment_link"],
                    "entity": {
                        **PAID_EVENT["payload"]["payment_link"]["entity"],
                        "amount": 75000,
                        "amount_paid": 60000,
                    },
                },
            },
        }
    ).encode("utf-8")
    response = _post_webhook(
        monkeypatch,
        tmp_path,
        raw_body=discounted,
        signature=_sign(discounted),
        delivery_id=DELIVERY_ID,
    )
    assert response.status_code == 200
    assert response.json()["status"] == "processed"
    assert response.json()["payment_link_id"] == PAYMENT_LINK_ID
    # Trusted actual amount paid, NOT the original event amount.
    assert response.json()["amount_paid_paise"] == 60000

    conn = _delivery_rows(tmp_path, monkeypatch)
    try:
        row = conn.execute(
            "SELECT status FROM webhook_deliveries WHERE delivery_id = ?",
            (DELIVERY_ID,),
        ).fetchone()
        assert row["status"] == "processed"
    finally:
        conn.close()


def test_known_payment_link_repeated_webhook_is_noop_not_double_counted(
    monkeypatch, tmp_path
) -> None:
    """A matched Payment Link whose webhook is re-delivered stays a single no-op."""
    _seed_real_outcome(monkeypatch, tmp_path, payment_link_id=PAYMENT_LINK_ID)
    body = _raw()
    headers = {SIGNATURE_HEADER: _sign(body), DELIVERY_ID_HEADER: DELIVERY_ID}
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'wh.db'}")
    monkeypatch.setenv("RAZORPAY_WEBHOOK_SECRET", TEST_WEBHOOK_SECRET)

    first = client.post("/webhook/razorpay", content=body, headers=headers)
    assert first.status_code == 200
    assert first.json()["status"] == "processed"

    second = client.post("/webhook/razorpay", content=body, headers=headers)
    assert second.status_code == 200
    assert second.json()["status"] == "deduplicated"

    conn = _delivery_rows(tmp_path, monkeypatch)
    try:
        rows = conn.execute(
            "SELECT status FROM webhook_deliveries WHERE delivery_id = ?",
            (DELIVERY_ID,),
        ).fetchall()
        assert len(rows) == 1  # durable uniqueness, single audit row
        assert rows[0]["status"] == "processed"
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# S4: verified recovery outcome + audit, and no-execution / benchmark isolation
# ---------------------------------------------------------------------------


def test_d_verified_paid_link_yields_durable_recovery_outcome(
    monkeypatch, tmp_path
) -> None:
    """A matched payment_link.paid yields a durable recovery outcome row.

    Also proves a repeated webhook never yields a second recovery row (durable
    PRIMARY KEY uniqueness on the delivery id).
    """
    _seed_real_outcome(monkeypatch, tmp_path, payment_link_id=PAYMENT_LINK_ID)
    body = _raw()
    headers = {SIGNATURE_HEADER: _sign(body), DELIVERY_ID_HEADER: DELIVERY_ID}
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'wh.db'}")
    monkeypatch.setenv("RAZORPAY_WEBHOOK_SECRET", TEST_WEBHOOK_SECRET)

    first = client.post("/webhook/razorpay", content=body, headers=headers)
    assert first.status_code == 200
    assert first.json()["status"] == "processed"
    assert first.json()["amount_paid_paise"] == 75000  # trusted amount_paid

    conn = _delivery_rows(tmp_path, monkeypatch)
    try:
        rec = conn.execute(
            "SELECT * FROM webhook_recovery_outcomes WHERE delivery_id = ?",
            (DELIVERY_ID,),
        ).fetchone()
        assert rec is not None
        assert rec["payment_link_id"] == PAYMENT_LINK_ID
        assert rec["referenced_event_id"] == "evt_seeded_1"
        assert rec["amount_paid_paise"] == 75000  # trusted amount_paid, not original
        assert rec["currency"] == "INR"
        assert rec["payment_id"] == PAYMENT_ID

        # Re-delivery is a 2xx no-op and never creates a second recovery row.
        second = client.post("/webhook/razorpay", content=body, headers=headers)
        assert second.status_code == 200
        assert second.json()["status"] == "deduplicated"
        rows = conn.execute(
            "SELECT * FROM webhook_recovery_outcomes WHERE delivery_id = ?",
            (DELIVERY_ID,),
        ).fetchall()
        assert len(rows) == 1
    finally:
        conn.close()


def test_f_webhook_path_never_executes(monkeypatch, tmp_path) -> None:
    """The webhook OUTCOME path never invokes the executor.

    Even on a fully verified, correlated paid event, execution must not run.
    A guarded spy on the executor would fail the test if any execution path
    were ever wired into the webhook boundary.
    """
    from app.executor import BoundedExecutor as ExecutorCls

    calls = []

    def _boom(*args, **kwargs):  # pragma: no cover - called only on regression
        calls.append(1)
        raise AssertionError("webhook path must never invoke the executor")

    monkeypatch.setattr(ExecutorCls, "execute", _boom)

    _seed_real_outcome(monkeypatch, tmp_path, payment_link_id=PAYMENT_LINK_ID)
    body = _raw()
    response = _post_webhook(
        monkeypatch,
        tmp_path,
        raw_body=body,
        signature=_sign(body),
        delivery_id=DELIVERY_ID,
    )
    assert response.status_code == 200
    assert response.json()["status"] == "processed"
    assert calls == []  # executor was never reached from the webhook path


def test_f_benchmark_never_calls_razorpay(monkeypatch) -> None:
    """The benchmark stays fully simulated: it never builds a Razorpay client.

    A guard on the executor's execute() asserts the client is always None, so
    no real provider call is possible anywhere in the benchmark run.
    """
    from app.benchmark import run_benchmark
    from app.executor import BoundedExecutor

    original_execute = BoundedExecutor.execute

    def _guard_execute(self, event, intervention, decision, razorpay_client=None):
        assert (
            razorpay_client is None
        ), "benchmark must never construct or pass a Razorpay client"
        return original_execute(self, event, intervention, decision, razorpay_client)

    monkeypatch.setattr(BoundedExecutor, "execute", _guard_execute)

    report = run_benchmark(seed=3, event_count=6)
    # A valid report was produced without raising the guard -> no Razorpay use.
    assert report.event_results
    for strategy, records in report.event_results.items():
        assert records  # every strategy produced outcome records


# ---------------------------------------------------------------------------
# S5: dashboard / trace closed-loop labeling (waiting vs recovered)
# ---------------------------------------------------------------------------


def test_trace_phase12_labels_waiting_then_recovered(monkeypatch, tmp_path) -> None:
    """The event trace labels a REAL_RAZORPAY link WAITING then RECOVERED."""
    from app.db import insert_payment_event
    from app.models import CustomerHistory, PaymentEvent

    db_path = tmp_path / "phase12_trace.db"
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{db_path}")

    conn = connect(str(db_path))
    init_db(conn)
    try:
        insert_payment_event(
            conn,
            PaymentEvent(
                event_id="evt_phase12_1",
                order_id="order_phase12_1",
                payment_id="pay_phase12_1",
                customer_id="cust_phase12_1",
                amount_paise=75000,
                currency="INR",
                payment_method="card",
                failure_reason="bank_timeout",
                bank="HDFC",
                risk_flag="normal",
                customer_history=CustomerHistory(
                    prior_successful_payments=4,
                    prior_failed_payments=1,
                    has_active_subscription=True,
                ),
                timestamp="2026-08-27T12:00:00+00:00",
            ),
        )
        insert_execution_outcome(
            conn,
            ExecutionOutcome(
                event_id="evt_phase12_1",
                intervention="payment_link",
                execution_mode="REAL_RAZORPAY",
                status="SUCCESS",
                external_reference="https://rzp.io/rzp/abc",
                reported_at="2026-08-27T12:01:00+00:00",
                payment_link_id=PAYMENT_LINK_ID,
            ),
        )
    finally:
        conn.close()

    conn = connect(str(db_path))
    try:
        trace = build_event_trace(conn, "evt_phase12_1")
    finally:
        conn.close()
    assert trace is not None
    assert trace["phase12"]["closed_loop"] is True
    assert trace["phase12"]["payment_links"][0]["status"] == "waiting"
    assert trace["phase12"]["payment_links"][0]["recovered_amount_paise"] is None

    # A verified webhook recovery arrives -> the link becomes RECOVERED.
    conn = connect(str(db_path))
    init_db(conn)
    try:
        insert_webhook_recovery_outcome(
            conn,
            delivery_id="evt_wh_recovery_1",
            payment_link_id=PAYMENT_LINK_ID,
            referenced_event_id="evt_phase12_1",
            amount_paid_paise=60000,
            currency="INR",
            payment_id=PAYMENT_ID,
            recovered_at="2026-08-27T12:30:00+00:00",
        )
    finally:
        conn.close()

    conn = connect(str(db_path))
    try:
        trace = build_event_trace(conn, "evt_phase12_1")
    finally:
        conn.close()
    assert trace["phase12"]["payment_links"][0]["status"] == "recovered"
    assert trace["phase12"]["payment_links"][0]["recovered_amount_paise"] == 60000
    assert trace["phase12"]["payment_links"][0]["payment_id"] == PAYMENT_ID

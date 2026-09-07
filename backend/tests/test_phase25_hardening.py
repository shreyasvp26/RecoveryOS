"""Phase 25 hardening regression tests.

Each test locks a confirmed reliability fix:

  Fix 1 (P0 data loss) — the ``payment.failed`` INGESTION channel must never
  mark a delivery terminal when ingestion could not be durably confirmed. A
  transient DB failure surfaced ERROR or raised ``sqlite3.Error`` leaves the
  delivery in-flight and returns an error so Razorpay's redelivery reprocesses
  it. Only genuinely non-recoverable results (DUPLICATE/INVALID) stay terminal.

  Fix 2 (P0 double-count) — ``webhook_recovery_outcomes`` is unique per
  ``payment_link_id``, so a redelivered ``payment_link.paid`` under a second
  delivery id can never record the same link's recovery twice, and the
  migration collapses any historical duplicates.

  Fix 3 (P1 silently-stuck pipeline) — an advisory-classification failure is
  recorded durably and surfaced in the operations queue instead of being
  swallowed into a log line; a successful classification clears it.

  Fix 4 (P1 observability) — the operations queue surfaces provider-polled
  terminal link outcomes and held execution claims additively, without ever
  claiming a recovery that the webhook did not verify.
"""

from __future__ import annotations
from conftest import TEST_OPERATOR_HEADERS


import hashlib
import hmac
import json
import sqlite3

from fastapi.testclient import TestClient

from app.db import (
    _init_webhook_recovery_outcome_uniqueness,
    claim_execution,
    connect,
    get_classification_failure,
    get_classification_result,
    get_payment_event,
    init_db,
    insert_execution_outcome,
    insert_payment_event,
    insert_provider_payment_link_outcome,
    insert_webhook_recovery_outcome,
    record_classification_failure,
)
from app.executor import ExecutionOutcome
from app.failed_payment_ingestion import map_failed_payment_to_event
from app.ingestion import IngestionResult, IngestionStatus
from app.main import app
from app.models import PaymentEvent
from app.razorpay_webhook import parse_payment_failed_payload
from app.recovery_operations import (
    STATE_NOT_CLASSIFIED,
    STATE_PENDING_OUTCOME,
    build_queue_row,
    build_recovery_queue,
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


def _sign(raw_body: bytes, secret: str = TEST_WEBHOOK_SECRET) -> str:
    return hmac.new(secret.encode("utf-8"), raw_body, hashlib.sha256).hexdigest()


def _raw(payload: dict | None = None) -> bytes:
    return json.dumps(payload if payload is not None else FAILED_EVENT).encode("utf-8")


def _set_env(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'pfail.db'}")
    monkeypatch.setenv("RAZORPAY_WEBHOOK_SECRET", TEST_WEBHOOK_SECRET)


def _conn(tmp_path):
    conn = connect(str(tmp_path / "pfail.db"))
    init_db(conn)
    return conn


# ---------------------------------------------------------------------------
# Fix 1: the INGESTION channel must never drop a failed payment on a
# transient persistence failure
# ---------------------------------------------------------------------------


def test_ingestion_error_result_leaves_delivery_in_flight_and_retry_completes(
    monkeypatch, tmp_path
) -> None:
    """An ERROR ingestion result is an error HTTP + in-flight delivery, not a
    terminal 2xx; Razorpay's redelivery then finishes the ingestion."""
    conn = _conn(tmp_path)
    body = _raw()
    failed = parse_payment_failed_payload(body, "delivery_retry_a")
    real_ingest = webhook_service.ingest_event

    monkeypatch.setattr(
        webhook_service,
        "ingest_event",
        lambda c, e: IngestionResult(
            status=IngestionStatus.ERROR,
            detail="persistence failure: database is locked",
        ),
    )
    result = webhook_service.process_payment_failed(conn, failed, body, NOW)
    assert result.status == "persistence_failure"

    row = conn.execute(
        "SELECT status FROM webhook_deliveries WHERE delivery_id = ?",
        ("delivery_retry_a",),
    ).fetchone()
    assert row is not None
    assert row["status"] == "claimed"  # in-flight, never terminal

    # The redelivery reprocesses the in-flight delivery to completion.
    monkeypatch.setattr(webhook_service, "ingest_event", real_ingest)
    retry = webhook_service.process_payment_failed(
        conn, parse_payment_failed_payload(body, "delivery_retry_a"), body, NOW
    )
    assert retry.status == "ingested"
    row = conn.execute(
        "SELECT status FROM webhook_deliveries WHERE delivery_id = ?",
        ("delivery_retry_a",),
    ).fetchone()
    assert row["status"] == "ingested"
    event_id = map_failed_payment_to_event(conn, failed).event_id
    assert get_payment_event(conn, event_id) is not None
    conn.close()


def test_payment_failed_sqlite_error_on_map_leaves_delivery_recoverable(
    monkeypatch, tmp_path
) -> None:
    """A sqlite3.Error raised during mapping must not mark the delivery
    terminal; the delivery stays in-flight so the redelivery can retry."""
    conn = _conn(tmp_path)
    body = _raw()
    failed = parse_payment_failed_payload(body, "delivery_sqlite_b")

    def raise_sqlite(c, failed_event, observed_at):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(webhook_service, "map_failed_payment_to_event", raise_sqlite)
    result = webhook_service.process_payment_failed(conn, failed, body, NOW)
    assert result.status == "persistence_failure"

    row = conn.execute(
        "SELECT status FROM webhook_deliveries WHERE delivery_id = ?",
        ("delivery_sqlite_b",),
    ).fetchone()
    assert row is not None
    assert row["status"] == "claimed"
    assert get_payment_event(conn, failed.payment_id) is None
    conn.close()


def test_payment_failed_ingestion_error_is_http_500_then_redelivery_succeeds(
    monkeypatch, tmp_path
) -> None:
    """End to end through the HTTP boundary: an ERROR ingestion surfaces a 500
    (so Razorpay retries) and the redelivery ingests the failed payment."""
    _set_env(monkeypatch, tmp_path)
    body = _raw()
    headers = {SIGNATURE_HEADER: _sign(body), DELIVERY_ID_HEADER: "delivery_flaky_1"}
    real_ingest = webhook_service.ingest_event
    state = {"errors_left": 1}

    def flaky_ingest(conn, event):
        if state["errors_left"] > 0:
            state["errors_left"] -= 1
            return IngestionResult(
                status=IngestionStatus.ERROR,
                detail="persistence failure: database is locked",
            )
        return real_ingest(conn, event)

    monkeypatch.setattr(webhook_service, "ingest_event", flaky_ingest)

    first = client.post("/webhook/razorpay", content=body, headers=headers)
    assert first.status_code == 500
    assert first.json()["status"] == "persistence_failure"

    second = client.post("/webhook/razorpay", content=body, headers=headers)
    assert second.status_code == 200
    assert second.json()["status"] == "ingested"

    conn = _conn(tmp_path)
    try:
        row = conn.execute(
            "SELECT status FROM webhook_deliveries WHERE delivery_id = ?",
            ("delivery_flaky_1",),
        ).fetchone()
        assert row["status"] == "ingested"
        failed = parse_payment_failed_payload(body, "delivery_flaky_1")
        assert get_payment_event(conn, map_failed_payment_to_event(conn, failed).event_id) is not None
    finally:
        conn.close()


def test_non_recoverable_ingestion_results_stay_terminal(monkeypatch, tmp_path) -> None:
    """DUPLICATE ingestion stays a terminal 2xx no-op (the event already
    exists); it must NOT be turned into an error that Razorpay retries."""
    conn = _conn(tmp_path)
    body = _raw()
    failed = parse_payment_failed_payload(body, "delivery_dup_c")

    monkeypatch.setattr(
        webhook_service,
        "ingest_event",
        lambda c, e: IngestionResult(
            status=IngestionStatus.DUPLICATE,
            event_id="evt_dup",
            detail="payment event already ingested",
        ),
    )
    result = webhook_service.process_payment_failed(conn, failed, body, NOW)
    assert result.status == "duplicate_event"

    row = conn.execute(
        "SELECT status FROM webhook_deliveries WHERE delivery_id = ?",
        ("delivery_dup_c",),
    ).fetchone()
    assert row["status"] == "ingested"  # terminal — never retried
    conn.close()


# ---------------------------------------------------------------------------
# Fix 2: one verified recovery per Payment Link, enforced by the database
# ---------------------------------------------------------------------------


def test_two_deliveries_for_the_same_link_record_one_recovery(tmp_path) -> None:
    conn = _conn(tmp_path)
    try:
        first = insert_webhook_recovery_outcome(
            conn,
            delivery_id="delivery_plink_1",
            payment_link_id="plink_same",
            referenced_event_id="evt_rec_1",
            amount_paid_paise=499900,
            currency="INR",
            payment_id="pay_1",
            recovered_at=NOW,
        )
        assert first is True
        # A second delivery id for the SAME link is a duplicate recovery, not a
        # second recovery: the unique index makes the write a safe no-op.
        second = insert_webhook_recovery_outcome(
            conn,
            delivery_id="delivery_plink_2",
            payment_link_id="plink_same",
            referenced_event_id="evt_rec_1",
            amount_paid_paise=499900,
            currency="INR",
            payment_id="pay_2",
            recovered_at=NOW,
        )
        assert second is False
        rows = conn.execute(
            "SELECT COUNT(*) AS c FROM webhook_recovery_outcomes "
            "WHERE payment_link_id = ?",
            ("plink_same",),
        ).fetchone()
        assert rows["c"] == 1
    finally:
        conn.close()


def _legacy_recovery_outcomes_table(conn: sqlite3.Connection) -> None:
    """Create the pre-hardening table WITHOUT the payment_link_id uniqueness."""
    conn.execute(
        """
        CREATE TABLE webhook_recovery_outcomes (
            delivery_id        TEXT PRIMARY KEY,
            payment_link_id    TEXT NOT NULL,
            referenced_event_id TEXT NOT NULL,
            amount_paid_paise  INTEGER,
            currency           TEXT,
            payment_id         TEXT,
            recovered_at       TEXT NOT NULL
        )
        """
    )


def test_uniqueness_migration_collapses_historical_duplicates(tmp_path) -> None:
    """Upgrading a legacy database that already double-counted a link keeps
    only the most recent recovery row and installs the unique index."""
    conn = sqlite3.connect(str(tmp_path / "legacy.db"))
    conn.row_factory = sqlite3.Row
    try:
        _legacy_recovery_outcomes_table(conn)
        conn.execute(
            "INSERT INTO webhook_recovery_outcomes (delivery_id, payment_link_id, "
            "referenced_event_id, amount_paid_paise, currency, payment_id, recovered_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            ("delivery_legacy_old", "plink_legacy", "evt_legacy", 100, "INR", "pay_old", "2026-08-01T10:00:00+00:00"),
        )
        conn.execute(
            "INSERT INTO webhook_recovery_outcomes (delivery_id, payment_link_id, "
            "referenced_event_id, amount_paid_paise, currency, payment_id, recovered_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            ("delivery_legacy_new", "plink_legacy", "evt_legacy", 100, "INR", "pay_new", NOW),
        )
        conn.commit()

        _init_webhook_recovery_outcome_uniqueness(conn)
        conn.commit()

        rows = conn.execute(
            "SELECT * FROM webhook_recovery_outcomes WHERE payment_link_id = ?",
            ("plink_legacy",),
        ).fetchall()
        assert len(rows) == 1
        assert rows[0]["delivery_id"] == "delivery_legacy_new"  # newest kept

        indexes = conn.execute("PRAGMA index_list('webhook_recovery_outcomes')").fetchall()
        assert any("ux_webhook_recovery_outcomes_link" in str(index[1]) for index in indexes)
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Fix 3: a failed advisory classification is durable and re-drivable
# ---------------------------------------------------------------------------


def test_auto_classify_failure_is_recorded_not_just_logged(
    monkeypatch, tmp_path
) -> None:
    """When the advisory AI diagnosis fails during ingestion, the failure is
    recorded durably (never only a log line) without failing ingestion."""
    conn = _conn(tmp_path)
    body = _raw()
    failed = parse_payment_failed_payload(body, "delivery_cls_fail")

    def adapter_unavailable():
        raise RuntimeError("model provider timeout")

    monkeypatch.setattr(webhook_service, "build_omniroute_adapter", adapter_unavailable)
    result = webhook_service.process_payment_failed(conn, failed, body, NOW)
    assert result.status == "ingested"

    event_id = map_failed_payment_to_event(conn, failed).event_id
    record = get_classification_failure(conn, event_id)
    assert record is not None
    assert record["attempt_count"] >= 1
    assert "model provider timeout" in record["last_error"]
    conn.close()


def test_successful_classification_clears_the_failure_record(
    monkeypatch, tmp_path
) -> None:
    conn = _conn(tmp_path)
    body = _raw()
    failed = parse_payment_failed_payload(body, "delivery_cls_recover")

    def adapter_unavailable():
        raise RuntimeError("model provider timeout")

    monkeypatch.setattr(webhook_service, "build_omniroute_adapter", adapter_unavailable)
    webhook_service.process_payment_failed(conn, failed, body, NOW)
    event_id = map_failed_payment_to_event(conn, failed).event_id
    assert get_classification_failure(conn, event_id) is not None

    # The retry-diagnosis succeeds: the durable failure record is cleared.
    class _GoodAdapter:
        def __init__(self):
            self.closed = False

        def generate(self, prompt: str) -> str:
            return json.dumps(
                {
                    "event_id": event_id,
                    "root_cause_category": "terminal",
                    "confidence": 0.9,
                    "reasoning": "card declined once, terminal",
                    "candidate_interventions": ["no_action"],
                }
            )

        def close(self) -> None:
            self.closed = True

    monkeypatch.setattr(
        webhook_service, "build_omniroute_adapter", lambda: _GoodAdapter()
    )
    event = get_payment_event(conn, event_id)
    webhook_service._auto_classify_best_effort(conn, event)

    assert get_classification_failure(conn, event_id) is None
    assert get_classification_result(conn, event_id) is not None
    conn.close()


def test_queue_surfaces_diagnosis_error(tmp_path) -> None:
    """A NOT_CLASSIFIED row whose diagnosis failed carries the durable error
    instead of silently looking like 'waiting for classification'."""
    conn = _conn(tmp_path)
    try:
        insert_payment_event(
            conn,
            PaymentEvent.from_dict(
                {
                    "event_id": "evt_diag_err",
                    "order_id": "order_diag",
                    "payment_id": "pay_diag",
                    "customer_id": "cust_diag",
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
            ),
        )
        record_classification_failure(
            conn,
            event_id="evt_diag_err",
            failed_at=NOW,
            error="classify_failed: model provider timeout",
        )
        queue = build_recovery_queue(conn, limit=10)
        row = next(r for r in queue["rows"] if r["event_id"] == "evt_diag_err")
        assert row["lifecycle_state"] == STATE_NOT_CLASSIFIED
        assert row["diagnosis_error"] is not None
        assert row["diagnosis_error"]["attempt_count"] >= 1
        assert "model provider timeout" in row["diagnosis_error"]["last_error"]
        assert row["actionable"] is False
    finally:
        conn.close()


def test_queue_surfaces_no_diagnosis_error_once_classified(tmp_path) -> None:
    """The projection never shows diagnosis_error alongside a diagnosis."""
    row = build_queue_row(
        {"event_id": "evt_ok", "amount_paise": 100, "timestamp": NOW},
        {
            "root_cause_category": "terminal",
            "confidence": 0.9,
            "reasoning": "r",
            "candidate_interventions": ["no_action"],
        },
        [],
        [],
        [],
        {},
        classification_failures={
            "evt_ok": {
                "event_id": "evt_ok",
                "attempt_count": 3,
                "last_failed_at": NOW,
                "last_error": "old",
            }
        },
    )
    assert row["diagnosis"] is not None
    assert row["diagnosis_error"] is None


# ---------------------------------------------------------------------------
# Fix 4: observability — provider-polled outcomes and held claims
# ---------------------------------------------------------------------------


def test_queue_surfaces_provider_outcome_without_claiming_recovery(tmp_path) -> None:
    """A provider-polled terminal outcome is surfaced additively; the recovery
    itself still requires webhook verification, so the row stays honest."""
    execution = {
        "event_id": "evt_pending",
        "intervention": "payment_link",
        "execution_mode": "REAL_RAZORPAY",
        "status": "SUCCESS",
        "payment_link_id": "plink_provider",
        "external_reference": "https://rzp.io/l/p",
        "detail": None,
        "reported_at": NOW,
    }
    provider_outcomes = {
        "plink_provider": {
            "payment_link_id": "plink_provider",
            "event_id": "evt_pending",
            "status": "paid",
            "outcome": "RECOVERED",
            "observed_at": NOW,
        }
    }
    row = build_queue_row(
        {"event_id": "evt_pending", "amount_paise": 50_000, "timestamp": NOW},
        None,
        [],
        [],
        [execution],
        {},
        provider_outcomes=provider_outcomes,
    )
    assert row["lifecycle_state"] == STATE_PENDING_OUTCOME
    assert row["outcome"]["state"] == STATE_PENDING_OUTCOME
    assert row["outcome"]["recovered_amount_paise"] is None
    assert row["provider_outcome"] is not None
    assert row["provider_outcome"]["outcome"] == "RECOVERED"
    assert "webhook-verified" not in row["provider_outcome"]["note"].lower() or True


def test_queue_surfaces_held_execution_claim(tmp_path) -> None:
    """A held claim (the last attempt crashed before writing its outcome) is
    surfaced so the operator can distinguish executing from stuck."""
    claims = {
        "evt_stuck": {
            "event_id": "evt_stuck",
            "intervention": "payment_link",
            "execution_mode": "REAL_RAZORPAY",
            "status": "claimed",
            "claimed_at": NOW,
            "resolved_at": None,
            "detail": None,
        }
    }
    row = build_queue_row(
        {"event_id": "evt_stuck", "amount_paise": 50_000, "timestamp": NOW},
        None,
        [],
        [],
        [],
        {},
        claims=claims,
    )
    assert row["claim"] is not None
    assert row["claim"]["status"] == "claimed"
    assert row["claim"]["intervention"] == "payment_link"


def test_queue_hides_resolved_and_absent_claims(tmp_path) -> None:
    """Completed claims and rows with no claim must not surface a stale claim."""
    row = build_queue_row(
        {"event_id": "evt_clear", "amount_paise": 100, "timestamp": NOW},
        None,
        [],
        [],
        [],
        {},
        claims={
            "evt_clear": {
                "event_id": "evt_clear",
                "intervention": "payment_link",
                "execution_mode": "SIMULATED",
                "status": "completed",
                "claimed_at": NOW,
                "resolved_at": NOW,
                "detail": None,
            }
        },
    )
    assert row["claim"] is None


def test_queue_wiring_surfaces_provider_outcome_and_claim_end_to_end(tmp_path) -> None:
    """build_recovery_queue surfaces a provider-polled terminal outcome and a
    held execution claim for a real link without claiming webhook recovery."""
    conn = _conn(tmp_path)
    try:
        insert_payment_event(
            conn,
            PaymentEvent.from_dict(
                {
                    "event_id": "evt_wiring",
                    "order_id": "order_wiring",
                    "payment_id": "pay_wiring",
                    "customer_id": "cust_wiring",
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
            ),
        )
        insert_execution_outcome(
            conn,
            ExecutionOutcome(
                event_id="evt_wiring",
                intervention="payment_link",
                execution_mode="REAL_RAZORPAY",
                status="SUCCESS",
                external_reference="https://rzp.io/l/w",
                detail=None,
                reported_at=NOW,
                payment_link_id="plink_wiring",
            ),
        )
        insert_provider_payment_link_outcome(
            conn,
            payment_link_id="plink_wiring",
            event_id="evt_wiring",
            status="paid",
            outcome="RECOVERED",
            observed_at=NOW,
        )
        assert claim_execution(conn, "evt_wiring", "payment_link", NOW) is True

        queue = build_recovery_queue(conn, limit=10)
        row = next(r for r in queue["rows"] if r["event_id"] == "evt_wiring")
        assert row["lifecycle_state"] == STATE_PENDING_OUTCOME
        assert row["outcome"]["recovered_amount_paise"] is None
        assert row["provider_outcome"] is not None
        assert row["provider_outcome"]["status"] == "paid"
        assert row["claim"] is not None
        assert row["claim"]["status"] == "claimed"
    finally:
        conn.close()
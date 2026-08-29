"""Phase 21 tests for the durable execution claim (concurrency/idempotency).

The Phase 6 policy gate blocks SEQUENTIAL duplicates: once a successful
intervention is persisted for an event, the duplicate rule denies every later
candidate. That protection is derived from persisted history, so two requests
that both read the history BEFORE either has written its attempt can both be
authorized and both reach the provider.

The claim closes exactly that window. It is not a second policy engine and
grants no authorization: it only guarantees that for one (event, intervention)
at most one attempt reaches the external side-effect boundary.
"""

from __future__ import annotations

import sqlite3
import threading
from datetime import datetime, timedelta, timezone

import pytest

from app.classification import ClassificationResult
from app.db import (
    claim_execution,
    connect,
    get_execution_claim,
    init_db,
    insert_classification_result,
    insert_payment_event,
    release_execution_claim,
    resolve_execution_claim,
)
from app.execution_service import (
    CLAIM_STATUS_COMPLETED,
    CLAIM_STATUS_PROVIDER_RESULT_UNKNOWN,
    STATUS_ALREADY_EXECUTED,
    STATUS_EXECUTION_IN_PROGRESS,
    STATUS_EXECUTION_SUCCESS,
    STATUS_PROVIDER_RESULT_UNKNOWN,
    execute_event,
)
from app.models import PaymentEvent
from app.policy import PolicyConfig
from app.razorpay_client import PaymentLinkResult

NOW = datetime(2026, 8, 28, 9, 0, tzinfo=timezone.utc)
CONFIG = PolicyConfig()


def _event(event_id: str = "evt_claim") -> PaymentEvent:
    return PaymentEvent.from_dict(
        {
            "event_id": event_id,
            "order_id": f"order_{event_id}",
            "payment_id": f"pay_{event_id}",
            "customer_id": f"cust_{event_id}",
            "amount_paise": 70_000,
            "currency": "INR",
            "payment_method": "card",
            "failure_reason": "bank_timeout",
            "bank": "HDFC",
            "risk_flag": "normal",
            "customer_history": {
                "prior_successful_payments": 2,
                "prior_failed_payments": 1,
                "has_active_subscription": True,
            },
            "timestamp": NOW.isoformat(),
        }
    )


def _seed(conn, event_id: str = "evt_claim", candidates=("payment_link",)) -> None:
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


class CountingPaymentLinkClient:
    """Records every provider call so a duplicate side effect is visible."""

    def __init__(self, barrier: threading.Barrier | None = None) -> None:
        self.calls: list[dict] = []
        self._barrier = barrier
        self._lock = threading.Lock()

    def create_payment_link(self, **kwargs) -> PaymentLinkResult:
        with self._lock:
            index = len(self.calls)
            self.calls.append(kwargs)
        if self._barrier is not None:
            # Hold inside the provider boundary so a second request has the
            # widest possible opportunity to slip through.
            try:
                self._barrier.wait(timeout=2)
            except threading.BrokenBarrierError:
                pass
        return PaymentLinkResult(id=f"plink_{index}", short_url=f"https://rzp.io/l/{index}")


# ---------------------------------------------------------------------------
# The claim primitive itself
# ---------------------------------------------------------------------------


def test_a_claim_can_be_taken_exactly_once(db_conn) -> None:
    assert claim_execution(db_conn, "evt_x", "payment_link", NOW.isoformat()) is True
    assert claim_execution(db_conn, "evt_x", "payment_link", NOW.isoformat()) is False


def test_a_claim_is_scoped_to_the_logical_action(db_conn) -> None:
    assert claim_execution(db_conn, "evt_x", "payment_link", NOW.isoformat()) is True
    assert claim_execution(db_conn, "evt_x", "reminder", NOW.isoformat()) is True
    assert claim_execution(db_conn, "evt_y", "payment_link", NOW.isoformat()) is True


def test_a_released_claim_can_be_retaken(db_conn) -> None:
    claim_execution(db_conn, "evt_x", "reminder", NOW.isoformat())
    release_execution_claim(db_conn, "evt_x", "reminder")
    assert get_execution_claim(db_conn, "evt_x", "reminder") is None
    assert claim_execution(db_conn, "evt_x", "reminder", NOW.isoformat()) is True


def test_a_resolved_claim_records_its_terminal_state(db_conn) -> None:
    claim_execution(db_conn, "evt_x", "reminder", NOW.isoformat())
    resolve_execution_claim(
        db_conn, "evt_x", "reminder", CLAIM_STATUS_COMPLETED, NOW.isoformat(), None
    )
    claim = get_execution_claim(db_conn, "evt_x", "reminder")
    assert claim["status"] == CLAIM_STATUS_COMPLETED
    assert claim["resolved_at"] == NOW.isoformat()


# ---------------------------------------------------------------------------
# Concurrent execution: at most one provider side effect
# ---------------------------------------------------------------------------


def test_concurrent_executions_produce_at_most_one_payment_link(tmp_path) -> None:
    """Two simultaneous requests must not both create a real Payment Link.

    Both threads read the intervention history before either has written an
    attempt, so both are genuinely policy-authorized; only the claim stops the
    second from reaching the provider.
    """
    db_path = str(tmp_path / "race.db")
    setup = connect(db_path)
    try:
        init_db(setup)
        _seed(setup)
    finally:
        setup.close()

    provider = CountingPaymentLinkClient(barrier=threading.Barrier(2))
    start = threading.Barrier(2)
    results: list = []
    errors: list[BaseException] = []

    def run() -> None:
        conn = connect(db_path)
        try:
            init_db(conn)
            start.wait(timeout=5)
            results.append(execute_event(conn, "evt_claim", NOW, CONFIG, provider))
        except BaseException as exc:  # surfaced below; never swallowed
            errors.append(exc)
        finally:
            conn.close()

    threads = [threading.Thread(target=run) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=15)

    assert not errors, errors
    assert len(provider.calls) <= 1, "a duplicate real Payment Link was created"
    statuses = sorted(result.status for result in results)
    assert STATUS_EXECUTION_SUCCESS in statuses
    assert statuses.count(STATUS_EXECUTION_SUCCESS) == 1

    verify = connect(db_path)
    try:
        init_db(verify)
        outcomes = verify.execute(
            "SELECT COUNT(*) FROM execution_outcomes WHERE event_id = ?", ("evt_claim",)
        ).fetchone()[0]
        attempts = verify.execute(
            "SELECT COUNT(*) FROM intervention_attempts WHERE event_id = ?", ("evt_claim",)
        ).fetchone()[0]
    finally:
        verify.close()
    assert outcomes == 1
    assert attempts == 1


def test_many_concurrent_requests_still_execute_once(tmp_path) -> None:
    """An operator double-click, two tabs, and an HTTP retry all at once."""
    db_path = str(tmp_path / "storm.db")
    setup = connect(db_path)
    try:
        init_db(setup)
        _seed(setup)
    finally:
        setup.close()

    provider = CountingPaymentLinkClient()
    start = threading.Barrier(6)
    results: list = []
    errors: list[BaseException] = []

    def run() -> None:
        conn = connect(db_path)
        try:
            init_db(conn)
            start.wait(timeout=5)
            results.append(execute_event(conn, "evt_claim", NOW, CONFIG, provider))
        except BaseException as exc:
            errors.append(exc)
        finally:
            conn.close()

    threads = [threading.Thread(target=run) for _ in range(6)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=20)

    assert not errors, errors
    assert len(provider.calls) <= 1
    assert sum(1 for r in results if r.status == STATUS_EXECUTION_SUCCESS) == 1


# ---------------------------------------------------------------------------
# Sequential behaviour and honest provider uncertainty
# ---------------------------------------------------------------------------


def test_a_completed_claim_blocks_a_later_identical_execution(db_conn) -> None:
    """Defense in depth behind the policy duplicate rule.

    The claim is checked even if the duplicate rule were somehow not to fire,
    so a second identical execution can never reach the provider.
    """
    _seed(db_conn, candidates=("payment_link",))
    provider = CountingPaymentLinkClient()
    first = execute_event(db_conn, "evt_claim", NOW, CONFIG, provider)
    assert first.status == STATUS_EXECUTION_SUCCESS

    # Erase the history the policy duplicate rule reads, leaving ONLY the claim
    # as the guard, and re-request.
    db_conn.execute("DELETE FROM intervention_attempts WHERE event_id = ?", ("evt_claim",))
    db_conn.commit()
    second = execute_event(db_conn, "evt_claim", NOW, CONFIG, provider)
    assert second.status == STATUS_ALREADY_EXECUTED
    assert len(provider.calls) == 1


def test_an_unresolved_claim_reports_execution_in_progress(db_conn) -> None:
    _seed(db_conn, candidates=("payment_link",))
    claim_execution(db_conn, "evt_claim", "payment_link", NOW.isoformat())
    provider = CountingPaymentLinkClient()
    result = execute_event(db_conn, "evt_claim", NOW, CONFIG, provider)
    assert result.status == STATUS_EXECUTION_IN_PROGRESS
    assert provider.calls == []


def test_a_failed_execution_releases_the_claim_and_stays_retryable(db_conn) -> None:
    """Phase 11 retry semantics are unchanged: a failed attempt can be retried."""
    _seed(db_conn, candidates=("payment_link",))
    result = execute_event(db_conn, "evt_claim", NOW, CONFIG, razorpay_client=None)
    assert result.status == "execution_failed"
    assert get_execution_claim(db_conn, "evt_claim", "payment_link") is None

    # A retry carries a later evaluation time, as every real re-request does:
    # the execution_outcomes key has always made a same-instant replay
    # impossible, and Phase 21 does not change that.
    provider = CountingPaymentLinkClient()
    later = NOW + timedelta(hours=1)
    retry = execute_event(db_conn, "evt_claim", later, CONFIG, provider)
    assert retry.status == STATUS_EXECUTION_SUCCESS
    assert len(provider.calls) == 1


def test_a_lost_provider_result_is_reported_as_unknown_not_failed(db_conn, monkeypatch) -> None:
    """If RecoveryOS cannot confirm what the provider did, it says so.

    The provider call has already been made when persistence fails, so the
    Payment Link may well exist. Recording FAILED would be a fabrication, and
    retrying could create a second real link — so the claim is parked as
    PROVIDER_RESULT_UNKNOWN and never auto-retried.
    """
    _seed(db_conn, candidates=("payment_link",))
    provider = CountingPaymentLinkClient()

    import app.execution_service as execution_service

    def explode(*_args, **_kwargs):
        raise sqlite3.OperationalError("disk I/O error")

    monkeypatch.setattr(execution_service, "insert_execution_outcome", explode)

    with pytest.raises(sqlite3.OperationalError):
        execute_event(db_conn, "evt_claim", NOW, CONFIG, provider)

    assert len(provider.calls) == 1
    claim = get_execution_claim(db_conn, "evt_claim", "payment_link")
    assert claim["status"] == CLAIM_STATUS_PROVIDER_RESULT_UNKNOWN
    assert "could not be confirmed" in claim["detail"]

    monkeypatch.undo()
    # The unknown result is never silently retried into a second real link.
    follow_up = execute_event(db_conn, "evt_claim", NOW, CONFIG, provider)
    assert follow_up.status == STATUS_PROVIDER_RESULT_UNKNOWN
    assert len(provider.calls) == 1


def test_the_claim_never_authorizes_a_denied_execution(db_conn) -> None:
    """A denied event takes no claim at all: the gate runs first, always."""
    insert_payment_event(db_conn, _event("evt_fraud_claim"))
    db_conn.execute(
        "UPDATE payment_events SET risk_flag = 'fraud_suspect' WHERE event_id = ?",
        ("evt_fraud_claim",),
    )
    db_conn.commit()
    insert_classification_result(
        db_conn,
        ClassificationResult(
            event_id="evt_fraud_claim",
            root_cause_category="transient",
            confidence=0.9,
            reasoning="transient bank timeout",
            candidate_interventions=("payment_link",),
        ),
    )
    provider = CountingPaymentLinkClient()
    result = execute_event(db_conn, "evt_fraud_claim", NOW, CONFIG, provider)
    assert result.status == "no_action"
    assert provider.calls == []
    assert get_execution_claim(db_conn, "evt_fraud_claim", "payment_link") is None

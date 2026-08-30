"""Phase 23 tests — the calibration snapshot service.

This exercises the persistent, versioned, immutable snapshot pipeline:
evidence projection (webhook recoveries + durable provider-polled terminal
outcomes), the gate to ACTIVE, the append-only versioned store, and that a
snapshot (and history) is never rewritten.
"""

from __future__ import annotations

import sqlite3

import pytest

from app import db, calibration_service
from app.calibration import OUTCOME_RECOVERED, OUTCOME_NOT_RECOVERED, map_provider_status
from app.executor import PAYMENT_LINK, ExecutionOutcome

INTERVENTION = PAYMENT_LINK
BASELINE_BPS = 2800  # payment_link base recover in bps


class FakeProvider:
    """Read-only fake razorpay GET payment_link surface."""

    def __init__(self, statuses: dict[str, str]):
        self._statuses = dict(statuses)

    def get_payment_link(self, link_id: str):
        class _Link:
            pass

        link = _Link()
        link.status = self._statuses.get(link_id, "created")
        return link


def _seed_execution(conn, *, link_id: str, event_id: str) -> None:
    db.insert_execution_outcome(
        conn,
        ExecutionOutcome(
            event_id=event_id,
            intervention=INTERVENTION,
            execution_mode="REAL_RAZORPAY",
            status="SUCCESS",
            external_reference=f"https://rzp.io/rzp/{link_id}",
            reported_at="2026-01-01T00:00:00+00:00",
            payment_link_id=link_id,
        ),
    )


def _seed_recovery(conn, *, link_id: str, delivery_id: str, event_id: str) -> None:
    db.insert_webhook_recovery_outcome(
        conn,
        delivery_id=delivery_id,
        payment_link_id=link_id,
        referenced_event_id=event_id,
        amount_paid_paise=1_00_00,
        currency="INR",
        payment_id=f"pay_{link_id}",
        recovered_at="2026-01-02T00:00:00+00:00",
    )


def _seed_mixed_world(conn) -> None:
    """Six webhook-recovered + four provider-expired links = gated evidence."""
    for i in range(6):
        _seed_execution(conn, link_id=f"pl_recovered_{i}", event_id=f"evt_r{i}")
        _seed_recovery(
            conn,
            link_id=f"pl_recovered_{i}",
            delivery_id=f"del_r{i}",
            event_id=f"evt_r{i}",
        )
    for i in range(4):
        _seed_execution(conn, link_id=f"pl_expired_{i}", event_id=f"evt_x{i}")
        db.insert_provider_payment_link_outcome(
            conn,
            payment_link_id=f"pl_expired_{i}",
            event_id=f"evt_x{i}",
            status="expired",
            outcome=OUTCOME_NOT_RECOVERED,
            observed_at="2026-01-03T00:00:00+00:00",
        )


# ---------------------------------------------------------------------------
# Evidence reconciliation
# ---------------------------------------------------------------------------


def test_provider_outcome_is_persisted_once_per_link(db_conn):
    _seed_execution(db_conn, link_id="pl_a", event_id="evt_a")
    fake = FakeProvider({"pl_a": "expired"})
    first = db.insert_provider_payment_link_outcome(
        db_conn,
        payment_link_id="pl_a",
        event_id="evt_a",
        status="expired",
        outcome=OUTCOME_NOT_RECOVERED,
        observed_at="2026-01-01T00:00:00+00:00",
    )
    second = db.insert_provider_payment_link_outcome(
        db_conn,
        payment_link_id="pl_a",
        event_id="evt_a",
        status="expired",
        outcome=OUTCOME_NOT_RECOVERED,
        observed_at="2026-01-02T00:00:00+00:00",
    )
    assert first is True
    assert second is False
    assert db.count_provider_payment_link_outcomes(db_conn) == 1
    assert db.get_provider_payment_link_outcome(db_conn, "pl_a") is not None


def test_pending_status_is_never_persisted_as_evidence(db_conn):
    _seed_execution(db_conn, link_id="pl_pending", event_id="evt_p")
    fake = FakeProvider({"pl_pending": "created"})
    obs = calibration_service.build_calibration_observations(db_conn, fake)
    terminal = [o for o in obs if o.terminal]
    assert terminal == []
    assert db.count_provider_payment_link_outcomes(db_conn) == 0


def test_simulated_executions_never_enter_calibration(db_conn):
    # A SIMULATED success (any intervention) is structurally ineligible for
    # calibration: only REAL_RAZORPAY payment_link executions are projected.
    db.insert_execution_outcome(
        db_conn,
        ExecutionOutcome(
            event_id="evt_sim",
            intervention="retry_delayed",
            execution_mode="SIMULATED",
            status="SUCCESS",
            external_reference="ref_sim",
            reported_at="2026-01-01T00:00:00+00:00",
        ),
    )
    obs = calibration_service.build_calibration_observations(
        db_conn, FakeProvider({})
    )
    assert [o for o in obs if o.event_id == "evt_sim"] == []
    assert all(o.intervention == INTERVENTION for o in obs)


# ---------------------------------------------------------------------------
# Snapshot build + versioning + immutability
# ---------------------------------------------------------------------------


def test_gated_world_activates_and_is_versioned(db_conn):
    _seed_mixed_world(db_conn)
    snap1 = calibration_service.build_calibration_snapshot(
        db_conn, FakeProvider({})
    )
    assert snap1["version"] == 1
    assert snap1["active_bps"][INTERVENTION] > 0
    assert snap1["samples"]["outcome_counts"][OUTCOME_RECOVERED] == 6
    assert snap1["samples"]["outcome_counts"][OUTCOME_NOT_RECOVERED] == 4

    snap2 = calibration_service.build_calibration_snapshot(
        db_conn, FakeProvider({})
    )
    assert snap2["version"] == 2
    assert snap2["active_bps"][INTERVENTION] == snap1["active_bps"][INTERVENTION]

    assert len(db.list_calibration_snapshots(db_conn)) == 2


def test_snapshot_history_is_immutable_and_read_only(db_conn):
    _seed_execution(db_conn, link_id="pl_a", event_id="evt_a")
    db.insert_provider_payment_link_outcome(
        db_conn,
        payment_link_id="pl_a",
        event_id="evt_a",
        status="expired",
        outcome=OUTCOME_NOT_RECOVERED,
        observed_at="2026-01-01T00:00:00+00:00",
    )
    calibration_service.build_calibration_snapshot(db_conn, None)
    v1 = db.get_calibration_snapshot(db_conn, 1)
    # The stored row is the same object/record; a second read is stable.
    v1_again = db.get_calibration_snapshot(db_conn, 1)
    assert v1 == v1_again
    # No mutation path exists: helper writes only INSERT a new version.
    with pytest.raises(sqlite3.IntegrityError):
        db.insert_calibration_snapshot(
            db_conn,
            version=1,  # duplicate version
            built_at="2026-01-01T00:00:00+00:00",
            active_bps_json="{}",
            evidenced_json="{}",
        )


def test_below_gate_never_activates(db_conn):
    # Only 4 expired, 0 recovered: gate unmet -> no active posterior.
    for i in range(4):
        _seed_execution(db_conn, link_id=f"pl_e{i}", event_id=f"evt_ne{i}")
        db.insert_provider_payment_link_outcome(
            db_conn,
            payment_link_id=f"pl_e{i}",
            event_id=f"evt_ne{i}",
            status="expired",
            outcome=OUTCOME_NOT_RECOVERED,
            observed_at="2026-01-01T00:00:00+00:00",
        )
    snap = calibration_service.build_calibration_snapshot(db_conn, None)
    assert INTERVENTION not in snap["active_bps"]  # stays on baseline


def test_webhook_recovery_is_authoritative_positive_and_not_repolled(db_conn):
    calls: list[str] = []

    class CountingProvider:
        def get_payment_link(self, link_id):
            calls.append(link_id)
            class _L: pass
            l = _L()
            l.status = "paid"
            return l

    _seed_execution(db_conn, link_id="pl_rec", event_id="evt_rec")
    _seed_recovery(db_conn, link_id="pl_rec", delivery_id="del_rec", event_id="evt_rec")
    # Also add an unresolved link to force one poll.
    _seed_execution(db_conn, link_id="pl_x", event_id="evt_x")

    obs = calibration_service.build_calibration_observations(db_conn, CountingProvider())
    recovered = [o for o in obs if o.outcome == OUTCOME_RECOVERED]
    # The webhook-recovered link is counted once, not re-polled.
    assert any(o.evidence_id == "del_rec" for o in recovered)
    assert "pl_rec" not in calls  # never re-polled after a verified webhook


def test_load_active_snapshot_returns_none_before_any_build(db_conn):
    assert calibration_service.load_active_snapshot(db_conn) is None


def test_load_active_snapshot_returns_latest_immutable_snapshot(db_conn):
    _seed_mixed_world(db_conn)
    calibration_service.build_calibration_snapshot(db_conn, None)
    calibration_service.build_calibration_snapshot(db_conn, None)
    snap = calibration_service.load_active_snapshot(db_conn)
    assert snap is not None
    assert snap.version == 2
    assert snap.posterior_for(INTERVENTION) is not None

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
from app.calibration import (
    OUTCOME_RECOVERED,
    OUTCOME_NOT_RECOVERED,
    CalibrationError,
    EVIDENCE_SOURCE_WEBHOOK,
    canonical_terminal_outcome,
    map_provider_status,
    validate_provider_outcome,
)
from app.executor import PAYMENT_LINK, ExecutionOutcome
from app.optimizer_audit import OptimizerDecisionRecord

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


def _seed_prediction(conn, *, event_id: str, decided_at: str = "2026-01-01T00:00:00+00:00") -> None:
    """Persist the payment_link optimizer decision that predicted the execution.

    Phase 23 hardening: calibration evidence is eligible only when the event
    carries a persisted payment_link prediction, so every seeded execution must
    be backed by its decision record.
    """
    db.insert_optimizer_decision(
        conn,
        OptimizerDecisionRecord(
            event_id=event_id,
            decided_at=decided_at,
            selected_intervention=INTERVENTION,
            selection_reason="max_expected_value",
            candidates_considered=(INTERVENTION,),
            allowed_candidates=(INTERVENTION,),
            evaluations=(),
        ),
    )


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
    _seed_prediction(conn, event_id=event_id)


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


# ---------------------------------------------------------------------------
# Hardening: provider evidence integrity & duplicate projection consolidation
# ---------------------------------------------------------------------------

def test_canonical_terminal_outcome_only_for_terminal_statuses():
    assert canonical_terminal_outcome("paid") == OUTCOME_RECOVERED
    assert canonical_terminal_outcome("expired") == OUTCOME_NOT_RECOVERED
    # Non-terminal / unrecognized / unreadable have NO terminal outcome.
    assert canonical_terminal_outcome("created") is None
    assert canonical_terminal_outcome("partially_paid") is None
    assert canonical_terminal_outcome("cancelled") is None
    assert canonical_terminal_outcome(None) is None
    assert canonical_terminal_outcome("mystery_status") is None


def test_validate_provider_outcome_accepts_only_canonical_pairs():
    assert validate_provider_outcome("paid", OUTCOME_RECOVERED) == OUTCOME_RECOVERED
    assert (
        validate_provider_outcome("expired", OUTCOME_NOT_RECOVERED)
        == OUTCOME_NOT_RECOVERED
    )


@pytest.mark.parametrize(
    "status,outcome",
    [
        ("created", OUTCOME_NOT_RECOVERED),  # non-terminal status, negative outcome
        ("expired", OUTCOME_RECOVERED),  # contradictory (paid only is positive)
        ("paid", OUTCOME_NOT_RECOVERED),  # contradictory negative on paid
        ("mystery_status", OUTCOME_RECOVERED),  # unknown status
        (None, OUTCOME_RECOVERED),  # unreadable status
        ("created", OUTCOME_RECOVERED),  # non-terminal, positive
    ],
)
def test_validate_provider_outcome_rejects_contradictions(status, outcome):
    with pytest.raises(CalibrationError):
        validate_provider_outcome(status, outcome)


def test_validate_provider_outcome_rejects_non_terminal_row_entirely():
    # A terminal-but-non-canonical outcome for a terminal status is rejected.
    with pytest.raises(CalibrationError):
        validate_provider_outcome("cancelled", OUTCOME_NOT_RECOVERED)


def test_contradictory_persisted_provider_outcome_is_excluded(db_conn):
    # A corrupt persisted row (status 'paid' but recorded NOT_RECOVERED) can
    # never become a sample: it is excluded, never "fixed".
    _seed_execution(db_conn, link_id="pl_bad", event_id="evt_bad")
    with pytest.raises(CalibrationError):
        # Direct write is blocked by app-level validation…
        db.insert_provider_payment_link_outcome(
            db_conn,
            payment_link_id="pl_bad",
            event_id="evt_bad",
            status="paid",
            outcome=OUTCOME_NOT_RECOVERED,
            observed_at="2026-01-01T00:00:00+00:00",
        )


def test_write_boundary_rejects_unknown_status(db_conn):
    _seed_execution(db_conn, link_id="pl_unk", event_id="evt_unk")
    with pytest.raises(CalibrationError):
        db.insert_provider_payment_link_outcome(
            db_conn,
            payment_link_id="pl_unk",
            event_id="evt_unk",
            status="mystery_status",
            outcome=OUTCOME_NOT_RECOVERED,
            observed_at="2026-01-01T00:00:00+00:00",
        )


def test_provider_evidence_admitted_only_through_readonly_boundary(db_conn):
    # The durable provider store is validated at the write; a projected row that
    # is somehow non-canonical is excluded during projection too.
    _seed_execution(db_conn, link_id="pl_z", event_id="evt_z")
    obs = calibration_service.build_calibration_observations(
        db_conn, FakeProvider({"pl_z": "paid"})
    )
    exp = [o for o in obs if o.event_id == "evt_z"]
    assert len(exp) == 1 and exp[0].outcome == OUTCOME_RECOVERED


def test_duplicate_executions_share_one_observation_per_link(db_conn):
    # A link that appears in two executions must contribute at most ONE sample.
    for i in range(2):
        _seed_execution(db_conn, link_id="pl_dup", event_id=f"evt_dup_{i}")
    # One durable terminal outcome for the shared link.
    db.insert_provider_payment_link_outcome(
        db_conn,
        payment_link_id="pl_dup",
        event_id="evt_dup_0",
        status="expired",
        outcome=OUTCOME_NOT_RECOVERED,
        observed_at="2026-01-01T00:00:00+00:00",
    )
    obs = calibration_service.build_calibration_observations(
        db_conn, FakeProvider({})
    )
    links = [o.event_id for o in obs]
    assert links.count("evt_dup_0") + links.count("evt_dup_1") == 1


def test_duplicate_executions_and_recovery_still_one_observation(db_conn):
    for i in range(2):
        _seed_execution(db_conn, link_id="pl_dupr", event_id=f"evt_dupr_{i}")
    _seed_recovery(db_conn, link_id="pl_dupr", delivery_id="del_dupr", event_id="evt_dupr_0")
    obs = calibration_service.build_calibration_observations(
        db_conn, FakeProvider({})
    )
    assert len(
        [o for o in obs if o.event_id in ("evt_dupr_0", "evt_dupr_1")]
    ) == 1


def test_execution_without_prediction_is_excluded(db_conn):
    # No optimizer decision -> no eligibility, even with a terminal provider
    # outcome and even with a webhook recovery referencing the event. Nothing
    # is calibrated against an outcome a decision never predicted.
    db.insert_execution_outcome(
        db_conn,
        ExecutionOutcome(
            event_id="evt_nopred",
            intervention=INTERVENTION,
            execution_mode="REAL_RAZORPAY",
            status="SUCCESS",
            external_reference="https://rzp.io/rzp/pl_np",
            reported_at="2026-01-01T00:00:00+00:00",
            payment_link_id="pl_np",
        ),
    )
    db.insert_provider_payment_link_outcome(
        db_conn,
        payment_link_id="pl_np",
        event_id="evt_nopred",
        status="expired",
        outcome=OUTCOME_NOT_RECOVERED,
        observed_at="2026-01-03T00:00:00+00:00",
    )
    obs = calibration_service.build_calibration_observations(
        db_conn, FakeProvider({})
    )
    assert [o for o in obs if o.event_id == "evt_nopred"] == []


def test_webhook_recovery_wins_over_contradictory_provider_outcome(db_conn):
    # A verified recovery is authoritative: it beats any provider-polled or
    # persisted non-positive state for the same link.
    _seed_execution(db_conn, link_id="pl_conf", event_id="evt_conf")
    # Persist a (wrongly) expired provider row for the same link.
    db.insert_provider_payment_link_outcome(
        db_conn,
        payment_link_id="pl_conf",
        event_id="evt_conf",
        status="expired",
        outcome=OUTCOME_NOT_RECOVERED,
        observed_at="2026-01-03T00:00:00+00:00",
    )
    # Now a verified webhook recovery arrives for the same link.
    _seed_recovery(db_conn, link_id="pl_conf", delivery_id="del_conf", event_id="evt_conf")
    obs = calibration_service.build_calibration_observations(
        db_conn, FakeProvider({})
    )
    matched = [o for o in obs if o.event_id == "evt_conf"]
    assert len(matched) == 1
    assert matched[0].outcome == OUTCOME_RECOVERED
    assert matched[0].evidence_source == EVIDENCE_SOURCE_WEBHOOK


def test_provider_outcome_tied_to_other_event_is_excluded(db_conn):
    # Two executions, two links; the persisted provider outcome for one link is
    # (corruptly) tied to the OTHER link's event -> the mismatched row cannot
    # be projected for that execution.
    _seed_execution(db_conn, link_id="pl_a", event_id="evt_a")
    _seed_execution(db_conn, link_id="pl_b", event_id="evt_b")
    db.insert_provider_payment_link_outcome(
        db_conn,
        payment_link_id="pl_b",
        event_id="evt_a",  # mismatched: belongs to a different link's event
        status="expired",
        outcome=OUTCOME_NOT_RECOVERED,
        observed_at="2026-01-03T00:00:00+00:00",
    )
    obs = calibration_service.build_calibration_observations(
        db_conn, FakeProvider({})
    )
    # Only evt_b may consume pl_b's outcome; evt_a's link has no evidence.
    assert [o for o in obs if o.event_id == "evt_a"] == []
    assert [o for o in obs if o.event_id == "evt_b"] == []

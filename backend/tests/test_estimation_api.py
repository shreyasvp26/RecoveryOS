"""Phase 23 API tests for /estimator-evidence (read + recalibrate)."""

from __future__ import annotations

from fastapi.testclient import TestClient

from app.db import (
    connect,
    init_db,
    insert_execution_outcome,
    insert_provider_payment_link_outcome,
    insert_webhook_recovery_outcome,
)
from app.executor import ExecutionOutcome
from app.main import app

client = TestClient(app)


def _seed_gated_world(conn) -> None:
    """Six webhook-recovered + four provider-expired links (gate met)."""
    for i in range(6):
        link = f"pl_r{i}"
        event = f"evt_r{i}"
        insert_execution_outcome(
            conn,
            ExecutionOutcome(
                event_id=event,
                intervention="payment_link",
                execution_mode="REAL_RAZORPAY",
                status="SUCCESS",
                external_reference=f"https://rzp.io/rzp/{link}",
                reported_at="2026-01-01T00:00:00+00:00",
                payment_link_id=link,
            ),
        )
        insert_webhook_recovery_outcome(
            conn,
            delivery_id=f"del_r{i}",
            payment_link_id=link,
            referenced_event_id=event,
            amount_paid_paise=100_00,
            currency="INR",
            payment_id=f"pay_{link}",
            recovered_at="2026-01-02T00:00:00+00:00",
        )
    for i in range(4):
        link = f"pl_x{i}"
        event = f"evt_x{i}"
        insert_execution_outcome(
            conn,
            ExecutionOutcome(
                event_id=event,
                intervention="payment_link",
                execution_mode="REAL_RAZORPAY",
                status="SUCCESS",
                external_reference=f"https://rzp.io/rzp/{link}",
                reported_at="2026-01-01T00:00:00+00:00",
                payment_link_id=link,
            ),
        )
        insert_provider_payment_link_outcome(
            conn,
            payment_link_id=link,
            event_id=event,
            status="expired",
            outcome="NOT_RECOVERED",
            observed_at="2026-01-03T00:00:00+00:00",
        )


def test_estimator_evidence_empty_before_any_snapshot(monkeypatch, tmp_path):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'est.db'}")
    conn = connect(str(tmp_path / "est.db"))
    init_db(conn)
    conn.close()
    resp = client.get("/estimator-evidence")
    assert resp.status_code == 200
    body = resp.json()
    assert body["latest"] is None
    assert body["active_version"] is None
    assert body["snapshot_count"] == 0


def test_recalibrate_builds_versioned_snapshot_and_activates(monkeypatch, tmp_path):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'est.db'}")
    conn = connect(str(tmp_path / "est.db"))
    init_db(conn)
    _seed_gated_world(conn)
    conn.close()

    resp = client.post("/estimator-evidence/recalibrate")
    assert resp.status_code == 200
    snap1 = resp.json()
    assert snap1["version"] == 1
    assert "payment_link" in snap1["active_bps"]
    assert snap1["samples"]["outcome_counts"]["RECOVERED"] == 6
    assert snap1["samples"]["outcome_counts"]["NOT_RECOVERED"] == 4

    resp2 = client.post("/estimator-evidence/recalibrate")
    assert resp2.status_code == 200
    assert resp2.json()["version"] == 2

    resp3 = client.get("/estimator-evidence")
    body = resp3.json()
    assert body["snapshot_count"] == 2
    assert body["active_version"] == 2
    assert body["latest"]["version"] == 2


def test_estimator_evidence_never_reports_active_when_below_gate(monkeypatch, tmp_path):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'est.db'}")
    conn = connect(str(tmp_path / "est.db"))
    init_db(conn)
    # Only 4 expired links, no recovered: gate unmet.
    for i in range(4):
        link = f"pl_g{i}"
        event = f"evt_g{i}"
        insert_execution_outcome(
            conn,
            ExecutionOutcome(
                event_id=event,
                intervention="payment_link",
                execution_mode="REAL_RAZORPAY",
                status="SUCCESS",
                external_reference=f"https://rzp.io/rzp/{link}",
                reported_at="2026-01-01T00:00:00+00:00",
                payment_link_id=link,
            ),
        )
        insert_provider_payment_link_outcome(
            conn,
            payment_link_id=link,
            event_id=event,
            status="expired",
            outcome="NOT_RECOVERED",
            observed_at="2026-01-03T00:00:00+00:00",
        )
    conn.close()

    resp = client.post("/estimator-evidence/recalibrate")
    assert resp.status_code == 200
    assert resp.json()["active_bps"] == {}

    body = client.get("/estimator-evidence").json()
    assert body["active_version"] is None

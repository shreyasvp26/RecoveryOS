"""Calibration snapshot service (Phase 23) — build and load immutable snapshots.

This service is the ONLY place that writes an ``estimator_calibration_snapshots``
row. It projects the REAL_RAZORPAY payment_link evidence (verified webhook
recoveries + durable provider-polled terminal outcomes, optionally reconciled
with a fresh read-only provider poll), computes the per-intervention calibration,
and appends exactly ONE immutable, versioned snapshot.

It is still not an authority: it never executes, never authorizes, and never
imports the executor/policy/optimizer. It writes immutable evidence and snapshot
rows for the decision chain to *read*. It never rewrites a historical snapshot or
a historical decision.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from typing import Any

from . import db
from .adaptive_estimation import CalibrationSnapshot
from .calibration import (
    OUTCOME_RECOVERED,
    calibrate,
    calibration_samples,
    outcome_counts,
)

# The ONLY intervention calibration ever applies to: REAL_RAZORPAY payment_link.
# Kept as a local constant so this module depends on no execution authority.
PAYMENT_LINK_INTERVENTION: str = "payment_link"


def _executed_links(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    """Return the REAL_RAZORPAY payment_link SUCCESS executions, oldest first.

    These are the ONLY executions eligible for calibration: simulated,
    non-payment_link, failed, or link-less executions are excluded structurally.
    """
    rows = conn.execute(
        """
        SELECT event_id, intervention, execution_mode, status, payment_link_id
        FROM execution_outcomes
        WHERE execution_mode = 'REAL_RAZORPAY'
          AND intervention = ?
          AND status = 'SUCCESS'
          AND payment_link_id IS NOT NULL
          AND payment_link_id != ''
        ORDER BY reported_at ASC, event_id ASC
        """,
        (PAYMENT_LINK_INTERVENTION,),
    ).fetchall()
    return [dict(row) for row in rows]


def _reconcile_provider_outcomes(
    conn: sqlite3.Connection,
    executions: list[dict[str, Any]],
    webhook_recoveries: dict[str, dict[str, Any]],
    observed_at: str,
    provider: Any,
) -> dict[str, dict[str, Any]]:
    """Populate the durable provider evidence for links still unsettled.

    A link with a verified webhook recovery is already settled positive and is
    never re-polled. For every other link, a read-only provider poll resolves
    the current status; a TERMINAL result (paid/expired) is persisted once into
    ``provider_payment_link_outcomes`` (idempotent, keyed by link id). Non-
    terminal (created/partially_paid) and unreadable (cancelled/failure) results
    are never persisted: they are not samples. Returns the merged terminal
    provider-outcome mapping for the projection.
    """
    # Include the already-persisted terminal provider outcomes.
    link_ids = [row["payment_link_id"] for row in executions]
    existing = db.get_provider_payment_link_outcomes_for_links(conn, link_ids)

    unresolved = [
        row["payment_link_id"]
        for row in executions
        if row["payment_link_id"] not in webhook_recoveries
        and row["payment_link_id"] not in existing
    ]
    if provider is None:
        return existing

    from .calibration import map_provider_status

    for link_id in unresolved:
        try:
            status = provider.get_payment_link(link_id).status
        except Exception:
            continue
        outcome = map_provider_status(status)
        if outcome not in (OUTCOME_RECOVERED, "NOT_RECOVERED"):
            # PENDING / UNKNOWN are not terminal samples; do not persist.
            continue
        event_id = next(
            (row["event_id"] for row in executions if row["payment_link_id"] == link_id),
            "",
        )
        try:
            db.insert_provider_payment_link_outcome(
                conn,
                payment_link_id=link_id,
                event_id=event_id,
                status=status,
                outcome=outcome,
                observed_at=observed_at,
            )
            existing[link_id] = db.get_provider_payment_link_outcome(conn, link_id)
        except sqlite3.Error:
            continue
    return existing


def build_calibration_observations(
    conn: sqlite3.Connection,
    provider: Any,
    observed_at: str | None = None,
) -> list[Any]:
    """Project the durable calibration evidence into calibration observations.

    Positive outcomes come from the authoritative webhook recoveries; negative
    and poll-discovered outcomes come from the durable provider store (freshly
    reconciled via a read-only poll). Only REAL_RAZORPAY payment_link successes
    are considered. Returns ``CalibrationObservation`` objects.
    """
    from .calibration import (
        EVIDENCE_SOURCE_WEBHOOK,
        EVIDENCE_SOURCE_PROVIDER_POLL,
        TERMINAL_OUTCOMES,
        CalibrationObservation,
    )

    executions = _executed_links(conn)
    link_ids = [row["payment_link_id"] for row in executions]
    webhook_recoveries = db.get_webhook_recovery_outcomes_for_payment_links(
        conn, link_ids
    )
    observed = observed_at or datetime.now(timezone.utc).isoformat()
    provider_outcomes = _reconcile_provider_outcomes(
        conn, executions, webhook_recoveries, observed, provider
    )

    observations: list[CalibrationObservation] = []
    for execution in executions:
        link_id = execution["payment_link_id"]
        event_id = str(execution["event_id"])
        recovery = webhook_recoveries.get(link_id)
        if recovery is not None:
            observations.append(
                CalibrationObservation(
                    event_id=event_id,
                    intervention=PAYMENT_LINK_INTERVENTION,
                    outcome=OUTCOME_RECOVERED,
                    terminal=OUTCOME_RECOVERED in TERMINAL_OUTCOMES,
                    amount_paid_paise=recovery.get("amount_paid_paise"),
                    observed_at=recovery.get("recovered_at"),
                    evidence_id=recovery.get("delivery_id"),
                    evidence_source=EVIDENCE_SOURCE_WEBHOOK,
                )
            )
            continue
        provider_outcome = provider_outcomes.get(link_id)
        if provider_outcome is not None:
            observations.append(
                CalibrationObservation(
                    event_id=event_id,
                    intervention=PAYMENT_LINK_INTERVENTION,
                    outcome=str(provider_outcome["outcome"]),
                    terminal=(provider_outcome["outcome"] in TERMINAL_OUTCOMES),
                    amount_paid_paise=None,
                    observed_at=provider_outcome.get("observed_at"),
                    evidence_id=None,
                    evidence_source=EVIDENCE_SOURCE_PROVIDER_POLL,
                )
            )
    return observations


def build_calibration_snapshot(
    conn: sqlite3.Connection,
    provider: Any,
    observed_at: str | None = None,
) -> dict[str, Any]:
    """Compute, persist, and return the newest immutable calibration snapshot.

    The snapshot is appended as ``version`` = 1 + the highest persisted version.
    It records the per-intervention calibration state for every executable
    intervention; an intervention that meets the calibration gate is active and
    its posterior may feed decisions, anything else keeps the frozen baseline.
    History is never rewritten.
    """
    observed = observed_at or datetime.now(timezone.utc).isoformat()
    observations = build_calibration_observations(conn, provider, observed)
    result = calibrate(observations)

    version = _next_version(conn)
    db.insert_calibration_snapshot(
        conn,
        version=version,
        built_at=observed,
        active_bps_json=_active_bps_json(result),
        evidenced_json=_evidenced_json(result),
    )
    return list_calibration_snapshot(conn, version=version, observed=observations)


def _active_bps_json(result: dict[str, Any]) -> str:
    """JSON of active (gated) intervention posteriors; empty when none active."""
    import json

    return json.dumps(
        {
            intervention: calibration.posterior_bps
            for intervention, calibration in result.items()
            if calibration.active
        }
    )


def _evidenced_json(result: dict[str, Any]) -> str:
    """JSON of the per-intervention evidence summary (every intervention)."""
    import json

    return json.dumps({
        intervention: {
            "baseline_bps": calibration.baseline_bps,
            "observed_total": calibration.observed_total,
            "observed_recovered": calibration.observed_recovered,
            "observed_not_recovered": calibration.observed_not_recovered,
            "prior_successes": calibration.prior_successes,
            "prior_failures": calibration.prior_failures,
        }
        for intervention, calibration in result.items()
    })


def _next_version(conn: sqlite3.Connection) -> int:
    latest = db.get_latest_calibration_snapshot(conn)
    return 1 if latest is None else int(latest["version"]) + 1


def list_calibration_snapshot(
    conn: sqlite3.Connection, *, version: int, observed: list[Any]
) -> dict[str, Any]:
    """Assemble the API payload for one persisted snapshot version."""
    snapshot = db.get_calibration_snapshot(conn, version)
    if snapshot is None:
        raise ValueError(f"snapshot version {version} does not exist")
    return {
        "version": snapshot["version"],
        "built_at": snapshot["built_at"],
        "active_bps": snapshot["active_bps"],
        "evidenced": snapshot["evidenced"],
        "samples": {
            "total": len(calibration_samples(observed)),
            "outcome_counts": outcome_counts(observed),
        },
    }


def load_active_snapshot(conn: sqlite3.Connection) -> CalibrationSnapshot | None:
    """Load the latest persisted snapshot as an immutable CalibrationSnapshot.

    Returns None when no snapshot has ever been built (the decision chain then
    uses the frozen baseline unchanged). Read-only.
    """
    latest = db.get_latest_calibration_snapshot(conn)
    if latest is None:
        return None
    return CalibrationSnapshot(
        version=int(latest["version"]),
        built_at=str(latest["built_at"]),
        active_bps=latest["active_bps"],
        evidenced=latest["evidenced"],
    )


def build_production_estimator(
    conn: sqlite3.Connection,
) -> Any | None:
    """Build the estimator the production decision chain should use, or None.

    Returns a ``CalibratedRecoveryProbabilityEstimator`` when the latest
    snapshot has at least one active (gated) intervention, so production ranks
    with calibrated posteriors; otherwise returns None, meaning the chain keeps
    the frozen baseline estimator unchanged. Read-only.
    """
    snapshot = load_active_snapshot(conn)
    if snapshot is None or not snapshot.active_bps:
        return None
    from .adaptive_estimation import CalibratedRecoveryProbabilityEstimator

    return CalibratedRecoveryProbabilityEstimator(baseline=None, snapshot=snapshot)

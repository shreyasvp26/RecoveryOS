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
    CalibrationError,
    CalibrationObservation,
    EVIDENCE_SOURCE_PROVIDER_POLL,
    EVIDENCE_SOURCE_WEBHOOK,
    TERMINAL_OUTCOMES,
    calibrate,
    calibration_samples,
    canonical_terminal_outcome,
    outcome_counts,
    validate_provider_outcome,
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
    the current status; a TERMINAL canonical result (``paid``/``expired``) is
    validated against the provider contract and only then persisted once into
    ``provider_payment_link_outcomes`` (idempotent, keyed by link id).
    Non-terminal (``created``/``partially_paid``) and unreadable
    (``cancelled``/failure) results are never persisted: they are not samples.
    Returns the merged terminal provider-outcome mapping for the projection.
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

    # Only a provider-polled TERMINAL status may be persisted. A malformed or
    # contradicting provider observation is excluded here, at the write
    # boundary, so corrupt rows never reach the durable store.
    for link_id in unresolved:
        try:
            status = provider.get_payment_link(link_id).status
        except Exception:
            continue
        if not isinstance(status, str):
            continue
        outcome = canonical_terminal_outcome(status)
        if outcome is None:
            # PENDING / UNKNOWN are not terminal samples; do not persist.
            continue
        event_id = next(
            (str(row["event_id"]) for row in executions if row["payment_link_id"] == link_id),
            "",
        )
        if not event_id:
            continue
        try:
            validate_provider_outcome(status, outcome)
        except CalibrationError:
            continue
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


def _events_with_prediction(
    conn: sqlite3.Connection, event_ids: list[str]
) -> set[str]:
    """The events that carry a persisted payment_link prediction.

    Calibration evidence is sampled per REAL_RAZORPAY payment_link execution,
    and such an execution is only meaningfully observable when the decision that
    predicted it is durably recorded. An execution with NO persisted decision
    (e.g. a synthetic/testing row, or a non-economic selection path that left no
    prediction) is missing the prediction that drove it and is excluded rather
    than calibrated against an outcome it never predicted.
    """
    if not event_ids:
        return set()
    decisions = db.get_optimizer_decisions_for_events(
        conn, [str(event_id) for event_id in event_ids]
    )
    return {
        event_id
        for event_id, rows in decisions.items()
        if any(
            str(row.get("selected_intervention")) == PAYMENT_LINK_INTERVENTION
            for row in rows
        )
    }


def build_calibration_observations(
    conn: sqlite3.Connection,
    provider: Any,
    observed_at: str | None = None,
) -> list[CalibrationObservation]:
    """Project the durable calibration evidence into calibration observations.

    THE single authoritative projection path for Phase 23 evidence: execution
    rows (REAL_RAZORPAY payment_link SUCCESS, link present) are intersected
    with the events that carried a persisted payment_link prediction, resolved
    against verified webhook recoveries (authoritative positive) or validated,
    durable provider-polled terminal outcomes, and emitted as at most ONE
    observation PER PAYMENT LINK — never per execution row.

    Every persisted evidence row is validated before it can enter a sample:

      * the execution must be REAL_RAZORPAY, payment_link, SUCCESS, with a link;
      * the event must carry a persisted payment_link prediction;
      * a webhook recovery is authoritative positive ONLY when it is tied to
        that exact execution's event;
      * a provider outcome is admitted ONLY when its status is recognized, its
        outcome is the canonical mapping, and its event matches the execution;
      * a link appears at most once, so duplicates cannot inflate samples;
      * a verified recovery wins over any contradictory provider-polled outcome
        (authoritative chronology: a provider-confirmed payment or the webhook
        that proves it);

    Anything malformed — an unknown status, a contradictory status/outcome
    pair, a linked-to-another-execution row, a simulated/failed/foreign
    execution — is excluded outright. NOT_RECOVERED is never inferred.
    """
    executions = _executed_links(conn)
    if not executions:
        return []

    predicted = _events_with_prediction(
        conn, [str(row["event_id"]) for row in executions]
    )
    eligible = [
        row for row in executions if str(row["event_id"]) in predicted
    ]
    if not eligible:
        return []

    link_ids = [row["payment_link_id"] for row in eligible]
    webhook_recoveries = db.get_webhook_recovery_outcomes_for_payment_links(
        conn, link_ids
    )
    observed = observed_at or datetime.now(timezone.utc).isoformat()
    provider_outcomes = _reconcile_provider_outcomes(
        conn, eligible, webhook_recoveries, observed, provider
    )

    # One observation per Payment Link, never per execution row: the durable
    # evidence is keyed by link, so a link that appears in several executions
    # contributes at most ONE terminal sample to the calibration rate.
    by_link: dict[str, dict[str, Any]] = {}
    for execution in eligible:
        by_link.setdefault(execution["payment_link_id"], execution)

    observations: list[CalibrationObservation] = []
    for link_id, execution in by_link.items():
        event_id = str(execution["event_id"])
        recovery = webhook_recoveries.get(link_id)
        if recovery is not None:
            # A verified webhook recovery is authoritative positive evidence and
            # settles any provider-polled outcome for this link. It is admitted
            # only when it is tied to the eligible execution's event.
            if str(recovery.get("referenced_event_id")) != event_id:
                continue
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
        if provider_outcome is None:
            continue
        # The outcome must belong to THIS execution, not to a different one
        # that happens to share the link id.
        if str(provider_outcome.get("event_id")) != event_id:
            continue
        try:
            outcome = validate_provider_outcome(
                provider_outcome.get("status"), str(provider_outcome["outcome"])
            )
        except CalibrationError:
            # Malformed or contradictory persisted evidence is excluded, never
            # guessed and never turned into a negative sample.
            continue
        observations.append(
            CalibrationObservation(
                event_id=event_id,
                intervention=PAYMENT_LINK_INTERVENTION,
                outcome=outcome,
                terminal=outcome in TERMINAL_OUTCOMES,
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

    The decision chain is never allowed to guess a probability, so every state
    here degrades to the frozen baseline — the ONLY thing that changes is the
    observable provenance recorded on the decision:

    * No snapshot has ever been built -> ``None``: the chain keeps the plain
      frozen baseline and decisions record ``BASELINE / no_calibration_evidence``.
    * A snapshot exists (whether or not any intervention is active) -> the
      calibrated wrapper: active posteriors rank when their gate is met, every
      other intervention ranks on its baseline, and decisions record
      ``CALIBRATED`` or ``BASELINE / threshold_not_met`` accordingly.
    * The latest snapshot is corrupt/unreadable or the read failed -> a wrapper
      flagged unavailable: behaviour is identical to the baseline, but decisions
      record ``BASELINE / calibration_unavailable`` so the fallback is observable.

    This function never raises; a read/parse failure is surfaced through the
    wrapper's provenance rather than as execution failure.
    """
    try:
        snapshot = load_active_snapshot(conn)
    except Exception:
        # Calibration could not be read or parsed. Decision behaviour stays on
        # the baseline; only the recorded reason says it was unavailable.
        from .adaptive_estimation import CalibratedRecoveryProbabilityEstimator

        return CalibratedRecoveryProbabilityEstimator(
            baseline=None, snapshot=None, available=False
        )
    if snapshot is None:
        return None
    from .adaptive_estimation import CalibratedRecoveryProbabilityEstimator

    return CalibratedRecoveryProbabilityEstimator(baseline=None, snapshot=snapshot)

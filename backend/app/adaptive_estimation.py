"""Calibrated adaptive estimator (Phase 23) — composition, not re-authoring.

This module wraps the FROZEN Phase 16 ``RecoveryProbabilityEstimator`` (the
baseline) with a ``CalibrationSnapshot`` so that, when a calibration is active,
an intervention's probability is the calibrated posterior; otherwise the frozen
baseline estimate stands. It preserves the estimator's public contract exactly:
``estimate(event, classification, intervention) -> RecoveryProbability``.

THE SAFETY INVARIANT IS UNCHANGED
---------------------------------
This estimator still only produces a ``RecoveryProbability`` for the optimizer
to rank; it authorizes nothing and executes nothing. The optimizer remains the
decision layer and policy remains the authorization boundary
(``optimizer_decision_set subset-of policy_allowed_candidates``). A snapshot is
immutable and was gated by the calibration module before it could be active, so
the wrapper never invents a probability from uncalibrated evidence.

PROVENANCE
----------
The wrapper records, per intervention, WHICH probability was used and why:
the snapshot version plus the evidence counts that produced it, or
``BASELINE`` when no active calibration applies. This provenance is surfaced on
new decisions and by the estimator-evidence API; historical decisions are never
rewritten.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

from .calibration import STATUS_BASELINE, STATUS_CALIBRATED
from .classification import ClassificationResult
from .economics import PROBABILITY_SCALE, RecoveryProbability
from .estimator import RecoveryProbabilityEstimator
from .models import PaymentEvent

# Decision-level estimator provenance modes persisted on optimizer decisions.
# A decision records WHICH estimator produced it so it stays explainable
# tomorrow even after the active snapshot changes. ``CALIBRATED`` means the
# decision ranked with a gated posterior; ``BASELINE`` means it ranked with the
# frozen baseline. A pre-hardening decision that never recorded provenance is
# reconstructed as ``LEGACY_BASELINE`` (never fabricated as calibrated).
PROVENANCE_CALIBRATED = "CALIBRATED"
PROVENANCE_BASELINE = "BASELINE"
PROVENANCE_LEGACY_BASELINE = "LEGACY_BASELINE"

# Reasons why a decision's estimator is BASELINE. These make the fallback
# observable so the system can tell "no evidence yet" apart from "evidence or
# calibration is unavailable":
#   active_calibration      the chosen intervention used a gated posterior
#   no_calibration_evidence  no snapshot has ever been built
#   threshold_not_met        a snapshot exists but the gate is not met
#   calibration_unavailable  a snapshot was corrupt/unreadable or the read failed
#   legacy_decision          a pre-hardening decision that recorded no provenance
REASON_CALIBRATED_ACTIVE = "active_calibration"
REASON_NO_CALIBRATION = "no_calibration_evidence"
REASON_THRESHOLD_NOT_MET = "threshold_not_met"
REASON_CALIBRATION_UNAVAILABLE = "calibration_unavailable"
REASON_LEGACY = "legacy_decision"


@dataclass(frozen=True)
class CalibrationSnapshot:
    """An immutable, versioned snapshot of intervention calibration state.

    ``version`` counts from 1 and never changes once written. ``active_bps``
    maps an intervention to its ACTIVE calibrated posterior (only interventions
    that met the calibration gate appear here); ``evidenced`` maps every
    intervention to its calibration evidence summary (so the API and provenance
    can always explain the current state). ``built_at`` is the snapshot build
    timestamp.
    """

    version: int
    built_at: str
    active_bps: Mapping[str, int]
    evidenced: Mapping[str, Mapping[str, Any]]

    def __post_init__(self) -> None:
        if isinstance(self.version, bool) or not isinstance(self.version, int) or self.version < 1:
            raise ValueError("version must be a positive integer")
        _require_nonempty(self.built_at, "built_at")
        if not isinstance(self.active_bps, Mapping):
            raise ValueError("active_bps must be a mapping")
        for intervention, bps in self.active_bps.items():
            if not isinstance(intervention, str) or not intervention:
                raise ValueError("active_bps keys must be non-empty intervention names")
            if (
                isinstance(bps, bool)
                or not isinstance(bps, int)
                or not (0 <= bps <= PROBABILITY_SCALE)
            ):
                raise ValueError(
                    f"active posterior for {intervention!r} must be within "
                    f"[0, {PROBABILITY_SCALE}]"
                )
        if not isinstance(self.evidenced, Mapping):
            raise ValueError("evidenced must be a mapping")

    def posterior_for(self, intervention: str) -> int | None:
        """The active calibrated posterior for an intervention, or None."""
        return self.active_bps.get(intervention)


def _require_nonempty(value: Any, name: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a non-empty string")


class CalibratedRecoveryProbabilityEstimator:
    """Composes the frozen baseline with an optional active calibration.

    When ``snapshot`` is None (no calibration ever activated) the wrapper is
    behaviourally identical to the frozen baseline. When a snapshot is active,
    interventions with a gated posterior use it; all others fall back to the
    baseline. Estimate remains a pure function of
    (event, classification, intervention) plus the immutable snapshot.
    """

    def __init__(
        self,
        baseline: RecoveryProbabilityEstimator | None = None,
        snapshot: CalibrationSnapshot | None = None,
        available: bool = True,
    ) -> None:
        self._baseline = baseline or RecoveryProbabilityEstimator()
        self._snapshot = snapshot
        # ``available`` distinguishes "no active calibration" (a normal state,
        # reason threshold_not_met / no_calibration_evidence) from "calibration
        # is unavailable" (a corrupt snapshot or a failed read, reason
        # calibration_unavailable). Both fall back to baseline as safely as
        # ever; only the observable reason differs.
        self._available = available

    @property
    def snapshot(self) -> CalibrationSnapshot | None:
        return self._snapshot

    @property
    def available(self) -> bool:
        return self._available

    def estimate(
        self,
        event: PaymentEvent,
        classification: ClassificationResult,
        intervention: str,
    ) -> RecoveryProbability:
        """Return the calibrated probability, or the frozen baseline estimate."""
        posterior = (
            self._snapshot.posterior_for(intervention)
            if self._snapshot is not None
            else None
        )
        if posterior is not None:
            return RecoveryProbability(basis_points=posterior)
        return self._baseline.estimate(event, classification, intervention)

    def provenance(self, intervention: str) -> dict[str, Any]:
        """Explain which probability source produced an estimate (read-only).

        Returns the snapshot version and the evidence behind an active
        posterior, or a baseline marker when no calibration applies. This is
        display/diagnostic data only and never rewrites any decision.
        """
        if self._snapshot is not None:
            posterior = self._snapshot.posterior_for(intervention)
            if posterior is not None:
                row = self._snapshot.evidenced.get(intervention, {})
                return {
                    "status": STATUS_CALIBRATED,
                    "version": self._snapshot.version,
                    "posterior_bps": posterior,
                    "observed_total": row.get("observed_total"),
                    "observed_recovered": row.get("observed_recovered"),
                    "observed_not_recovered": row.get("observed_not_recovered"),
                    "baseline_bps": row.get("baseline_bps"),
                    "reason": REASON_CALIBRATED_ACTIVE,
                }
        return {
            "status": STATUS_BASELINE,
            "version": self._snapshot.version if self._snapshot is not None else None,
            "posterior_bps": None,
            "reason": self._baseline_reason(),
        }

    def _baseline_reason(self) -> str:
        """Why this estimate is BASELINE: unavailable, no snapshot, or gate unmet."""
        if not self._available:
            return REASON_CALIBRATION_UNAVAILABLE
        if self._snapshot is None:
            return REASON_NO_CALIBRATION
        return REASON_THRESHOLD_NOT_MET

    def decision_provenance(self, intervention: str) -> dict[str, Any]:
        """The decision-level estimator provenance (persisted on new decisions).

        This is the state captured AT DECISION TIME and recorded immutably with
        the optimizer decision, so a decision made under snapshot v1 continues
        to identify v1 after v2 becomes active. It is read-only and never
        rewrites a decision or a snapshot. Returns:

            {
              "estimator_mode":   CALIBRATED | BASELINE,
              "estimator_version": int when calibrated (or baseline under a
                                    specific snapshot), else None,
              "estimator_reason": see the REASON_ constants,
            }

        ``intervention`` lets the decision report the state that produced the
        chosen candidate's probability; all candidates of one decision share the
        same immutable snapshot version.
        """
        if self._snapshot is not None and self._snapshot.posterior_for(intervention) is not None:
            return {
                "estimator_mode": PROVENANCE_CALIBRATED,
                "estimator_version": self._snapshot.version,
                "estimator_reason": REASON_CALIBRATED_ACTIVE,
            }
        return {
            "estimator_mode": PROVENANCE_BASELINE,
            "estimator_version": self._snapshot.version if self._snapshot is not None else None,
            "estimator_reason": self._baseline_reason(),
        }

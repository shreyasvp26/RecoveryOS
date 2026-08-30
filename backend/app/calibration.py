"""Evidence-calibrated recovery estimation (Phase 23) — calibration, not authority.

Phase 23 evolves the frozen Phase 16 decision chain from a static additive
score into evidence-calibrated economics. This module owns ONLY the
calibration layer:

    baseline estimator (frozen)  ->  calibration evidence  ->  versioned snapshot
                                                                  -> calibrated probability

WHAT THIS MODULE IS NOT
-----------------------
It is not an authority. It executes nothing, authorizes nothing, and changes no
policy decision. It never calls the LLM. It never imports the executor, the
webhook boundary, the policy engine, or the optimizer. Its output is a
probability the (frozen) optimizer may choose to rank with — the optimizer
remains a decision layer, and policy remains the authorization boundary.

EVIDENCE-GATING
---------------
Calibration is intervention-level only and is fed EXCLUSIVELY by real,
operator-side provider evidence on REAL_RAZORPAY ``payment_link`` executions:

    paid    -> RECOVERED      (authoritative: verified webhook, or poll status)
    expired -> NOT_RECOVERED  (authoritative: provider status, polled)
    created / partially_paid -> PENDING (not terminal; never a sample)
    cancelled / provider failure / timeout / failed execution -> UNKNOWN (never negative)

SIMULATED executions, the benchmark, the Policy Lab, replay and the hidden
ground-truth world are structurally ineligible and can never reach a
calibration sample. NOT_RECOVERED is never inferred from absence of payment:
only a provider-confirmed ``expired`` status settles a link as NOT_RECOVERED.

IMMUTABLE, VERSIONED SNAPSHOTS
------------------------------
Each snapshot build computes a deterministic per-intervention posterior from
the terminal evidence available at build time and appends ONE immutable,
versioned row (v1, v2, ...). Historical snapshots, and the historical decisions
they preceded, are never rewritten. A snapshot is only ACTIVE (i.e. allowed to
change the probabilities that feed decisions) when an intervention meets every
calibration threshold with its OWN evidence:

    total observations  >= MIN_TOTAL_OBSERVATIONS (10)
    recovered           >= MIN_POSITIVE            (1)
    not_recovered       >= MIN_NEGATIVE            (1)

An intervention that does not meet the gate keeps its frozen baseline
probability. Samples are never borrowed across interventions.

ARITHMETIC
----------
Probabilities stay in integer basis points on [0, PROBABILITY_SCALE]. The
posterior is a Beta-binomial update against a baseline-derived prior of strength
PRIOR_STRENGTH, computed entirely with integer arithmetic (floor division),
matching the repo's monetary/economic rounding policy — never floating point.

    prior_successes_i = floor(baseline_bps_i * PRIOR_STRENGTH / PROBABILITY_SCALE)
    prior_failures_i  = PRIOR_STRENGTH - prior_successes_i
    posterior_bps_i   = (recovered_i + prior_successes_i) * PROBABILITY_SCALE
                        // (total_i + PRIOR_STRENGTH)
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Mapping, Sequence

from .economics import PROBABILITY_SCALE
from .estimator import BASE_RECOVERY_BPS

# Calibration thresholds, per intervention, over that intervention's OWN
# terminal evidence. A snapshot is active only when every gate is met.
MIN_TOTAL_OBSERVATIONS: int = 10
MIN_POSITIVE: int = 1
MIN_NEGATIVE: int = 1

# The strength of the baseline-derived prior (an effective prior sample size).
PRIOR_STRENGTH: int = 20

# Calibration outcomes. PENDING and UNKNOWN are non-terminal: they never enter
# a sample. UNKNOWN is explicitly never negative.
OUTCOME_RECOVERED = "RECOVERED"
OUTCOME_NOT_RECOVERED = "NOT_RECOVERED"
OUTCOME_PENDING = "PENDING"
OUTCOME_UNKNOWN = "UNKNOWN"

CALIBRATION_OUTCOMES: tuple[str, ...] = (
    OUTCOME_RECOVERED,
    OUTCOME_NOT_RECOVERED,
    OUTCOME_PENDING,
    OUTCOME_UNKNOWN,
)

TERMINAL_OUTCOMES: frozenset[str] = frozenset(
    {OUTCOME_RECOVERED, OUTCOME_NOT_RECOVERED}
)

# Evidence sources that may enter a calibration sample.
EVIDENCE_SOURCE_WEBHOOK = "webhook"
EVIDENCE_SOURCE_PROVIDER_POLL = "provider_poll"

# The single honest state for "we cannot say".
STATUS_BASELINE = "BASELINE"
STATUS_CALIBRATED = "CALIBRATED"

# The authoritative Razorpay Payment Link status domain
# (GET /v1/payment_links/:id). No other status exists; an unrecognized status
# is UNKNOWN and is never calibration evidence.
PROVIDER_STATUS_PAID = "paid"
PROVIDER_STATUS_PARTIALLY_PAID = "partially_paid"
PROVIDER_STATUS_EXPIRED = "expired"
PROVIDER_STATUS_CANCELLED = "cancelled"
PROVIDER_STATUS_CREATED = "created"
PROVIDER_STATUSES: frozenset[str] = frozenset(
    {
        PROVIDER_STATUS_PAID,
        PROVIDER_STATUS_PARTIALLY_PAID,
        PROVIDER_STATUS_EXPIRED,
        PROVIDER_STATUS_CANCELLED,
        PROVIDER_STATUS_CREATED,
    }
)


class CalibrationError(Exception):
    """The calibration layer received state it cannot turn into a safe value."""


def _require_identifier(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise CalibrationError(f"{name} must be a non-empty string, got {value!r}")
    return value


@dataclass(frozen=True)
class CalibrationObservation:
    """One executed REAL_RAZORPAY payment_link and its observed outcome.

    ``terminal`` is True only for RECOVERED / NOT_RECOVERED; only terminal
    observations may enter a calibration sample. ``evidence_source`` names
    whether the outcome came from a verified webhook recovery or a provider
    poll. Everything is copied verbatim from persisted/provider state; nothing
    is inferred.
    """

    event_id: str
    intervention: str
    outcome: str
    terminal: bool
    amount_paid_paise: int | None
    observed_at: str | None
    evidence_id: str | None
    evidence_source: str

    def __post_init__(self) -> None:
        _require_identifier(self.event_id, "event_id")
        _require_identifier(self.intervention, "intervention")
        if self.outcome not in CALIBRATION_OUTCOMES:
            raise CalibrationError(
                f"outcome must be one of {sorted(CALIBRATION_OUTCOMES)}, "
                f"got {self.outcome!r}"
            )
        terminal = self.outcome in TERMINAL_OUTCOMES
        object.__setattr__(self, "terminal", terminal)
        if self.amount_paid_paise is not None and (
            not isinstance(self.amount_paid_paise, int)
            or isinstance(self.amount_paid_paise, bool)
            or self.amount_paid_paise < 0
        ):
            raise CalibrationError("amount_paid_paise must be None or a non-negative int")
        if self.evidence_source not in (
            EVIDENCE_SOURCE_WEBHOOK,
            EVIDENCE_SOURCE_PROVIDER_POLL,
        ):
            raise CalibrationError(
                f"evidence_source must be one of "
                f"{EVIDENCE_SOURCE_WEBHOOK!r}, {EVIDENCE_SOURCE_PROVIDER_POLL!r}"
            )

    def to_dict(self) -> dict[str, Any]:
        return {
            "event_id": self.event_id,
            "intervention": self.intervention,
            "outcome": self.outcome,
            "terminal": self.terminal,
            "amount_paid_paise": self.amount_paid_paise,
            "observed_at": self.observed_at,
            "evidence_id": self.evidence_id,
            "evidence_source": self.evidence_source,
        }


# ---------------------------------------------------------------------------
# Terminal contract: provider -> calibration outcome.
# ---------------------------------------------------------------------------


def map_provider_status(status: str | None) -> str:
    """Map a provider-observed Payment Link status to a calibration outcome.

    This is the single authoritative mapping of the Phase 23 terminal contract.
    ``cancelled`` and an unrecognized/unreadable status map to UNKNOWN, never to
    a negative outcome: only a provider-confirmed ``expired`` settles a link as
    NOT_RECOVERED. ``paid`` is RECOVERED; ``created``/``partially_paid`` are
    PENDING (still open, not a sample).
    """
    if status == "paid":
        return OUTCOME_RECOVERED
    if status == "expired":
        return OUTCOME_NOT_RECOVERED
    if status in ("created", "partially_paid"):
        return OUTCOME_PENDING
    return OUTCOME_UNKNOWN


def canonical_terminal_outcome(status: str | None) -> str | None:
    """The ONLY canonical TERMINAL calibration outcome a provider status has.

    Returns ``RECOVERED`` only for ``paid`` and ``NOT_RECOVERED`` only for
    ``expired``; every other status (``created``, ``partially_paid``,
    ``cancelled``, unrecognized, unreadable) has no terminal outcome and
    returns None — those links are PENDING/UNKNOWN, never a sample, and no
    outcome is ever inferred for them.
    """
    outcome = map_provider_status(status)
    return outcome if outcome in TERMINAL_OUTCOMES else None


def validate_provider_outcome(status: str | None, outcome: str) -> str:
    """Enforce canonical status<->outcome consistency before calibration.

    ``outcome`` is accepted VERBATIM only when it is the single canonical
    terminal outcome for ``status``. Every malformed or contradictory pairing —
    an unrecognized status, a non-terminal status, or a terminal outcome that
    does not match its status (e.g. status ``created`` with outcome
    ``NOT_RECOVERED``, or status ``expired`` with outcome ``RECOVERED``) — is
    rejected with ``CalibrationError`` so a corrupt or contradictory persisted
    row can never become calibration evidence. The safe evidence set is the
    rows that survive this check; nothing is guessed and nothing is "fixed".
    """
    if not isinstance(status, str) or status not in PROVIDER_STATUSES:
        raise CalibrationError(
            f"unknown provider status {status!r}; the authoritative domain is "
            f"{sorted(PROVIDER_STATUSES)}"
        )
    canonical = canonical_terminal_outcome(status)
    if canonical is None:
        raise CalibrationError(
            f"status {status!r} has no terminal calibration outcome; it is "
            "PENDING/UNKNOWN and can never be a sample"
        )
    if outcome != canonical:
        raise CalibrationError(
            f"status {status!r} canonically maps to {canonical!r}; "
            f"recorded outcome {outcome!r} contradicts the provider contract"
        )
    return outcome


# ---------------------------------------------------------------------------
# Prior derivation (baseline-derived, integer-exact).
# ---------------------------------------------------------------------------


def prior_successes(baseline_bps: int) -> int:
    """The pseudo-success prior count for an intervention baseline.

    ``baseline_bps`` is the frozen estimator's modelled base recovery for the
    intervention. The prior splits PRIOR_STRENGTH into pseudo success/failure
    counts proportional to that baseline, using exact integer floor division.
    """
    if not isinstance(baseline_bps, int) or isinstance(baseline_bps, bool):
        raise CalibrationError("baseline_bps must be an integer")
    if not (0 <= baseline_bps <= PROBABILITY_SCALE):
        raise CalibrationError("baseline_bps must satisfy 0 <= b <= 10000")
    return baseline_bps * PRIOR_STRENGTH // PROBABILITY_SCALE


def prior_failures(baseline_bps: int) -> int:
    """The pseudo-failure prior count for an intervention baseline."""
    return PRIOR_STRENGTH - prior_successes(baseline_bps)


def posterior_bps(recovered: int, total: int, baseline_bps: int) -> int:
    """Deterministic Beta-binomial posterior in integer bps (floor division).

        posterior_bps = (recovered + prior_successes) * 10000
                        // (total + PRIOR_STRENGTH)

    ``total`` is the count of TERMINAL observations (recovered + not_recovered)
    for the intervention. The result is floored, bounded to [0, PROBABILITY_SCALE]
    and independent of input order.
    """
    if isinstance(recovered, bool) or not isinstance(recovered, int) or recovered < 0:
        raise CalibrationError("recovered must be a non-negative integer")
    if isinstance(total, bool) or not isinstance(total, int) or total < 0:
        raise CalibrationError("total must be a non-negative integer")
    if recovered > total:
        raise CalibrationError("recovered cannot exceed total")
    prior_s = prior_successes(baseline_bps)
    value = (recovered + prior_s) * PROBABILITY_SCALE // (total + PRIOR_STRENGTH)
    return max(0, min(PROBABILITY_SCALE, value))


def calibration_samples(
    observations: Sequence[CalibrationObservation],
) -> list[CalibrationObservation]:
    """Filter to terminal observations (the only population a rate uses)."""
    return [observation for observation in observations if observation.terminal]


def outcome_counts(
    observations: Sequence[CalibrationObservation],
) -> dict[str, int]:
    """Count observations per outcome, every outcome key present."""
    counts = {outcome: 0 for outcome in CALIBRATION_OUTCOMES}
    for observation in observations:
        counts[observation.outcome] = counts.get(observation.outcome, 0) + 1
    return counts


# ---------------------------------------------------------------------------
# Per-intervention calibration summary.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class InterventionCalibration:
    """The resulting state for ONE intervention: gated posterior or baseline.

    ``active`` is True only when this intervention's OWN evidence meets every
    threshold. When inactive, ``posterior_bps`` equals ``baseline_bps`` and the
    intervention continues to be ranked by the frozen baseline.
    """

    intervention: str
    baseline_bps: int
    observed_total: int
    observed_recovered: int
    observed_not_recovered: int
    prior_successes: int
    prior_failures: int
    posterior_bps: int
    active: bool
    status: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "intervention": self.intervention,
            "baseline_bps": self.baseline_bps,
            "observed_total": self.observed_total,
            "observed_recovered": self.observed_recovered,
            "observed_not_recovered": self.observed_not_recovered,
            "prior_successes": self.prior_successes,
            "prior_failures": self.prior_failures,
            "posterior_bps": self.posterior_bps,
            "active": self.active,
            "status": self.status,
        }


def calibrate_intervention(
    intervention: str,
    observations: Sequence[CalibrationObservation],
) -> InterventionCalibration:
    """Compute the calibration state for one intervention from its own samples.

    An intervention's samples are never borrowed from another. If the gate is
    not met, the intervention stays on its frozen baseline probability.
    """
    _require_identifier(intervention, "intervention")
    baseline = BASE_RECOVERY_BPS[intervention]
    terminal = [
        observation
        for observation in observations
        if observation.intervention == intervention and observation.terminal
    ]
    recovered = sum(
        1 for observation in terminal if observation.outcome == OUTCOME_RECOVERED
    )
    not_recovered = sum(
        1
        for observation in terminal
        if observation.outcome == OUTCOME_NOT_RECOVERED
    )
    total = recovered + not_recovered

    meets_gate = (
        total >= MIN_TOTAL_OBSERVATIONS
        and recovered >= MIN_POSITIVE
        and not_recovered >= MIN_NEGATIVE
    )
    posterior = posterior_bps(recovered, total, baseline) if meets_gate else baseline
    return InterventionCalibration(
        intervention=intervention,
        baseline_bps=baseline,
        observed_total=total,
        observed_recovered=recovered,
        observed_not_recovered=not_recovered,
        prior_successes=prior_successes(baseline),
        prior_failures=prior_failures(baseline),
        posterior_bps=posterior,
        active=meets_gate,
        status=STATUS_CALIBRATED if meets_gate else STATUS_BASELINE,
    )


def calibrate(
    observations: Sequence[CalibrationObservation],
) -> dict[str, InterventionCalibration]:
    """Calibrate every executable intervention from the given evidence.

    Every intervention in the frozen taxonomy gets a row so a consumer can
    always see its calibration status, whether active or baseline.
    """
    result: dict[str, InterventionCalibration] = {}
    for intervention in BASE_RECOVERY_BPS:
        result[intervention] = calibrate_intervention(intervention, observations)
    return result


def to_dict_mapping(
    calibrations: Mapping[str, InterventionCalibration],
) -> dict[str, dict[str, Any]]:
    """Serialize a calibration mapping for snapshot/API output."""
    return {
        intervention: calibration.to_dict()
        for intervention, calibration in calibrations.items()
    }

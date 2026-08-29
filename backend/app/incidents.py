"""Phase 20 deterministic incident-level revenue degradation detection.

THE QUESTION THIS ANSWERS
-------------------------
RecoveryOS already decides what to do about ONE failed payment. This module
answers the system-level question that sits above that loop:

    where is recovery performance itself getting worse, by how much, and
    which payments does that cover?

WHAT THIS IS
------------
An ANALYTICAL layer over evidence RecoveryOS already produced. It reads
existing ``PaymentEvent`` records and the evaluated (SIMULATED) outcomes the
existing evaluation machinery already computed for them, partitions them into
two equal-width time windows, aggregates them by segment, and compares the two
windows. That is the whole methodology.

WHAT THIS IS NOT
----------------
Not a monitor, not a forecaster, not an anomaly model. There is no EWMA, no
z-score, no learned baseline and no streaming infrastructure: the baseline is
simply the immediately preceding window of the same width. Nothing here
classifies, authorizes, selects, executes, or writes anything. The detector is
a pure function of (events, evaluated outcomes, configuration), so the same
dataset always yields the same incidents, with the same ids, in the same order.

GROUND TRUTH STAYS OUT
----------------------
Detection reads OBSERVED evaluated outcomes — whether a simulated recovery
happened and for how much — and never the hidden world's probabilities,
expected values or oracle options. ``EvaluatedOutcome`` is deliberately the
narrowest possible carrier of that evidence: an event id, a boolean, and an
integer amount. There is no field on it for a probability to travel in.

EVERY FIGURE IS SIMULATED OR MODELLED
-------------------------------------
Recovery evidence comes from the controlled synthetic evaluation, so recovery
rates here are simulated evaluation results. ``simulated_revenue_at_risk_paise``
is one step further removed: it is a MODELLED estimate obtained by applying the
observed recovery-rate gap to the current window's payment value. It is not
merchant loss, not production revenue, and not confirmed recoverable money.

MONEY
-----
Integer paise throughout, and rates as integer basis points, so no financial
figure is ever produced by float arithmetic. Rupee formatting happens only at
the presentation boundary.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Iterable, Mapping, Sequence

from .models import PaymentEvent
from .policy import parse_aware_datetime

# Bumping this identifies a deliberate change to HOW an incident is detected —
# the windows, the gate, the threshold, the severity rules, the impact model.
# It participates in every incident id, so a methodology change cannot silently
# reuse an old identity.
INCIDENT_METHODOLOGY_VERSION = "phase20-preceding-window-comparison-v1"

# Incident figures are analytical readings of simulated evaluation evidence.
# Stamped on every incident so the label travels with the number.
INCIDENT_RESULT_MODE = "SIMULATED"

# --- windows ---------------------------------------------------------------
#
# The current window is the last ``WINDOW_DAYS`` days of OBSERVED data and the
# baseline is the equally wide window immediately before it. Anchoring on the
# latest observed event timestamp rather than on the wall clock is what keeps
# the detector reproducible: ``datetime.now()`` would make today's incidents
# undetectable tomorrow.
WINDOW_DAYS = 28

# --- eligibility -----------------------------------------------------------
#
# A segment must have produced at least this many evaluated outcomes in EACH
# window before it can be compared at all. Five is the smallest sample at which
# a recovery rate is a rate rather than an anecdote, and it is a gate, never a
# score: passing it earns a comparison, not severity.
MIN_CURRENT_OBSERVATIONS = 5
MIN_BASELINE_OBSERVATIONS = 5

# --- detection threshold ---------------------------------------------------
#
# Rates are integer basis points (10,000 bps = 100%), so a percentage-point is
# 100 bps and the 15pp detection threshold is 1500 bps. An incident requires
# the recovery rate to have fallen by at least this much.
BPS = 10_000
PERCENTAGE_POINT_BPS = 100
DEGRADATION_THRESHOLD_BPS = 15 * PERCENTAGE_POINT_BPS

# --- severity --------------------------------------------------------------
SEVERITY_LOW = "LOW"
SEVERITY_MEDIUM = "MEDIUM"
SEVERITY_HIGH = "HIGH"
SEVERITY_CRITICAL = "CRITICAL"

# Ordered least to most severe; the promotion logic walks this list.
SEVERITY_ORDER: tuple[str, ...] = (
    SEVERITY_LOW,
    SEVERITY_MEDIUM,
    SEVERITY_HIGH,
    SEVERITY_CRITICAL,
)

# Degradation each severity requires, in bps. A level is REACHED by deviation.
SEVERITY_DEGRADATION_BPS: Mapping[str, int] = {
    SEVERITY_LOW: 15 * PERCENTAGE_POINT_BPS,
    SEVERITY_MEDIUM: 20 * PERCENTAGE_POINT_BPS,
    SEVERITY_HIGH: 30 * PERCENTAGE_POINT_BPS,
    SEVERITY_CRITICAL: 40 * PERCENTAGE_POINT_BPS,
}

# Impact each severity above LOW requires. A level is EARNED by impact: a huge
# percentage move over a handful of low-value payments is a real observation
# but not a critical revenue event, so deviation alone can never promote.
# Amounts are integer paise (₹10,000 = 1,000,000 paise).
SEVERITY_MIN_AFFECTED_EVENTS: Mapping[str, int] = {
    SEVERITY_MEDIUM: 10,
    SEVERITY_HIGH: 25,
    SEVERITY_CRITICAL: 50,
}
SEVERITY_MIN_REVENUE_AT_RISK_PAISE: Mapping[str, int] = {
    SEVERITY_MEDIUM: 10_000 * 100,
    SEVERITY_HIGH: 50_000 * 100,
    SEVERITY_CRITICAL: 100_000 * 100,
}

# --- status ----------------------------------------------------------------
#
# Incidents are DERIVED, never stored, so the only status a detector can honestly
# report is OPEN: an incident exists for exactly as long as the dataset still
# satisfies the detection rule, and stops being returned when it does not.
# RESOLVED is named here because the vocabulary needs the other half, and it is
# what a disappeared incident means; RecoveryOS deliberately does not implement
# acknowledgement, assignment or escalation.
STATUS_OPEN = "OPEN"
STATUS_RESOLVED = "RESOLVED"

# --- segmentation ----------------------------------------------------------
DIMENSION_BANK = "bank"
DIMENSION_PAYMENT_METHOD = "payment_method"
DIMENSION_FAILURE_REASON = "failure_reason"

# The segmentations compared, in canonical order. Composite bank+method is
# included because a degradation frequently lives in one rail at one bank and
# is diluted by either dimension alone. Nothing more general exists: this is a
# fixed tuple, not a multidimensional analytics framework.
SEGMENTATIONS: tuple[tuple[str, ...], ...] = (
    (DIMENSION_BANK,),
    (DIMENSION_PAYMENT_METHOD,),
    (DIMENSION_FAILURE_REASON,),
    (DIMENSION_BANK, DIMENSION_PAYMENT_METHOD),
)

# How many failure reasons the incident carries as evidence.
TOP_FAILURE_REASONS = 5


class IncidentError(Exception):
    """Incident detection cannot proceed honestly."""


# ---------------------------------------------------------------------------
# Inputs
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class EvaluatedOutcome:
    """One event's OBSERVED evaluated outcome. Carries no ground truth.

    This is the entire evidence contract between the existing evaluation
    machinery and the detector. There is intentionally no probability, no
    expected value and no oracle field, so hidden-world internals cannot reach
    detection even by accident.
    """

    event_id: str
    recovered: bool
    recovered_amount_paise: int

    def __post_init__(self) -> None:
        if not isinstance(self.event_id, str) or not self.event_id.strip():
            raise IncidentError("event_id must be a non-empty string")
        if type(self.recovered) is not bool:
            raise IncidentError("recovered must be a boolean")
        if (
            type(self.recovered_amount_paise) is not int
            or self.recovered_amount_paise < 0
        ):
            raise IncidentError(
                "recovered_amount_paise must be a non-negative integer (paise)"
            )
        if self.recovered and self.recovered_amount_paise <= 0:
            raise IncidentError(
                "a recovered outcome must carry a positive recovered amount"
            )
        if not self.recovered and self.recovered_amount_paise != 0:
            raise IncidentError(
                "a non-recovered outcome must carry a zero recovered amount"
            )


@dataclass(frozen=True)
class DetectionConfig:
    """Every knob that can change which incidents exist.

    Configurable, but never silently: the defaults are the documented Phase 20
    methodology, the values are validated, and the whole configuration is
    digested into every incident id, so a run under a different configuration
    can never be mistaken for a run under this one.
    """

    window_days: int = WINDOW_DAYS
    min_current_observations: int = MIN_CURRENT_OBSERVATIONS
    min_baseline_observations: int = MIN_BASELINE_OBSERVATIONS
    degradation_threshold_bps: int = DEGRADATION_THRESHOLD_BPS
    methodology: str = INCIDENT_METHODOLOGY_VERSION

    def __post_init__(self) -> None:
        for name in (
            "window_days",
            "min_current_observations",
            "min_baseline_observations",
            "degradation_threshold_bps",
        ):
            value = getattr(self, name)
            if type(value) is not int or value < 1:
                raise IncidentError(f"{name} must be a positive integer")
        if self.degradation_threshold_bps > BPS:
            raise IncidentError(
                "degradation_threshold_bps cannot exceed 10000 (100 percentage "
                "points)"
            )
        if not isinstance(self.methodology, str) or not self.methodology.strip():
            raise IncidentError("methodology must be a non-empty string")

    def to_dict(self) -> dict[str, Any]:
        """Serialize the configuration that produced a set of incidents."""
        return {
            "methodology": self.methodology,
            "window_days": self.window_days,
            "min_current_observations": self.min_current_observations,
            "min_baseline_observations": self.min_baseline_observations,
            "degradation_threshold_bps": self.degradation_threshold_bps,
        }


# ---------------------------------------------------------------------------
# Windows
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ObservationWindows:
    """The two equal-width windows every comparison is made across.

    BOUNDARY SEMANTICS, stated once and applied everywhere: each window is
    half-open, EXCLUSIVE at its start and INCLUSIVE at its end.

        baseline = (anchor - 2w, anchor - w]
        current  = (anchor - w,  anchor]

    So an event exactly on ``anchor - w`` belongs to the BASELINE, an event
    exactly on ``anchor`` belongs to the CURRENT window, and an event on or
    before ``anchor - 2w`` belongs to neither and is ignored. The two windows
    therefore partition their span with no event counted twice and none lost at
    the seam.

    ``anchor`` is the latest OBSERVED event timestamp, not the wall clock.
    """

    anchor: datetime
    window_days: int

    @property
    def width(self) -> timedelta:
        return timedelta(days=self.window_days)

    @property
    def current_start(self) -> datetime:
        return self.anchor - self.width

    @property
    def current_end(self) -> datetime:
        return self.anchor

    @property
    def baseline_start(self) -> datetime:
        return self.anchor - 2 * self.width

    @property
    def baseline_end(self) -> datetime:
        return self.anchor - self.width

    def contains_current(self, moment: datetime) -> bool:
        """Is ``moment`` in the current window? (start exclusive, end inclusive)"""
        return self.current_start < moment <= self.current_end

    def contains_baseline(self, moment: datetime) -> bool:
        """Is ``moment`` in the baseline window? (start exclusive, end inclusive)"""
        return self.baseline_start < moment <= self.baseline_end

    def to_dict(self) -> dict[str, Any]:
        """Serialize both windows with explicit, inspectable bounds."""
        return {
            "window_days": self.window_days,
            "boundary_semantics": "start exclusive, end inclusive",
            "anchor": self.anchor.isoformat(),
            "anchor_source": "latest observed event timestamp",
            "current": {
                "start": self.current_start.isoformat(),
                "end": self.current_end.isoformat(),
            },
            "baseline": {
                "start": self.baseline_start.isoformat(),
                "end": self.baseline_end.isoformat(),
            },
        }


def observation_windows(
    events: Sequence[PaymentEvent], config: DetectionConfig | None = None
) -> ObservationWindows:
    """Derive both windows from the data itself.

    The anchor is the latest event timestamp in the dataset, so the windows are
    a property of the observations and not of when someone asked. Malformed or
    naive timestamps are refused rather than coerced.
    """
    config = config or DetectionConfig()
    if not events:
        raise IncidentError("at least one event is required to derive a window")
    return ObservationWindows(
        anchor=max(_event_time(event) for event in events),
        window_days=config.window_days,
    )


def _event_time(event: PaymentEvent) -> datetime:
    """The event's timestamp as an aware UTC datetime, or an explicit failure."""
    try:
        return parse_aware_datetime(event.timestamp)
    except Exception as exc:
        raise IncidentError(
            f"event {event.event_id!r} has an unusable timestamp: {exc}"
        ) from exc


# ---------------------------------------------------------------------------
# Aggregation
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class WindowMetrics:
    """Deterministic aggregate of one segment inside one window.

    ``scored`` is the honest denominator: events with no evaluated outcome were
    neither recovered nor confirmed unrecovered, so counting them as misses
    would manufacture degradation out of missing evidence.
    """

    events: int
    scored: int
    recovered: int
    amount_paise: int
    recovered_amount_paise: int
    failure_reason_counts: Mapping[str, int]

    @property
    def recovery_rate_bps(self) -> int | None:
        """Recovered events over scored events, in integer basis points.

        The canonical RecoveryOS recovery rate — recovered events over events
        that produced an outcome, exactly as ``replay_metrics.recovery_rate``
        defines it — expressed in bps so that no rate is ever a float that
        later multiplies money. Floor division, so the figure never overstates
        recovery. ``None`` when there is no denominator; never a flattering 0.
        """
        if self.scored <= 0:
            return None
        return self.recovered * BPS // self.scored

    @property
    def unrecovered_rate_bps(self) -> int | None:
        """The share of scored payments left unrecovered, in bps.

        The failure-side reading of the same evidence. RecoveryOS only ever
        ingests payments that ALREADY failed, so "failure rate" in the classic
        sense is 100% by construction and would carry no information; the
        meaningful failure-side metric is how much of that failed volume the
        control plane did not recover. It is the exact complement of the
        recovery rate and is reported as evidence, not as a second independent
        signal, which is why it can never raise an incident of its own.
        """
        rate = self.recovery_rate_bps
        return None if rate is None else BPS - rate

    def to_dict(self) -> dict[str, Any]:
        """Serialize the window aggregate, including its denominators."""
        return {
            "events": self.events,
            "scored": self.scored,
            "recovered": self.recovered,
            "amount_paise": self.amount_paise,
            "simulated_recovered_amount_paise": self.recovered_amount_paise,
            "recovery_rate_bps": self.recovery_rate_bps,
            "unrecovered_rate_bps": self.unrecovered_rate_bps,
        }


def _aggregate(
    events: Sequence[PaymentEvent], outcomes: Mapping[str, EvaluatedOutcome]
) -> WindowMetrics:
    """Aggregate one already-filtered set of events into window metrics."""
    scored = 0
    recovered = 0
    recovered_amount = 0
    failure_reasons: dict[str, int] = {}
    for event in events:
        failure_reasons[event.failure_reason] = (
            failure_reasons.get(event.failure_reason, 0) + 1
        )
        outcome = outcomes.get(event.event_id)
        if outcome is None:
            continue
        scored += 1
        if outcome.recovered:
            recovered += 1
            recovered_amount += outcome.recovered_amount_paise
    return WindowMetrics(
        events=len(events),
        scored=scored,
        recovered=recovered,
        amount_paise=sum(event.amount_paise for event in events),
        recovered_amount_paise=recovered_amount,
        failure_reason_counts=dict(sorted(failure_reasons.items())),
    )


# ---------------------------------------------------------------------------
# Segments
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Segment:
    """One deterministic slice of the workload: dimensions bound to values."""

    dimensions: tuple[str, ...]
    values: tuple[str, ...]

    def __post_init__(self) -> None:
        if len(self.dimensions) != len(self.values) or not self.dimensions:
            raise IncidentError("a segment binds one value per dimension")

    @property
    def key(self) -> tuple[tuple[str, str], ...]:
        """The canonical sortable key: dimension/value pairs in fixed order."""
        return tuple(zip(self.dimensions, self.values))

    @property
    def label(self) -> str:
        """A human label built only from data, e.g. ``HDFC + upi``."""
        return " + ".join(self.values)

    def get(self, dimension: str) -> str | None:
        """The value bound to ``dimension``, or None if unsegmented on it."""
        for name, value in self.key:
            if name == dimension:
                return value
        return None

    def matches(self, event: PaymentEvent) -> bool:
        """Does ``event`` belong to this segment?"""
        return all(
            getattr(event, dimension) == value for dimension, value in self.key
        )

    def to_dict(self) -> dict[str, Any]:
        """Serialize the segment, including the dimensions it does not bind."""
        return {
            "dimensions": list(self.dimensions),
            "values": list(self.values),
            "label": self.label,
            "bank": self.get(DIMENSION_BANK),
            "payment_method": self.get(DIMENSION_PAYMENT_METHOD),
            "failure_reason": self.get(DIMENSION_FAILURE_REASON),
        }


def segments_for(
    events: Iterable[PaymentEvent],
    segmentations: Sequence[Sequence[str]] = SEGMENTATIONS,
) -> tuple[Segment, ...]:
    """Every segment present in the data, in canonical order.

    Ordered by segmentation (as declared) and then lexically by bound values,
    so the segment sequence is a pure function of the dataset and never of
    dictionary or set iteration order.
    """
    found: list[Segment] = []
    for dimensions in segmentations:
        dimensions = tuple(dimensions)
        values = {
            tuple(getattr(event, dimension) for dimension in dimensions)
            for event in events
        }
        found.extend(
            Segment(dimensions=dimensions, values=value)
            for value in sorted(values)
        )
    return tuple(found)


# ---------------------------------------------------------------------------
# Financial impact
# ---------------------------------------------------------------------------


def simulated_revenue_at_risk_paise(
    degradation_bps: int, current_window_amount_paise: int
) -> int:
    """The MODELLED revenue impact of the observed recovery-rate gap.

        max(0, baseline_rate - current_rate) x current window payment value

    Integer arithmetic end to end: a bps gap multiplied by paise and floored
    back into paise. Never negative — a segment that improved has no revenue at
    risk, and inventing a negative "risk" would be meaningless.

    WHAT THIS IS NOT: actual merchant loss, actual bank or provider loss,
    production revenue, or confirmed recoverable money. It is an estimate of
    what the observed gap would be worth on the current window's volume, built
    from simulated evaluation evidence.
    """
    if degradation_bps <= 0 or current_window_amount_paise <= 0:
        return 0
    return degradation_bps * current_window_amount_paise // BPS


# ---------------------------------------------------------------------------
# Severity
# ---------------------------------------------------------------------------


def severity_for(
    degradation_bps: int,
    affected_events: int,
    revenue_at_risk_paise: int,
) -> str:
    """The deterministic severity of one incident.

    Two gates, applied in a fixed order, with no probability and no model:

    1. DEVIATION PROPOSES. The largest level whose degradation threshold the
       observed gap meets is the candidate (>=15pp LOW, >=20pp MEDIUM, >=30pp
       HIGH, >=40pp CRITICAL).
    2. IMPACT CONFIRMS. Every level above LOW additionally requires meaningful
       impact — at least its affected-event count OR at least its
       revenue-at-risk amount. A candidate that fails its impact test is
       demoted one level and re-tested, down to LOW.

    So severity is ``min(deviation level, highest impact-qualified level)``, and
    a 60pp swing over six low-value payments stays LOW rather than becoming
    CRITICAL on the strength of a small denominator.
    """
    if degradation_bps < SEVERITY_DEGRADATION_BPS[SEVERITY_LOW]:
        raise IncidentError(
            "severity is only defined for a degradation that reached the "
            "detection threshold"
        )
    candidate = SEVERITY_LOW
    for level in SEVERITY_ORDER:
        if degradation_bps >= SEVERITY_DEGRADATION_BPS[level]:
            candidate = level
    while candidate != SEVERITY_LOW and not _impact_qualifies(
        candidate, affected_events, revenue_at_risk_paise
    ):
        candidate = SEVERITY_ORDER[SEVERITY_ORDER.index(candidate) - 1]
    return candidate


def _impact_qualifies(
    level: str, affected_events: int, revenue_at_risk_paise: int
) -> bool:
    """Does the impact meet ``level``'s event OR revenue threshold?"""
    return (
        affected_events >= SEVERITY_MIN_AFFECTED_EVENTS[level]
        or revenue_at_risk_paise >= SEVERITY_MIN_REVENUE_AT_RISK_PAISE[level]
    )


# ---------------------------------------------------------------------------
# Leading observed contributor
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Contributor:
    """The failure reason most present in the degraded window.

    Called a CONTRIBUTOR and never a root cause: this is a count and a
    movement, and RecoveryOS has established no causal link between the two.
    """

    failure_reason: str
    current_count: int
    baseline_count: int

    @property
    def increase(self) -> int:
        """How many more times it appears now than in the baseline window."""
        return self.current_count - self.baseline_count

    def to_dict(self) -> dict[str, Any]:
        return {
            "failure_reason": self.failure_reason,
            "current_count": self.current_count,
            "baseline_count": self.baseline_count,
            "increase_vs_baseline": self.increase,
        }


def leading_contributor(
    current: WindowMetrics, baseline: WindowMetrics
) -> Contributor | None:
    """The leading observed contributor, chosen deterministically.

    Ranked by highest current-window count, then by largest increase over the
    baseline, then lexically by failure reason. The final tie-break makes the
    answer unique for every possible dataset, so two runs can never disagree.
    """
    if not current.failure_reason_counts:
        return None
    reason = min(
        current.failure_reason_counts,
        key=lambda name: (
            -current.failure_reason_counts[name],
            -(
                current.failure_reason_counts[name]
                - baseline.failure_reason_counts.get(name, 0)
            ),
            name,
        ),
    )
    return Contributor(
        failure_reason=reason,
        current_count=current.failure_reason_counts[reason],
        baseline_count=baseline.failure_reason_counts.get(reason, 0),
    )


def top_failure_reasons(
    current: WindowMetrics,
    baseline: WindowMetrics,
    limit: int = TOP_FAILURE_REASONS,
) -> tuple[dict[str, Any], ...]:
    """The most frequent current-window failure reasons, with their movement."""
    ranked = sorted(
        current.failure_reason_counts,
        key=lambda name: (-current.failure_reason_counts[name], name),
    )
    return tuple(
        {
            "failure_reason": name,
            "current_count": current.failure_reason_counts[name],
            "baseline_count": baseline.failure_reason_counts.get(name, 0),
            "increase_vs_baseline": (
                current.failure_reason_counts[name]
                - baseline.failure_reason_counts.get(name, 0)
            ),
        }
        for name in ranked[:limit]
    )


# ---------------------------------------------------------------------------
# The incident
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Incident:
    """One detected revenue-degradation incident, fully explained by its evidence.

    Every field is computed from the two window aggregates; nothing is stored,
    narrated or hardcoded. ``affected_event_ids`` are references into the
    existing event records, so an incident points AT the payment decisions
    rather than copying them.
    """

    incident_id: str
    detected_at: str
    status: str
    segment: Segment
    windows: ObservationWindows
    baseline: WindowMetrics
    current: WindowMetrics
    degradation_bps: int
    unrecovered_rate_delta_bps: int
    simulated_revenue_at_risk_paise: int
    severity: str
    contributor: Contributor | None
    affected_event_ids: tuple[str, ...]
    methodology: str = INCIDENT_METHODOLOGY_VERSION
    result_mode: str = INCIDENT_RESULT_MODE

    def __post_init__(self) -> None:
        if self.result_mode != INCIDENT_RESULT_MODE:
            raise IncidentError(
                "incident figures are always SIMULATED/modelled readings of "
                "evaluation evidence"
            )
        if self.status not in (STATUS_OPEN, STATUS_RESOLVED):
            raise IncidentError(f"unknown incident status {self.status!r}")
        if self.severity not in SEVERITY_ORDER:
            raise IncidentError(f"unknown severity {self.severity!r}")
        if self.simulated_revenue_at_risk_paise < 0:
            raise IncidentError("simulated revenue at risk is never negative")
        if len(set(self.affected_event_ids)) != len(self.affected_event_ids):
            raise IncidentError("an event may appear at most once in an incident")

    @property
    def affected_event_count(self) -> int:
        """How many current-window payments in this segment stayed unrecovered."""
        return len(self.affected_event_ids)

    def to_dict(self) -> dict[str, Any]:
        """The complete, self-explaining incident payload.

        Everything the operator sees is here, and every number in it was
        computed above from the event set and its evaluated outcomes.
        """
        return {
            "incident_id": self.incident_id,
            "methodology": self.methodology,
            "result_mode": self.result_mode,
            "detected_at": self.detected_at,
            "detected_at_source": (
                "latest observed event timestamp, not a production detection time"
            ),
            "status": self.status,
            "severity": self.severity,
            "segment": self.segment.to_dict(),
            "title": f"{self.segment.label} recovery degradation",
            "windows": self.windows.to_dict(),
            "baseline": self.baseline.to_dict(),
            "current": self.current.to_dict(),
            "deltas": {
                "recovery_rate_delta_bps": -self.degradation_bps,
                "degradation_bps": self.degradation_bps,
                "unrecovered_rate_delta_bps": self.unrecovered_rate_delta_bps,
            },
            "impact": {
                "affected_event_count": self.affected_event_count,
                "current_window_events": self.current.events,
                "current_window_amount_paise": self.current.amount_paise,
                "simulated_revenue_at_risk_paise": (
                    self.simulated_revenue_at_risk_paise
                ),
                "basis": (
                    "modelled estimate: observed recovery-rate gap applied to "
                    "the current window's payment value; not production revenue"
                ),
            },
            "evidence": {
                "leading_observed_contributor": (
                    None if self.contributor is None else self.contributor.to_dict()
                ),
                "top_failure_reasons": list(
                    top_failure_reasons(self.current, self.baseline)
                ),
            },
            "affected_event_ids": list(self.affected_event_ids),
        }


def incident_id_for(
    segment: Segment,
    windows: ObservationWindows,
    baseline: WindowMetrics,
    current: WindowMetrics,
    config: DetectionConfig,
) -> str:
    """The deterministic identity of one incident.

    A hash of everything that defines the incident — methodology, detector
    configuration, both window bounds, the segment, and the observed metrics on
    both sides. No wall clock and no random component participates, so the same
    dataset under the same configuration always yields the same ids, and any
    change to what was observed yields a different one.

    Canonical JSON with sorted keys digested with blake2b, matching the
    fingerprint convention already used by the benchmark, the policy scenarios
    and the event generator.
    """
    payload = {
        "config": config.to_dict(),
        "segment": {
            "dimensions": list(segment.dimensions),
            "values": list(segment.values),
        },
        "windows": windows.to_dict(),
        "baseline": baseline.to_dict(),
        "current": current.to_dict(),
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    digest = hashlib.blake2b(encoded.encode("utf-8"), digest_size=12).hexdigest()
    return f"incident:{config.methodology}:{digest}"


# ---------------------------------------------------------------------------
# Detection
# ---------------------------------------------------------------------------


def detect_incidents(
    events: Sequence[PaymentEvent],
    outcomes: Mapping[str, EvaluatedOutcome] | Sequence[EvaluatedOutcome],
    *,
    config: DetectionConfig | None = None,
) -> tuple[Incident, ...]:
    """Detect every revenue-degradation incident in one dataset.

    The whole methodology, in order:

        partition into two equal windows -> aggregate per segment ->
        gate on sample size -> compare recovery rates -> threshold ->
        model the financial impact -> assign severity -> resolve evidence

    Pure: no database, no HTTP, no clock, no randomness, no I/O. The same
    arguments always produce the same incidents, with the same ids, in the same
    order.
    """
    config = config or DetectionConfig()
    if not events:
        return ()
    outcome_map = _outcome_map(outcomes)
    windows = observation_windows(events, config)

    current_events: list[PaymentEvent] = []
    baseline_events: list[PaymentEvent] = []
    for event in events:
        moment = _event_time(event)
        if windows.contains_current(moment):
            current_events.append(event)
        elif windows.contains_baseline(moment):
            baseline_events.append(event)

    incidents: list[Incident] = []
    for segment in segments_for(current_events + baseline_events):
        incident = _detect_one(
            segment, windows, current_events, baseline_events, outcome_map, config
        )
        if incident is not None:
            incidents.append(incident)
    return order_incidents(incidents)


def _detect_one(
    segment: Segment,
    windows: ObservationWindows,
    current_events: Sequence[PaymentEvent],
    baseline_events: Sequence[PaymentEvent],
    outcomes: Mapping[str, EvaluatedOutcome],
    config: DetectionConfig,
) -> Incident | None:
    """Evaluate one segment, returning an incident only if the rules are met."""
    segment_current = [event for event in current_events if segment.matches(event)]
    segment_baseline = [event for event in baseline_events if segment.matches(event)]

    current = _aggregate(segment_current, outcomes)
    baseline = _aggregate(segment_baseline, outcomes)

    # Sample-size gate: eligibility only, never severity.
    if (
        current.scored < config.min_current_observations
        or baseline.scored < config.min_baseline_observations
    ):
        return None

    current_rate = current.recovery_rate_bps
    baseline_rate = baseline.recovery_rate_bps
    if current_rate is None or baseline_rate is None:
        return None

    degradation_bps = baseline_rate - current_rate
    if degradation_bps < config.degradation_threshold_bps:
        return None

    revenue_at_risk = simulated_revenue_at_risk_paise(
        degradation_bps, current.amount_paise
    )
    # The payments this incident covers: current-window payments in the segment
    # that were evaluated and stayed unrecovered. Sorted by event id so the
    # subset — and therefore any replay of it — is order-independent.
    affected = tuple(
        sorted(
            event.event_id
            for event in segment_current
            if event.event_id in outcomes and not outcomes[event.event_id].recovered
        )
    )
    return Incident(
        incident_id=incident_id_for(segment, windows, baseline, current, config),
        detected_at=windows.anchor.isoformat(),
        status=STATUS_OPEN,
        segment=segment,
        windows=windows,
        baseline=baseline,
        current=current,
        degradation_bps=degradation_bps,
        # The failure-side complement of the same movement, reported as
        # supporting evidence rather than as an independent detector.
        unrecovered_rate_delta_bps=degradation_bps,
        simulated_revenue_at_risk_paise=revenue_at_risk,
        severity=severity_for(degradation_bps, len(affected), revenue_at_risk),
        contributor=leading_contributor(current, baseline),
        affected_event_ids=affected,
        methodology=config.methodology,
    )


def order_incidents(incidents: Sequence[Incident]) -> tuple[Incident, ...]:
    """Canonical presentation order: worst modelled financial impact first.

    Ties break on degradation and then on incident id, which is unique, so the
    ordering is total and reproducible rather than dependent on detection order.
    """
    return tuple(
        sorted(
            incidents,
            key=lambda incident: (
                -incident.simulated_revenue_at_risk_paise,
                -incident.degradation_bps,
                incident.incident_id,
            ),
        )
    )


def _outcome_map(
    outcomes: Mapping[str, EvaluatedOutcome] | Sequence[EvaluatedOutcome],
) -> dict[str, EvaluatedOutcome]:
    """Accept either a mapping or a sequence of outcomes, keyed by event id."""
    if isinstance(outcomes, Mapping):
        items = outcomes.values()
    else:
        items = outcomes
    resolved: dict[str, EvaluatedOutcome] = {}
    for outcome in items:
        if not isinstance(outcome, EvaluatedOutcome):
            raise IncidentError("every outcome must be an EvaluatedOutcome")
        if outcome.event_id in resolved:
            raise IncidentError(
                f"duplicate evaluated outcome for event {outcome.event_id!r}"
            )
        resolved[outcome.event_id] = outcome
    return resolved

"""Phase 20: deterministic incident detection over events and evaluated outcomes.

Every dataset here is built explicitly in the test, so each assertion states
the exact evidence that produces (or refuses to produce) an incident. Nothing
depends on the canonical generator, on the database, or on the wall clock.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from app.incidents import (
    DIMENSION_BANK,
    DIMENSION_PAYMENT_METHOD,
    SEVERITY_LOW,
    STATUS_OPEN,
    DetectionConfig,
    EvaluatedOutcome,
    Incident,
    IncidentError,
    Segment,
    detect_incidents,
    observation_windows,
    order_incidents,
    segments_for,
)
from app.models import CustomerHistory, PaymentEvent

ANCHOR = datetime(2026, 8, 27, 12, 0, tzinfo=timezone.utc)
HISTORY = CustomerHistory(
    prior_successful_payments=3,
    prior_failed_payments=1,
    has_active_subscription=False,
)


def event(
    event_id: str,
    *,
    days_before_anchor: float = 1,
    bank: str = "HDFC",
    payment_method: str = "upi",
    failure_reason: str = "bank_timeout",
    amount_paise: int = 100_000,
) -> PaymentEvent:
    """One PaymentEvent placed relative to the dataset's latest timestamp."""
    return PaymentEvent(
        event_id=event_id,
        order_id=f"order_{event_id}",
        payment_id=f"pay_{event_id}",
        customer_id=f"cust_{event_id}",
        amount_paise=amount_paise,
        currency="INR",
        payment_method=payment_method,
        failure_reason=failure_reason,
        bank=bank,
        risk_flag="normal",
        customer_history=HISTORY,
        timestamp=(ANCHOR - timedelta(days=days_before_anchor)).isoformat(),
    )


def anchor_event() -> PaymentEvent:
    """An event exactly on the anchor, so the windows are pinned to ANCHOR.

    Deliberately placed in its own bank/method/reason so it pins the windows
    without joining — and skewing — the segment under test.
    """
    return event(
        "evt_anchor",
        days_before_anchor=0,
        bank="Anchor Bank",
        payment_method="wallet",
        failure_reason="anchor_only",
    )


def window_events(
    prefix: str, count: int, *, days_before_anchor: float, **kwargs
) -> list[PaymentEvent]:
    """``count`` events inside one window, distinguishable by id."""
    return [
        event(f"{prefix}_{index:03d}", days_before_anchor=days_before_anchor, **kwargs)
        for index in range(count)
    ]


def outcomes(
    events, recovered_count: int, *, recovered_amount_paise: int | None = None
) -> list[EvaluatedOutcome]:
    """Mark the first ``recovered_count`` events as simulated-recovered."""
    built = []
    for index, item in enumerate(events):
        recovered = index < recovered_count
        amount = (
            item.amount_paise
            if recovered_amount_paise is None
            else recovered_amount_paise
        )
        built.append(
            EvaluatedOutcome(
                event_id=item.event_id,
                recovered=recovered,
                recovered_amount_paise=amount if recovered else 0,
            )
        )
    return built


def dataset(current_recovered: int, baseline_recovered: int, *, count: int = 10):
    """A single-segment dataset with the given per-window recovery counts."""
    current = window_events("cur", count, days_before_anchor=1)
    baseline = window_events("base", count, days_before_anchor=35)
    events = [anchor_event(), *current, *baseline]
    evaluated = [
        *outcomes([anchor_event()], 1),
        *outcomes(current, current_recovered),
        *outcomes(baseline, baseline_recovered),
    ]
    return events, evaluated


# ---------------------------------------------------------------------------
# Detection
# ---------------------------------------------------------------------------


def test_obvious_degradation_produces_an_incident():
    """Baseline 80% recovery falling to 20% is a 60pp degradation."""
    incidents = detect_incidents(*dataset(current_recovered=2, baseline_recovered=8))

    assert incidents, "a 60pp recovery-rate collapse must be detected"
    worst = incidents[0]
    assert worst.degradation_bps == 6000
    assert worst.baseline.recovery_rate_bps == 8000
    assert worst.current.recovery_rate_bps == 2000
    assert worst.status == STATUS_OPEN
    assert worst.detected_at == ANCHOR.isoformat()


def test_stable_performance_produces_no_incident():
    assert detect_incidents(*dataset(current_recovered=8, baseline_recovered=8)) == ()


def test_improvement_produces_no_incident():
    """Recovery getting better is never an incident, at any magnitude."""
    assert detect_incidents(*dataset(current_recovered=9, baseline_recovered=2)) == ()


def test_exactly_the_threshold_is_an_incident():
    """A 15pp fall (75% -> 60%) is detected; the threshold is inclusive."""
    incidents = detect_incidents(
        *dataset(current_recovered=12, baseline_recovered=15, count=20)
    )
    assert incidents
    assert incidents[0].degradation_bps == 1500
    assert incidents[0].severity == SEVERITY_LOW


def test_just_below_the_threshold_is_not_an_incident():
    """A 14pp fall (75% -> 61%) stays below the bar and raises nothing."""
    assert (
        detect_incidents(*dataset(current_recovered=61, baseline_recovered=75, count=100))
        == ()
    )


def test_no_events_produces_no_incidents():
    assert detect_incidents([], []) == ()


def test_events_without_evaluated_outcomes_produce_no_incident():
    """Missing evidence is missing evidence; it is never read as a failure."""
    events, _ = dataset(current_recovered=0, baseline_recovered=10)
    assert detect_incidents(events, []) == ()


# ---------------------------------------------------------------------------
# Sample-size protection
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("count", [1, 3, 4])
def test_a_tiny_current_sample_raises_nothing(count):
    """Total collapse over four payments is still not an incident."""
    current = window_events("cur", count, days_before_anchor=1)
    baseline = window_events("base", 20, days_before_anchor=35)
    events = [anchor_event(), *current, *baseline]
    evaluated = [
        *outcomes([anchor_event()], 0),
        *outcomes(current, 0),
        *outcomes(baseline, 20),
    ]
    assert detect_incidents(events, evaluated) == ()


def test_five_current_and_five_baseline_observations_are_eligible():
    current = window_events("cur", 5, days_before_anchor=1)
    baseline = window_events("base", 5, days_before_anchor=35)
    events = [anchor_event(), *current, *baseline]
    evaluated = [
        *outcomes([anchor_event()], 0),
        *outcomes(current, 0),
        *outcomes(baseline, 5),
    ]
    assert detect_incidents(events, evaluated)


def test_an_insufficient_baseline_raises_nothing():
    current = window_events("cur", 20, days_before_anchor=1)
    baseline = window_events("base", 4, days_before_anchor=35)
    events = [anchor_event(), *current, *baseline]
    evaluated = [
        *outcomes([anchor_event()], 0),
        *outcomes(current, 0),
        *outcomes(baseline, 4),
    ]
    assert detect_incidents(events, evaluated) == ()


def test_the_sample_gate_counts_scored_events_not_ingested_events():
    """Twenty events with four outcomes is a sample of four, not of twenty."""
    current = window_events("cur", 20, days_before_anchor=1)
    baseline = window_events("base", 20, days_before_anchor=35)
    events = [anchor_event(), *current, *baseline]
    evaluated = [
        *outcomes([anchor_event()], 0),
        *outcomes(current[:4], 0),
        *outcomes(baseline, 20),
    ]
    assert detect_incidents(events, evaluated) == ()


# ---------------------------------------------------------------------------
# Time windows
# ---------------------------------------------------------------------------


def test_windows_are_anchored_on_the_latest_observed_event():
    windows = observation_windows([event("a", days_before_anchor=5), anchor_event()])
    assert windows.anchor == ANCHOR
    assert windows.current_start == ANCHOR - timedelta(days=28)
    assert windows.baseline_start == ANCHOR - timedelta(days=56)


def test_window_boundaries_are_start_exclusive_and_end_inclusive():
    windows = observation_windows([anchor_event()])
    boundary = ANCHOR - timedelta(days=28)

    assert windows.contains_current(ANCHOR)
    assert not windows.contains_current(boundary)
    assert windows.contains_baseline(boundary)
    assert not windows.contains_baseline(ANCHOR - timedelta(days=56))


def test_an_event_on_the_shared_boundary_counts_once_in_the_baseline():
    current = window_events("cur", 10, days_before_anchor=1)
    baseline = window_events("base", 9, days_before_anchor=35)
    seam = [event("evt_seam", days_before_anchor=28)]
    events = [anchor_event(), *current, *baseline, *seam]
    evaluated = [
        *outcomes([anchor_event()], 1),
        *outcomes(current, 0),
        *outcomes(baseline, 9),
        *outcomes(seam, 1),
    ]
    incident = detect_incidents(events, evaluated)[0]
    assert incident.baseline.events == 10
    assert incident.current.events == 10


def test_events_outside_both_windows_are_ignored():
    current = window_events("cur", 10, days_before_anchor=1)
    baseline = window_events("base", 10, days_before_anchor=35)
    ancient = window_events("old", 50, days_before_anchor=200)
    events = [anchor_event(), *current, *baseline, *ancient]
    evaluated = [
        *outcomes([anchor_event()], 1),
        *outcomes(current, 1),
        *outcomes(baseline, 10),
        *outcomes(ancient, 50),
    ]
    incident = detect_incidents(events, evaluated)[0]
    assert incident.baseline.events == 10
    assert incident.current.scored == 10


def test_a_naive_timestamp_is_refused_rather_than_assumed_utc():
    naive = PaymentEvent.from_dict(
        {**anchor_event().to_dict(), "timestamp": "2026-08-27T12:00:00"}
    )
    with pytest.raises(IncidentError):
        observation_windows([naive])


# ---------------------------------------------------------------------------
# Segmentation
# ---------------------------------------------------------------------------


def test_every_supported_segmentation_is_evaluated():
    current = window_events("cur", 10, days_before_anchor=1)
    baseline = window_events("base", 10, days_before_anchor=35)
    events = [anchor_event(), *current, *baseline]
    evaluated = [
        *outcomes([anchor_event()], 0),
        *outcomes(current, 0),
        *outcomes(baseline, 10),
    ]
    dimensions = {
        tuple(incident.segment.dimensions)
        for incident in detect_incidents(events, evaluated)
    }
    assert dimensions == {
        ("bank",),
        ("payment_method",),
        ("failure_reason",),
        ("bank", "payment_method"),
    }


def test_a_degradation_confined_to_one_composite_segment_is_isolated():
    """HDFC+UPI collapses while ICICI+card holds; only the former degrades."""
    degraded_current = window_events(
        "dcur", 10, days_before_anchor=1, bank="HDFC", payment_method="upi"
    )
    degraded_baseline = window_events(
        "dbase", 10, days_before_anchor=35, bank="HDFC", payment_method="upi"
    )
    healthy_current = window_events(
        "hcur", 10, days_before_anchor=1, bank="ICICI", payment_method="card"
    )
    healthy_baseline = window_events(
        "hbase", 10, days_before_anchor=35, bank="ICICI", payment_method="card"
    )
    events = [
        anchor_event(),
        *degraded_current,
        *degraded_baseline,
        *healthy_current,
        *healthy_baseline,
    ]
    evaluated = [
        *outcomes([anchor_event()], 0),
        *outcomes(degraded_current, 1),
        *outcomes(degraded_baseline, 9),
        *outcomes(healthy_current, 8),
        *outcomes(healthy_baseline, 8),
    ]
    labels = {
        incident.segment.label for incident in detect_incidents(events, evaluated)
    }
    assert "HDFC + upi" in labels
    assert "ICICI + card" not in labels
    assert "ICICI" not in labels


def test_segment_ordering_is_deterministic_and_data_driven():
    events = [
        event("a", bank="ICICI", payment_method="card"),
        event("b", bank="HDFC", payment_method="upi"),
    ]
    banks = [
        segment.values[0]
        for segment in segments_for(events)
        if segment.dimensions == (DIMENSION_BANK,)
    ]
    assert banks == ["HDFC", "ICICI"]
    composites = [
        segment.values
        for segment in segments_for(events)
        if segment.dimensions == (DIMENSION_BANK, DIMENSION_PAYMENT_METHOD)
    ]
    assert composites == [("HDFC", "upi"), ("ICICI", "card")]


def test_a_segment_binds_one_value_per_dimension():
    with pytest.raises(IncidentError):
        Segment(dimensions=(DIMENSION_BANK,), values=("HDFC", "upi"))


# ---------------------------------------------------------------------------
# Impact, evidence and ordering
# ---------------------------------------------------------------------------


def test_affected_events_are_the_unrecovered_current_window_payments():
    events, evaluated = dataset(current_recovered=2, baseline_recovered=9)
    incident = next(
        item
        for item in detect_incidents(events, evaluated)
        if item.segment.dimensions == (DIMENSION_BANK,)
    )
    assert incident.affected_event_count == 8
    assert list(incident.affected_event_ids) == sorted(incident.affected_event_ids)
    assert all(
        event_id.startswith("cur_") for event_id in incident.affected_event_ids
    )


def test_revenue_at_risk_matches_the_incident_evidence():
    events, evaluated = dataset(current_recovered=2, baseline_recovered=8)
    incident = detect_incidents(events, evaluated)[0]
    assert incident.simulated_revenue_at_risk_paise == (
        incident.degradation_bps * incident.current.amount_paise // 10_000
    )


def test_incidents_are_ordered_by_modelled_impact_then_identity():
    events, evaluated = dataset(current_recovered=2, baseline_recovered=8)
    incidents = detect_incidents(events, evaluated)
    keys = [
        (-incident.simulated_revenue_at_risk_paise, -incident.degradation_bps, incident.incident_id)
        for incident in incidents
    ]
    assert keys == sorted(keys)
    assert order_incidents(list(reversed(incidents))) == incidents


def test_multiple_independent_incidents_are_reported_together():
    hdfc_current = window_events("hc", 10, days_before_anchor=1, bank="HDFC")
    hdfc_baseline = window_events("hb", 10, days_before_anchor=35, bank="HDFC")
    axis_current = window_events("ac", 10, days_before_anchor=1, bank="Axis")
    axis_baseline = window_events("ab", 10, days_before_anchor=35, bank="Axis")
    events = [
        anchor_event(),
        *hdfc_current,
        *hdfc_baseline,
        *axis_current,
        *axis_baseline,
    ]
    evaluated = [
        *outcomes([anchor_event()], 0),
        *outcomes(hdfc_current, 1),
        *outcomes(hdfc_baseline, 9),
        *outcomes(axis_current, 2),
        *outcomes(axis_baseline, 8),
    ]
    banks = {
        incident.segment.label
        for incident in detect_incidents(events, evaluated)
        if incident.segment.dimensions == (DIMENSION_BANK,)
    }
    assert banks == {"HDFC", "Axis"}


def test_the_leading_contributor_is_computed_from_the_current_window():
    current = [
        *window_events("t", 8, days_before_anchor=1, failure_reason="bank_timeout"),
        *window_events("e", 2, days_before_anchor=1, failure_reason="expired_card"),
    ]
    baseline = window_events(
        "base", 10, days_before_anchor=35, failure_reason="bank_timeout"
    )
    events = [anchor_event(), *current, *baseline]
    evaluated = [
        *outcomes([anchor_event()], 0),
        *outcomes(current, 1),
        *outcomes(baseline, 9),
    ]
    incident = next(
        item
        for item in detect_incidents(events, evaluated)
        if item.segment.dimensions == (DIMENSION_BANK,)
    )
    assert incident.contributor.failure_reason == "bank_timeout"
    assert incident.contributor.current_count == 8


# ---------------------------------------------------------------------------
# Identity and determinism
# ---------------------------------------------------------------------------


def test_the_same_dataset_detects_identical_incidents_twice():
    events, evaluated = dataset(current_recovered=2, baseline_recovered=8)
    first = detect_incidents(events, evaluated)
    second = detect_incidents(list(reversed(events)), list(reversed(evaluated)))

    assert [incident.incident_id for incident in first] == [
        incident.incident_id for incident in second
    ]
    assert [incident.to_dict() for incident in first] == [
        incident.to_dict() for incident in second
    ]


def test_incident_ids_carry_no_wall_clock_and_no_randomness():
    events, evaluated = dataset(current_recovered=2, baseline_recovered=8)
    incident = detect_incidents(events, evaluated)[0]
    assert incident.incident_id.startswith("incident:phase20-")
    assert incident.incident_id == detect_incidents(events, evaluated)[0].incident_id


def test_a_different_observation_changes_the_incident_identity():
    first = detect_incidents(*dataset(current_recovered=2, baseline_recovered=8))[0]
    second = detect_incidents(*dataset(current_recovered=1, baseline_recovered=8))[0]
    assert first.incident_id != second.incident_id


def test_a_different_detector_configuration_changes_the_identity():
    events, evaluated = dataset(current_recovered=2, baseline_recovered=8)
    default = detect_incidents(events, evaluated)[0]
    tighter = detect_incidents(
        events, evaluated, config=DetectionConfig(min_current_observations=6)
    )[0]
    assert default.incident_id != tighter.incident_id


def test_detection_configuration_is_validated():
    for bad in (
        {"window_days": 0},
        {"min_current_observations": -1},
        {"degradation_threshold_bps": 10_001},
        {"methodology": " "},
    ):
        with pytest.raises(IncidentError):
            DetectionConfig(**bad)


def test_a_duplicate_evaluated_outcome_is_refused():
    events, evaluated = dataset(current_recovered=2, baseline_recovered=8)
    with pytest.raises(IncidentError):
        detect_incidents(events, [*evaluated, evaluated[0]])


def test_an_incident_is_always_a_simulated_reading():
    events, evaluated = dataset(current_recovered=2, baseline_recovered=8)
    incident = detect_incidents(events, evaluated)[0]
    with pytest.raises(IncidentError):
        Incident(
            **{
                **incident.__dict__,
                "result_mode": "REAL_RAZORPAY",
            }
        )

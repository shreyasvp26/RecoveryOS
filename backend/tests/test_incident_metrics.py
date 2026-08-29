"""Phase 20: the incident metric, financial and severity rules.

These are unit tests of the arithmetic itself — rates, the modelled revenue at
risk, severity boundaries and the leading contributor tie-break — so a change
to any published number has to change a test that states the number.
"""

from __future__ import annotations

import pytest

from app.incidents import (
    BPS,
    SEVERITY_CRITICAL,
    SEVERITY_HIGH,
    SEVERITY_LOW,
    SEVERITY_MEDIUM,
    Contributor,
    EvaluatedOutcome,
    IncidentError,
    WindowMetrics,
    leading_contributor,
    severity_for,
    simulated_revenue_at_risk_paise,
    top_failure_reasons,
)

PP = 100  # one percentage point, in basis points


def metrics(
    *,
    events: int = 10,
    scored: int = 10,
    recovered: int = 5,
    amount_paise: int = 1_000_000,
    recovered_amount_paise: int = 500_000,
    failure_reason_counts: dict[str, int] | None = None,
) -> WindowMetrics:
    return WindowMetrics(
        events=events,
        scored=scored,
        recovered=recovered,
        amount_paise=amount_paise,
        recovered_amount_paise=recovered_amount_paise,
        failure_reason_counts=failure_reason_counts or {},
    )


# ---------------------------------------------------------------------------
# Rates
# ---------------------------------------------------------------------------


def test_recovery_rate_is_recovered_over_scored_in_basis_points():
    assert metrics(scored=100, recovered=72).recovery_rate_bps == 72 * PP


def test_recovery_rate_excludes_unscored_events_from_the_denominator():
    """An event with no evaluated outcome is not a miss; it is not evidence."""
    assert metrics(events=100, scored=50, recovered=25).recovery_rate_bps == 50 * PP


def test_recovery_rate_without_a_denominator_is_none_not_zero():
    assert metrics(events=3, scored=0, recovered=0).recovery_rate_bps is None
    assert metrics(events=3, scored=0, recovered=0).unrecovered_rate_bps is None


def test_unrecovered_rate_is_the_complement_of_the_recovery_rate():
    window = metrics(scored=100, recovered=72)
    assert window.unrecovered_rate_bps == BPS - window.recovery_rate_bps
    assert window.unrecovered_rate_bps == 28 * PP


def test_recovery_rate_floors_rather_than_rounding_up():
    """1/3 is 33.33%; the reported rate never overstates recovery."""
    assert metrics(scored=3, recovered=1).recovery_rate_bps == 3333


# ---------------------------------------------------------------------------
# Simulated revenue at risk
# ---------------------------------------------------------------------------


def test_revenue_at_risk_applies_the_gap_to_current_window_value():
    # 18pp of ₹1,00,000 (10,000,000 paise) = ₹18,000 (1,800,000 paise).
    assert simulated_revenue_at_risk_paise(18 * PP, 10_000_000) == 1_800_000


def test_revenue_at_risk_is_integer_paise_arithmetic():
    value = simulated_revenue_at_risk_paise(1533, 987_654)
    assert isinstance(value, int)
    assert value == 1533 * 987_654 // BPS


def test_revenue_at_risk_is_zero_when_performance_did_not_degrade():
    assert simulated_revenue_at_risk_paise(0, 10_000_000) == 0
    assert simulated_revenue_at_risk_paise(-2500, 10_000_000) == 0


def test_revenue_at_risk_is_zero_without_current_window_value():
    assert simulated_revenue_at_risk_paise(18 * PP, 0) == 0


# ---------------------------------------------------------------------------
# Severity
# ---------------------------------------------------------------------------

# Impact that qualifies for every level, so a boundary test isolates deviation.
BIG_IMPACT = {"affected_events": 500, "revenue_at_risk_paise": 100_000_000}


@pytest.mark.parametrize(
    "degradation_pp,expected",
    [
        (15, SEVERITY_LOW),
        (19, SEVERITY_LOW),
        (20, SEVERITY_MEDIUM),
        (29, SEVERITY_MEDIUM),
        (30, SEVERITY_HIGH),
        (39, SEVERITY_HIGH),
        (40, SEVERITY_CRITICAL),
        (100, SEVERITY_CRITICAL),
    ],
)
def test_severity_deviation_boundaries(degradation_pp, expected):
    assert severity_for(degradation_pp * PP, **BIG_IMPACT) == expected


def test_severity_below_the_detection_threshold_is_refused():
    """Severity is only defined for something that is already an incident."""
    with pytest.raises(IncidentError):
        severity_for(14 * PP, **BIG_IMPACT)


@pytest.mark.parametrize(
    "affected,expected",
    [(9, SEVERITY_LOW), (10, SEVERITY_MEDIUM), (24, SEVERITY_MEDIUM), (25, SEVERITY_HIGH)],
)
def test_affected_event_thresholds_promote_severity(affected, expected):
    assert severity_for(35 * PP, affected, 0) == expected


@pytest.mark.parametrize(
    "revenue,expected",
    [
        (999_999, SEVERITY_LOW),
        (1_000_000, SEVERITY_MEDIUM),
        (4_999_999, SEVERITY_MEDIUM),
        (5_000_000, SEVERITY_HIGH),
        (9_999_999, SEVERITY_HIGH),
        (10_000_000, SEVERITY_CRITICAL),
    ],
)
def test_revenue_at_risk_thresholds_promote_severity(revenue, expected):
    assert severity_for(45 * PP, 0, revenue) == expected


def test_a_large_deviation_on_a_tiny_impact_stays_low():
    """A 60pp swing over 6 small payments is an observation, not a crisis."""
    assert severity_for(60 * PP, 6, 40_000) == SEVERITY_LOW


def test_impact_can_never_promote_beyond_the_deviation_level():
    assert severity_for(16 * PP, 10_000, 10_000_000_000) == SEVERITY_LOW


def test_either_impact_measure_alone_qualifies():
    assert severity_for(30 * PP, 25, 0) == SEVERITY_HIGH
    assert severity_for(30 * PP, 0, 5_000_000) == SEVERITY_HIGH


# ---------------------------------------------------------------------------
# Leading observed contributor
# ---------------------------------------------------------------------------


def test_leading_contributor_is_the_most_frequent_current_failure_reason():
    current = metrics(failure_reason_counts={"bank_timeout": 9, "expired_card": 4})
    baseline = metrics(failure_reason_counts={"bank_timeout": 2})
    assert leading_contributor(current, baseline) == Contributor(
        failure_reason="bank_timeout", current_count=9, baseline_count=2
    )


def test_leading_contributor_breaks_a_count_tie_on_the_larger_increase():
    current = metrics(failure_reason_counts={"a_reason": 5, "b_reason": 5})
    baseline = metrics(failure_reason_counts={"a_reason": 4, "b_reason": 1})
    assert leading_contributor(current, baseline).failure_reason == "b_reason"


def test_leading_contributor_breaks_a_full_tie_lexically():
    current = metrics(failure_reason_counts={"b_reason": 5, "a_reason": 5})
    baseline = metrics(failure_reason_counts={"a_reason": 1, "b_reason": 1})
    assert leading_contributor(current, baseline).failure_reason == "a_reason"


def test_leading_contributor_is_none_without_evidence():
    assert leading_contributor(metrics(), metrics()) is None


def test_top_failure_reasons_are_ranked_and_carry_their_movement():
    current = metrics(
        failure_reason_counts={"a": 2, "b": 7, "c": 7},
    )
    baseline = metrics(failure_reason_counts={"b": 1})
    ranked = top_failure_reasons(current, baseline, limit=2)
    assert [item["failure_reason"] for item in ranked] == ["b", "c"]
    assert ranked[0]["increase_vs_baseline"] == 6
    assert ranked[1]["baseline_count"] == 0


# ---------------------------------------------------------------------------
# The evidence contract
# ---------------------------------------------------------------------------


def test_evaluated_outcome_refuses_inconsistent_recovery_evidence():
    with pytest.raises(IncidentError):
        EvaluatedOutcome(event_id="evt_1", recovered=True, recovered_amount_paise=0)
    with pytest.raises(IncidentError):
        EvaluatedOutcome(event_id="evt_1", recovered=False, recovered_amount_paise=5)


def test_evaluated_outcome_carries_no_ground_truth_fields():
    """The evidence contract has no slot a hidden probability could ride in."""
    fields = set(
        EvaluatedOutcome(
            event_id="evt_1", recovered=False, recovered_amount_paise=0
        ).__dict__
    )
    assert fields == {"event_id", "recovered", "recovered_amount_paise"}

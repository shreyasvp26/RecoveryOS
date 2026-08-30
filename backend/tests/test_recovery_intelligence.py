"""Phase 22 calibration and performance analytics tests.

The aggregation functions are pure, so they are exercised directly on
constructed observations. That lets the calibration arithmetic be pinned
exactly — including the sign of the gap, which must never be reversed.
"""

from __future__ import annotations

import pytest

from app.outcome_feedback import (
    OUTCOME_NOT_RECOVERED,
    OUTCOME_PENDING,
    OUTCOME_RECOVERED,
    REASON_AWAITING_OUTCOME,
    REASON_CALIBRATION_ELIGIBLE,
    FeedbackObservation,
)
from app.recovery_intelligence import (
    BELOW_THRESHOLD,
    INSUFFICIENT_OBSERVATIONS,
    MIN_OBSERVATIONS,
    POSITIVE_EVIDENCE_ONLY,
    calibration,
    expected_vs_realized,
    intervention_performance,
    segment_performance,
)


def _observation(
    *,
    event_id: str = "evt",
    intervention: str = "payment_link",
    predicted_bps: int | None = 5_000,
    recovered: bool | None = True,
    recovered_amount_paise: int | None = 100_000,
    expected_recovered_value_paise: int | None = 50_000,
    payment_method: str = "upi",
    bank: str = "HDFC",
    failure_reason: str = "bank_timeout",
) -> FeedbackObservation:
    """Build one observation for the pure aggregation functions.

    ``recovered=True`` is a verified recovery, ``recovered=False`` is an
    authoritative terminal negative outcome (which the current provider
    contract does not actually produce — these exist so the calibration
    arithmetic can be pinned), and ``recovered=None`` is an unsettled
    observation that must never enter a statistic.
    """
    terminal = recovered is not None
    if recovered is True:
        outcome = OUTCOME_RECOVERED
    elif recovered is False:
        outcome = OUTCOME_NOT_RECOVERED
    else:
        outcome = OUTCOME_PENDING
    return FeedbackObservation(
        event_id=event_id,
        intervention=intervention,
        execution_mode="REAL_RAZORPAY",
        execution_status="SUCCESS",
        executed_at="2026-08-30T09:00:00+00:00",
        decided_at="2026-08-30T08:59:00+00:00",
        predicted_probability_bps=predicted_bps,
        expected_recovered_value_paise=expected_recovered_value_paise,
        amount_paise=100_000,
        payment_method=payment_method,
        bank=bank,
        failure_reason=failure_reason,
        payment_link_id="plink",
        outcome=outcome,
        terminal=terminal,
        calibration_eligible=terminal and predicted_bps is not None,
        verified_recovery=recovered is True,
        reason=REASON_CALIBRATION_ELIGIBLE if terminal else REASON_AWAITING_OUTCOME,
        recovered=recovered,
        recovered_amount_paise=recovered_amount_paise if recovered else None,
        observed_at="2026-08-30T09:30:00+00:00" if recovered else None,
        evidence_id="delivery" if recovered else None,
        note="test observation",
    )


def _observations(specs) -> list[FeedbackObservation]:
    return [
        _observation(event_id=f"evt_{index}", predicted_bps=bps, recovered=recovered)
        for index, (bps, recovered) in enumerate(specs)
    ]


def _padded(specs):
    """Extend a spec list to exactly MIN_OBSERVATIONS by repeating its cycle."""
    return [specs[index % len(specs)] for index in range(MIN_OBSERVATIONS)]


# ---------------------------------------------------------------------------
# Calibration arithmetic
# ---------------------------------------------------------------------------


def test_calibration_gap_is_zero_when_prediction_matches_observation():
    # 70%, 80%, 50% predicted; recovered, not recovered, recovered.
    # mean predicted = 66.66..%, observed = 66.66..%, gap = 0 pp.
    # Repeated four times so the exact ratio is preserved while clearing the
    # minimum sample threshold.
    specs = [(7_000, True), (8_000, False), (5_000, True)] * 4
    result = calibration(_observations(specs))
    assert result["calibration_observations"] == 12
    assert result["sufficient_observations"] is True
    assert result["mean_predicted_probability_bps"] == 6_667
    assert result["observed_recovery_rate_bps"] == 6_667
    assert result["calibration_gap_bps"] == 0


def test_negative_gap_means_observed_recovery_below_prediction():
    specs = _padded([(7_000, True), (7_000, False)])
    result = calibration(_observations(specs))
    assert result["mean_predicted_probability_bps"] == 7_000
    assert result["observed_recovery_rate_bps"] == 5_000
    assert result["calibration_gap_bps"] == -2_000


def test_positive_gap_means_observed_recovery_above_prediction():
    specs = [(3_000, True)] * 9 + [(3_000, False)]
    result = calibration(_observations(specs))
    assert result["observed_recovery_rate_bps"] == 9_000
    assert result["calibration_gap_bps"] == 6_000


def test_zero_observations_reports_insufficient_and_no_numbers():
    result = calibration([])
    assert result["calibration_observations"] == 0
    assert result["status"] == INSUFFICIENT_OBSERVATIONS
    assert result["observed_recovery_rate_bps"] is None
    assert result["calibration_gap_bps"] is None
    assert result["mean_predicted_probability_bps"] is None


def test_fewer_than_minimum_observations_withholds_observed_performance():
    specs = [(6_000, True)] * (MIN_OBSERVATIONS - 2) + [(6_000, False)]
    result = calibration(_observations(specs))
    assert result["sufficient_observations"] is False
    assert result["status"] == INSUFFICIENT_OBSERVATIONS
    assert result["status_detail"] == BELOW_THRESHOLD
    assert result["observed_recovery_rate_bps"] is None
    assert result["calibration_gap_bps"] is None
    # The prediction is a model estimate and does not depend on sample size.
    assert result["mean_predicted_probability_bps"] == 6_000


def test_exactly_minimum_observations_is_sufficient():
    specs = [(6_000, True)] * (MIN_OBSERVATIONS - 1) + [(6_000, False)]
    result = calibration(_observations(specs))
    assert result["calibration_observations"] == MIN_OBSERVATIONS
    assert result["sufficient_observations"] is True
    assert result["status"] == "OBSERVED"
    assert result["observed_recovery_rate_bps"] == 9_000


def test_more_than_minimum_observations_is_sufficient():
    specs = [(6_000, True)] * (MIN_OBSERVATIONS + 4) + [(6_000, False)]
    result = calibration(_observations(specs))
    assert result["sufficient_observations"] is True
    assert result["calibration_observations"] == MIN_OBSERVATIONS + 5


def test_unsettled_observations_never_enter_a_statistic():
    terminal = _observations([(5_000, True)] * 9 + [(5_000, False)])
    unsettled = [
        _observation(event_id=f"evt_pending_{index}", recovered=None)
        for index in range(50)
    ]
    result = calibration(terminal + unsettled)
    assert result["calibration_observations"] == MIN_OBSERVATIONS
    assert result["total_observations"] == MIN_OBSERVATIONS + 50
    assert result["observed_recovery_rate_bps"] == 9_000


# ---------------------------------------------------------------------------
# The censoring guard: positive evidence alone is not a recovery rate.
# ---------------------------------------------------------------------------


def test_verified_recoveries_alone_never_produce_a_recovery_rate():
    """Ten recoveries and no authoritative negative outcome is not 100%."""
    result = calibration(_observations([(6_000, True)] * MIN_OBSERVATIONS))
    assert result["verified_recoveries"] == MIN_OBSERVATIONS
    assert result["recovered_observations"] == MIN_OBSERVATIONS
    assert result["not_recovered_observations"] == 0
    assert result["has_terminal_negative_evidence"] is False
    assert result["sufficient_observations"] is False
    assert result["status"] == INSUFFICIENT_OBSERVATIONS
    assert result["status_detail"] == POSITIVE_EVIDENCE_ONLY
    assert result["observed_recovery_rate_bps"] is None
    assert result["calibration_gap_bps"] is None


def test_a_large_positive_only_population_is_still_not_a_rate():
    result = calibration(_observations([(6_000, True)] * 500))
    assert result["verified_recoveries"] == 500
    assert result["observed_recovery_rate_bps"] is None
    assert result["status_detail"] == POSITIVE_EVIDENCE_ONLY


def test_verified_recovery_evidence_stays_visible_without_calibration():
    """Positive evidence must not be hidden merely because no rate exists."""
    result = calibration(_observations([(6_000, True)] * 12))
    assert result["verified_recoveries"] == 12
    assert result["outcome_counts"]["RECOVERED"] == 12
    assert result["outcome_counts"]["NOT_RECOVERED"] == 0


def test_nine_recovered_and_one_not_recovered_is_ninety_percent():
    result = calibration(_observations([(5_000, True)] * 9 + [(5_000, False)]))
    assert result["calibration_observations"] == 10
    assert result["observed_recovery_rate_bps"] == 9_000
    assert result["status"] == "OBSERVED"


def test_seven_recovered_and_three_not_recovered_is_seventy_percent():
    result = calibration(_observations([(5_000, True)] * 7 + [(5_000, False)] * 3))
    assert result["observed_recovery_rate_bps"] == 7_000


def test_ten_recovered_and_ten_not_recovered_is_fifty_percent():
    result = calibration(_observations([(5_000, True)] * 10 + [(5_000, False)] * 10))
    assert result["calibration_observations"] == 20
    assert result["observed_recovery_rate_bps"] == 5_000
    assert result["calibration_gap_bps"] == 0


def test_predicted_seventy_observed_sixty_five_is_minus_five_points():
    # 13 recovered of 20 terminal outcomes = 65%, predicted 70%.
    specs = [(7_000, True)] * 13 + [(7_000, False)] * 7
    result = calibration(_observations(specs))
    assert result["mean_predicted_probability_bps"] == 7_000
    assert result["observed_recovery_rate_bps"] == 6_500
    assert result["calibration_gap_bps"] == -500


def test_calibration_is_deterministic_for_the_same_observations():
    observations = _observations(_padded([(7_000, True), (4_000, False)]))
    assert calibration(observations) == calibration(list(reversed(observations)))


# ---------------------------------------------------------------------------
# Intervention aggregation
# ---------------------------------------------------------------------------


def test_intervention_aggregation_groups_and_orders_deterministically():
    observations = [
        _observation(event_id="a", intervention="payment_link"),
        _observation(event_id="b", intervention="reminder"),
        _observation(event_id="c", intervention="alternate_method_prompt"),
    ]
    rows = intervention_performance(observations)
    assert [row["key"] for row in rows] == [
        "alternate_method_prompt",
        "payment_link",
        "reminder",
    ]
    assert all(row["attempts"] == 1 for row in rows)


def test_intervention_metrics_report_predicted_observed_gap_and_amounts():
    observations = [
        _observation(
            event_id=f"evt_{index}",
            predicted_bps=8_000,
            recovered=index % 2 == 0,
            recovered_amount_paise=100_000,
        )
        for index in range(MIN_OBSERVATIONS)
    ]
    row = intervention_performance(observations)[0]
    assert row["key"] == "payment_link"
    assert row["attempts"] == MIN_OBSERVATIONS
    assert row["mean_predicted_probability_bps"] == 8_000
    assert row["observed_recovery_rate_bps"] == 5_000
    assert row["calibration_gap_bps"] == -3_000
    assert row["recovered_observations"] == MIN_OBSERVATIONS // 2
    assert row["average_recovered_amount_paise"] == 100_000
    assert row["total_recovered_amount_paise"] == 100_000 * (MIN_OBSERVATIONS // 2)


def test_intervention_with_insufficient_observations_reports_no_observed_rate():
    row = intervention_performance([_observation()])[0]
    assert row["status"] == INSUFFICIENT_OBSERVATIONS
    assert row["observed_recovery_rate_bps"] is None
    assert row["calibration_gap_bps"] is None
    assert row["mean_predicted_probability_bps"] == 5_000


def test_missing_recovered_amount_is_excluded_from_the_average_not_zeroed():
    observations = [
        _observation(event_id="a", recovered_amount_paise=200_000),
        _observation(event_id="b", recovered_amount_paise=None),
    ]
    row = intervention_performance(observations)[0]
    assert row["observations_with_recovered_amount"] == 1
    assert row["average_recovered_amount_paise"] == 200_000


def test_attempts_count_every_execution_even_when_ineligible():
    observations = [
        _observation(event_id="a"),
        _observation(event_id="b", recovered=None),
    ]
    row = intervention_performance(observations)[0]
    assert row["attempts"] == 2
    assert row["calibration_observations"] == 1


# ---------------------------------------------------------------------------
# Segment aggregation
# ---------------------------------------------------------------------------


def test_segment_aggregation_covers_the_three_clean_dimensions():
    observations = [
        _observation(event_id="a", payment_method="upi", bank="HDFC"),
        _observation(
            event_id="b",
            payment_method="card",
            bank="ICICI",
            failure_reason="insufficient_funds",
        ),
    ]
    segments = segment_performance(observations)
    assert sorted(segments) == ["bank", "failure_reason", "payment_method"]
    assert [row["key"] for row in segments["payment_method"]] == ["card", "upi"]
    assert [row["key"] for row in segments["bank"]] == ["HDFC", "ICICI"]
    assert [row["key"] for row in segments["failure_reason"]] == [
        "bank_timeout",
        "insufficient_funds",
    ]


def test_segment_threshold_is_applied_per_segment():
    observations = [
        _observation(
            event_id=f"h{index}",
            bank="HDFC",
            predicted_bps=4_000,
            # One authoritative negative outcome, so this segment is a real
            # binary population rather than positive-only evidence.
            recovered=index < MIN_OBSERVATIONS - 1,
        )
        for index in range(MIN_OBSERVATIONS)
    ] + [_observation(event_id="i0", bank="ICICI")]
    rows = {row["key"]: row for row in segment_performance(observations)["bank"]}
    assert rows["HDFC"]["sufficient_observations"] is True
    assert rows["HDFC"]["observed_recovery_rate_bps"] == 9_000
    assert rows["HDFC"]["calibration_gap_bps"] == 5_000
    assert rows["ICICI"]["status"] == INSUFFICIENT_OBSERVATIONS
    assert rows["ICICI"]["observed_recovery_rate_bps"] is None


def test_empty_observations_produce_empty_segments():
    segments = segment_performance([])
    assert segments == {"payment_method": [], "bank": [], "failure_reason": []}


def test_segment_ordering_is_deterministic():
    observations = [
        _observation(event_id=str(index), bank=bank)
        for index, bank in enumerate(["SBI", "AXIS", "HDFC", "AXIS"])
    ]
    first = [row["key"] for row in segment_performance(observations)["bank"]]
    shuffled = list(reversed(observations))
    second = [row["key"] for row in segment_performance(shuffled)["bank"]]
    assert first == second == ["AXIS", "HDFC", "SBI"]


# ---------------------------------------------------------------------------
# Expected vs realized value
# ---------------------------------------------------------------------------


def test_expected_vs_realized_compares_only_complete_pairs():
    observations = [
        _observation(
            event_id="a",
            expected_recovered_value_paise=50_000,
            recovered_amount_paise=100_000,
        ),
        _observation(
            event_id="b",
            expected_recovered_value_paise=None,
            recovered_amount_paise=100_000,
        ),
        _observation(
            event_id="c",
            expected_recovered_value_paise=50_000,
            recovered_amount_paise=None,
        ),
        _observation(event_id="d", recovered=None),
    ]
    result = expected_vs_realized(observations)
    assert result["compared_observations"] == 1
    assert result["expected_recovered_value_paise"] == 50_000
    assert result["realized_recovered_amount_paise"] == 100_000
    assert result["sufficient_observations"] is False


def test_expected_vs_realized_is_empty_without_verified_recoveries():
    result = expected_vs_realized(
        [_observation(event_id="a", recovered=None)]
    )
    assert result["compared_observations"] == 0
    assert result["realized_recovered_amount_paise"] == 0


@pytest.mark.parametrize("count", [0, 1, MIN_OBSERVATIONS - 1])
def test_no_conclusion_is_drawn_below_the_threshold(count):
    result = calibration(_observations([(5_000, True)] * count))
    assert result["status"] == INSUFFICIENT_OBSERVATIONS

"""Phase 22 calibration and performance analytics tests.

The aggregation functions are pure, so they are exercised directly on
constructed observations. That lets the calibration arithmetic be pinned
exactly — including the sign of the gap, which must never be reversed.
"""

from __future__ import annotations

import pytest

from app.outcome_feedback import (
    OUTCOME_PENDING,
    OUTCOME_RECOVERED,
    REASON_AWAITING_OUTCOME,
    REASON_ELIGIBLE,
    FeedbackObservation,
)
from app.recovery_intelligence import (
    INSUFFICIENT_OBSERVATIONS,
    MIN_OBSERVATIONS,
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
    eligible: bool = True,
    recovered_amount_paise: int | None = 100_000,
    expected_recovered_value_paise: int | None = 50_000,
    payment_method: str = "upi",
    bank: str = "HDFC",
    failure_reason: str = "bank_timeout",
) -> FeedbackObservation:
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
        outcome=OUTCOME_RECOVERED if recovered else OUTCOME_PENDING,
        eligible=eligible,
        reason=REASON_ELIGIBLE if eligible else REASON_AWAITING_OUTCOME,
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
    assert result["eligible_observations"] == 12
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
    specs = _padded([(3_000, True)])
    result = calibration(_observations(specs))
    assert result["observed_recovery_rate_bps"] == 10_000
    assert result["calibration_gap_bps"] == 7_000


def test_zero_observations_reports_insufficient_and_no_numbers():
    result = calibration([])
    assert result["eligible_observations"] == 0
    assert result["status"] == INSUFFICIENT_OBSERVATIONS
    assert result["observed_recovery_rate_bps"] is None
    assert result["calibration_gap_bps"] is None
    assert result["mean_predicted_probability_bps"] is None


def test_fewer_than_minimum_observations_withholds_observed_performance():
    result = calibration(_observations([(6_000, True)] * (MIN_OBSERVATIONS - 1)))
    assert result["sufficient_observations"] is False
    assert result["status"] == INSUFFICIENT_OBSERVATIONS
    assert result["observed_recovery_rate_bps"] is None
    assert result["calibration_gap_bps"] is None
    # The prediction is a model estimate and does not depend on sample size.
    assert result["mean_predicted_probability_bps"] == 6_000


def test_exactly_minimum_observations_is_sufficient():
    result = calibration(_observations([(6_000, True)] * MIN_OBSERVATIONS))
    assert result["sufficient_observations"] is True
    assert result["status"] == "OBSERVED"
    assert result["observed_recovery_rate_bps"] == 10_000


def test_more_than_minimum_observations_is_sufficient():
    result = calibration(_observations([(6_000, True)] * (MIN_OBSERVATIONS + 5)))
    assert result["sufficient_observations"] is True
    assert result["eligible_observations"] == MIN_OBSERVATIONS + 5


def test_ineligible_observations_never_enter_a_statistic():
    eligible = _observations([(5_000, True)] * MIN_OBSERVATIONS)
    ineligible = [
        _observation(event_id="evt_pending", recovered=None, eligible=False)
        for _ in range(50)
    ]
    result = calibration(eligible + ineligible)
    assert result["eligible_observations"] == MIN_OBSERVATIONS
    assert result["total_observations"] == MIN_OBSERVATIONS + 50
    assert result["observed_recovery_rate_bps"] == 10_000


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
        _observation(event_id="b", recovered=None, eligible=False),
    ]
    row = intervention_performance(observations)[0]
    assert row["attempts"] == 2
    assert row["eligible_observations"] == 1


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
        _observation(event_id=f"h{index}", bank="HDFC", predicted_bps=4_000)
        for index in range(MIN_OBSERVATIONS)
    ] + [_observation(event_id="i0", bank="ICICI")]
    rows = {row["key"]: row for row in segment_performance(observations)["bank"]}
    assert rows["HDFC"]["sufficient_observations"] is True
    assert rows["HDFC"]["calibration_gap_bps"] == 6_000
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
        _observation(event_id="d", recovered=None, eligible=False),
    ]
    result = expected_vs_realized(observations)
    assert result["compared_observations"] == 1
    assert result["expected_recovered_value_paise"] == 50_000
    assert result["realized_recovered_amount_paise"] == 100_000
    assert result["sufficient_observations"] is False


def test_expected_vs_realized_is_empty_without_verified_recoveries():
    result = expected_vs_realized(
        [_observation(event_id="a", recovered=None, eligible=False)]
    )
    assert result["compared_observations"] == 0
    assert result["realized_recovered_amount_paise"] == 0


@pytest.mark.parametrize("count", [0, 1, MIN_OBSERVATIONS - 1])
def test_no_conclusion_is_drawn_below_the_threshold(count):
    result = calibration(_observations([(5_000, True)] * count))
    assert result["status"] == INSUFFICIENT_OBSERVATIONS

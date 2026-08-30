"""Recovery Intelligence analytics (Phase 22) — honest measurement only.

Aggregates the Phase 22 feedback observations into:

    overall calibration     predicted recovery vs observed recovery
    intervention performance    the same comparison, per intervention
    segment signals             the same comparison, per clean event dimension
    expected vs realized value  the optimizer's own estimate vs trusted amounts

WHAT THIS MODULE IS NOT
-----------------------
It is not a learning loop. Nothing computed here is written back into the
estimator, the optimizer, the policy engine or any decision. It produces
evidence for a human to read, and stops there. It performs no I/O of its own
beyond the read the caller hands it, uses no randomness, and reads no clock.

ARITHMETIC
----------
Probabilities stay in the integer basis points the optimizer persisted, and
every mean is computed as an exact rational before being rounded once for
display. The calibration gap is computed from the exact difference rather than
from the two rounded means, so a genuinely perfect calibration reports exactly
0 instead of a rounding artefact.

SMALL SAMPLES ARE NOT EVIDENCE
------------------------------
Below MIN_OBSERVATIONS no observed rate, gap or conclusion is reported at all.
The predicted probability may still be shown — it is a model estimate and does
not depend on sample size — but the observed side reads INSUFFICIENT
OBSERVATIONS.
"""

from __future__ import annotations

from fractions import Fraction
from typing import Any, Mapping, Sequence

from .economics import PROBABILITY_SCALE
from .outcome_feedback import (
    DEFAULT_SCAN_LIMIT,
    FeedbackObservation,
    build_observations,
    eligible_observations,
    ineligibility_counts,
)

# The minimum number of eligible observations before any observed rate, gap or
# performance conclusion is reported. Deterministic, centralized, documented in
# docs/RECOVERY_INTELLIGENCE.md, and tested.
MIN_OBSERVATIONS: int = 10

# The single honest state for "we do not have enough evidence to say".
INSUFFICIENT_OBSERVATIONS = "INSUFFICIENT_OBSERVATIONS"

SEGMENT_DIMENSIONS: tuple[str, ...] = ("payment_method", "bank", "failure_reason")


def _round_half_up(value: Fraction) -> int:
    """Round an exact rational to the nearest integer, halves away from zero.

    Deterministic and sign-symmetric, unlike Python's banker's rounding, so a
    gap of -0.5 bps and +0.5 bps are treated the same way.
    """
    if value >= 0:
        return int((value * 2 + 1) // 2)
    return -int(((-value) * 2 + 1) // 2)


def _mean_predicted_bps(
    observations: Sequence[FeedbackObservation],
) -> Fraction | None:
    """Exact mean of the predictions RecoveryOS actually used, or None."""
    predictions = [
        observation.predicted_probability_bps
        for observation in observations
        if observation.predicted_probability_bps is not None
    ]
    if not predictions:
        return None
    return Fraction(sum(predictions), len(predictions))


def calibration(observations: Sequence[FeedbackObservation]) -> dict[str, Any]:
    """Compare predicted recovery probability with observed recovery rate.

        observed_recovery_rate = recovered observations / eligible observations
        calibration_gap        = observed_recovery_rate - mean_predicted

    The sign is never reversed: a NEGATIVE gap means observed recovery came in
    BELOW what RecoveryOS predicted.

    ``mean_predicted_probability_bps`` is reported whenever any eligible
    observation carries a prediction, because it is a model estimate. The
    observed rate and the gap are withheld below the sample threshold.
    """
    eligible = eligible_observations(observations)
    count = len(eligible)
    recovered = sum(1 for observation in eligible if observation.recovered)
    mean_predicted = _mean_predicted_bps(eligible)
    sufficient = count >= MIN_OBSERVATIONS

    payload: dict[str, Any] = {
        "eligible_observations": count,
        "recovered_observations": recovered,
        "total_observations": len(observations),
        "minimum_observations": MIN_OBSERVATIONS,
        "sufficient_observations": sufficient,
        "mean_predicted_probability_bps": (
            _round_half_up(mean_predicted) if mean_predicted is not None else None
        ),
        "observed_recovery_rate_bps": None,
        "calibration_gap_bps": None,
        "status": INSUFFICIENT_OBSERVATIONS if not sufficient else "OBSERVED",
    }
    if not sufficient or mean_predicted is None:
        return payload

    observed = Fraction(recovered * PROBABILITY_SCALE, count)
    payload["observed_recovery_rate_bps"] = _round_half_up(observed)
    # Computed from the exact difference, never from the rounded components.
    payload["calibration_gap_bps"] = _round_half_up(observed - mean_predicted)
    return payload


def _group_metrics(
    key: str, observations: Sequence[FeedbackObservation], attempts: int
) -> dict[str, Any]:
    """Calibration plus value metrics for one intervention or segment."""
    stats = calibration(observations)
    eligible = eligible_observations(observations)
    amounts = [
        observation.recovered_amount_paise
        for observation in eligible
        if observation.recovered and observation.recovered_amount_paise is not None
    ]
    expected = [
        observation.expected_recovered_value_paise
        for observation in eligible
        if observation.expected_recovered_value_paise is not None
    ]
    return {
        "key": key,
        "attempts": attempts,
        "eligible_observations": stats["eligible_observations"],
        "recovered_observations": stats["recovered_observations"],
        "sufficient_observations": stats["sufficient_observations"],
        "status": stats["status"],
        "mean_predicted_probability_bps": stats["mean_predicted_probability_bps"],
        "observed_recovery_rate_bps": stats["observed_recovery_rate_bps"],
        "calibration_gap_bps": stats["calibration_gap_bps"],
        # Averages over the verified amounts only. An observation whose
        # provider reported no amount is excluded rather than counted as zero.
        "observations_with_recovered_amount": len(amounts),
        "average_recovered_amount_paise": (
            _round_half_up(Fraction(sum(amounts), len(amounts))) if amounts else None
        ),
        "total_recovered_amount_paise": sum(amounts) if amounts else 0,
        "average_expected_recovered_value_paise": (
            _round_half_up(Fraction(sum(expected), len(expected)))
            if expected
            else None
        ),
    }


def intervention_performance(
    observations: Sequence[FeedbackObservation],
) -> list[dict[str, Any]]:
    """Per-intervention metrics for the interventions actually executed.

    The rows come from persisted executions, so no intervention list is
    hardcoded and an intervention that was never executed simply does not
    appear. Ordered by intervention name, which is a total order.
    """
    grouped: dict[str, list[FeedbackObservation]] = {}
    for observation in observations:
        grouped.setdefault(observation.intervention, []).append(observation)
    return [
        _group_metrics(intervention, rows, attempts=len(rows))
        for intervention, rows in sorted(grouped.items())
    ]


def segment_performance(
    observations: Sequence[FeedbackObservation],
) -> dict[str, list[dict[str, Any]]]:
    """Per-segment metrics across the clean persisted event dimensions.

    Only ``payment_method``, ``bank`` and ``failure_reason`` are grouped: they
    are locked columns on the event contract. This is deliberately not a
    general segmentation engine.
    """
    result: dict[str, list[dict[str, Any]]] = {}
    for dimension in SEGMENT_DIMENSIONS:
        grouped: dict[str, list[FeedbackObservation]] = {}
        for observation in observations:
            value = getattr(observation, dimension)
            if value is None:
                continue
            grouped.setdefault(str(value), []).append(observation)
        result[dimension] = [
            _group_metrics(value, rows, attempts=len(rows))
            for value, rows in sorted(grouped.items())
        ]
    return result


def expected_vs_realized(
    observations: Sequence[FeedbackObservation],
) -> dict[str, Any]:
    """Compare the optimizer's expected recovered value with trusted amounts.

    Only eligible, verified recoveries that carry BOTH a persisted expected
    recovered value and a provider-reported amount are compared, because a
    comparison missing either half is not a comparison. This is not profit and
    it is not revenue uplift: it is the model's own estimate placed next to
    what the provider actually reported.
    """
    pairs = [
        (
            observation.expected_recovered_value_paise,
            observation.recovered_amount_paise,
        )
        for observation in eligible_observations(observations)
        if observation.recovered
        and observation.expected_recovered_value_paise is not None
        and observation.recovered_amount_paise is not None
    ]
    return {
        "compared_observations": len(pairs),
        "expected_recovered_value_paise": sum(expected for expected, _ in pairs),
        "realized_recovered_amount_paise": sum(realized for _, realized in pairs),
        "minimum_observations": MIN_OBSERVATIONS,
        "sufficient_observations": len(pairs) >= MIN_OBSERVATIONS,
    }


def build_recovery_intelligence(
    conn, *, scan_limit: int = DEFAULT_SCAN_LIMIT
) -> dict[str, Any]:
    """Assemble the full Recovery Intelligence payload from persisted state.

    Read-only end to end: it projects observations, aggregates them, and
    returns them. It writes nothing and decides nothing.
    """
    observations = build_observations(conn, scan_limit=scan_limit)
    return {
        "calibration": calibration(observations),
        "interventions": intervention_performance(observations),
        "segments": segment_performance(observations),
        "expected_vs_realized": expected_vs_realized(observations),
        "evidence": {
            "observations": len(observations),
            "ineligible_reasons": ineligibility_counts(observations),
            "scan_limit": scan_limit,
        },
        "methodology": {
            "prediction_source": "optimizer_decisions",
            "execution_source": "execution_outcomes",
            "recovery_source": "webhook_recovery_outcomes",
            "correlation_key": "payment_link_id",
            "minimum_observations": MIN_OBSERVATIONS,
            "operational_world_only": True,
        },
    }


def observation_rows(
    observations: Sequence[FeedbackObservation],
) -> list[Mapping[str, Any]]:
    """Serialize observations so every aggregate stays traceable to evidence."""
    return [observation.to_dict() for observation in observations]

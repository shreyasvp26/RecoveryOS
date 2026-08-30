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

THE DENOMINATOR IS TERMINAL OUTCOMES, NOT RECOVERIES
----------------------------------------------------
A recovery rate may only be computed over observations whose payment question
is SETTLED — RECOVERED or NOT_RECOVERED — and which carry the prediction that
drove them. Dividing recoveries by recoveries is 100% by construction and says
nothing about the estimator.

Because the current provider contract yields authoritative POSITIVE evidence
(``payment_link.paid``) and no authoritative negative outcome, that terminal
population is normally empty, and this module reports exactly that. Verified
recoveries are still counted and reported separately, because they are real
evidence — they are simply not a rate.

SMALL SAMPLES ARE NOT EVIDENCE
------------------------------
Below MIN_OBSERVATIONS terminal outcomes, no observed rate, gap or conclusion
is reported at all. The predicted probability may still be shown — it is a
model estimate and does not depend on sample size — but the observed side
reads INSUFFICIENT OBSERVATIONS.
"""

from __future__ import annotations

from fractions import Fraction
from typing import Any, Mapping, Sequence

from .economics import PROBABILITY_SCALE
from .outcome_feedback import (
    DEFAULT_OBSERVATION_LIMIT,
    FeedbackObservation,
    build_observation_population,
    calibration_observations,
    ineligibility_counts,
    outcome_counts,
    verified_recoveries,
)

# The minimum number of CALIBRATION-ELIGIBLE TERMINAL OUTCOMES before any
# observed rate, gap or performance conclusion is reported. It is a threshold
# on settled binary outcomes, not on verified recoveries: ten recoveries and no
# authoritative negative outcome is a zero-observation sample for this purpose.
# Deterministic, centralized, documented in docs/RECOVERY_INTELLIGENCE.md,
# and tested.
MIN_OBSERVATIONS: int = 10

# The single honest state for "we do not have enough evidence to say".
INSUFFICIENT_OBSERVATIONS = "INSUFFICIENT_OBSERVATIONS"
OBSERVED = "OBSERVED"

# Why a rate could not be computed. Stated explicitly so a reader is never left
# to guess whether the number is missing or merely small.
NO_TERMINAL_OUTCOMES = (
    "no authoritative terminal binary outcome exists yet. The provider "
    "contract currently confirms payment but never confirms non-payment, so a "
    "recovery rate cannot be computed from the available evidence."
)
BELOW_THRESHOLD = (
    "fewer than the minimum number of authoritative terminal binary outcomes "
    "required before an observed rate is reported."
)
# The censoring guard. Verified recoveries arrive as positive evidence only:
# absence of a paid webhook is not evidence of non-payment. A population
# containing no authoritative NOT_RECOVERED is therefore not a sample of
# outcomes, it is a list of the successes we happened to see — and dividing it
# by itself yields 100% no matter how good or bad the estimator is.
POSITIVE_EVIDENCE_ONLY = (
    "every observed terminal outcome is a verified recovery and no "
    "authoritative NOT_RECOVERED outcome exists. Absence of a payment "
    "confirmation is not evidence of non-payment, so this population is "
    "positive-only and cannot yield a recovery rate."
)

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

        observed_recovery_rate = RECOVERED / (RECOVERED + NOT_RECOVERED)
        calibration_gap        = observed_recovery_rate - mean_predicted

    The denominator is the calibration-eligible TERMINAL population, so a
    verified recovery with no counterpart negative outcome cannot inflate the
    rate to 100%. PENDING and UNKNOWN never enter either side.

    The sign is never reversed: a NEGATIVE gap means observed recovery came in
    BELOW what RecoveryOS predicted.

    ``verified_recoveries`` is reported separately and unconditionally: real
    positive evidence stays visible even when no rate can be computed.
    """
    terminal = calibration_observations(observations)
    count = len(terminal)
    recovered = sum(1 for observation in terminal if observation.recovered)
    not_recovered = count - recovered
    mean_predicted = _mean_predicted_bps(terminal)
    # BOTH conditions are required. Enough outcomes is not enough on its own:
    # a positive-only population is censored, not small.
    sufficient = count >= MIN_OBSERVATIONS and not_recovered > 0

    payload: dict[str, Any] = {
        # The calibration population: settled outcomes carrying a prediction.
        "calibration_observations": count,
        "recovered_observations": recovered,
        "not_recovered_observations": not_recovered,
        "has_terminal_negative_evidence": not_recovered > 0,
        # Authoritative positive evidence, independent of calibration.
        "verified_recoveries": len(verified_recoveries(observations)),
        "total_observations": len(observations),
        "outcome_counts": outcome_counts(observations),
        "minimum_observations": MIN_OBSERVATIONS,
        "sufficient_observations": sufficient,
        "mean_predicted_probability_bps": (
            _round_half_up(mean_predicted) if mean_predicted is not None else None
        ),
        "observed_recovery_rate_bps": None,
        "calibration_gap_bps": None,
        "status": OBSERVED if sufficient else INSUFFICIENT_OBSERVATIONS,
        "status_detail": None,
    }
    if not sufficient or mean_predicted is None:
        payload["status"] = INSUFFICIENT_OBSERVATIONS
        if count == 0:
            payload["status_detail"] = NO_TERMINAL_OUTCOMES
        elif not_recovered == 0:
            payload["status_detail"] = POSITIVE_EVIDENCE_ONLY
        else:
            payload["status_detail"] = BELOW_THRESHOLD
        return payload

    observed = Fraction(recovered * PROBABILITY_SCALE, count)
    payload["observed_recovery_rate_bps"] = _round_half_up(observed)
    # Computed from the exact difference, never from the rounded components.
    payload["calibration_gap_bps"] = _round_half_up(observed - mean_predicted)
    return payload


def _group_metrics(
    key: str, observations: Sequence[FeedbackObservation], attempts: int
) -> dict[str, Any]:
    """Calibration plus value metrics for one intervention or segment.

    The same eligibility rules apply as to the overall figure, computed only
    within this group: samples are never borrowed from another intervention or
    another segment, and an observed rate is never derived from recoveries
    alone.
    """
    stats = calibration(observations)
    recoveries = verified_recoveries(observations)
    amounts = [
        observation.recovered_amount_paise
        for observation in recoveries
        if observation.recovered_amount_paise is not None
    ]
    expected = [
        observation.expected_recovered_value_paise
        for observation in calibration_observations(observations)
        if observation.expected_recovered_value_paise is not None
    ]
    return {
        "key": key,
        "attempts": attempts,
        "calibration_observations": stats["calibration_observations"],
        "recovered_observations": stats["recovered_observations"],
        "not_recovered_observations": stats["not_recovered_observations"],
        "verified_recoveries": stats["verified_recoveries"],
        "sufficient_observations": stats["sufficient_observations"],
        "status": stats["status"],
        "status_detail": stats["status_detail"],
        "mean_predicted_probability_bps": stats["mean_predicted_probability_bps"],
        "observed_recovery_rate_bps": stats["observed_recovery_rate_bps"],
        "calibration_gap_bps": stats["calibration_gap_bps"],
        # Averages over the verified recovered amounts. An observation whose
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

    Only verified recoveries that carry BOTH a persisted expected recovered
    value and a provider-reported amount are compared, because a comparison
    missing either half is not a comparison. This is not profit and it is not
    revenue uplift: it is the model's own estimate placed next to what the
    provider actually reported.

    This deliberately uses verified recovery evidence rather than the
    calibration population: comparing money that actually moved does not
    require a settled negative outcome to exist.
    """
    pairs = [
        (
            observation.expected_recovered_value_paise,
            observation.recovered_amount_paise,
        )
        for observation in verified_recoveries(observations)
        if observation.expected_recovered_value_paise is not None
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
    conn, *, limit: int = DEFAULT_OBSERVATION_LIMIT
) -> dict[str, Any]:
    """Assemble the full Recovery Intelligence payload from persisted state.

    Read-only end to end: it projects observations, aggregates them, and
    returns them. It writes nothing and decides nothing.

    The population it covers is reported alongside the figures, so a bounded
    read can never be mistaken for a complete history.
    """
    population = build_observation_population(conn, limit=limit)
    observations = population.observations
    return {
        "calibration": calibration(observations),
        "interventions": intervention_performance(observations),
        "segments": segment_performance(observations),
        "expected_vs_realized": expected_vs_realized(observations),
        "evidence": {
            "observations": len(observations),
            "ineligible_reasons": ineligibility_counts(observations),
            # Whether these figures describe every recorded execution or a
            # deterministic prefix of them.
            "population": population.to_dict(),
        },
        "methodology": {
            "prediction_source": "optimizer_decisions",
            "execution_source": "execution_outcomes",
            "recovery_source": "webhook_recovery_outcomes",
            "correlation_key": "payment_link_id",
            # Stated in the payload so a consumer can never mistake the
            # verified recovery count for a rate denominator.
            "calibration_denominator": "RECOVERED + NOT_RECOVERED",
            "minimum_observations": MIN_OBSERVATIONS,
            "operational_world_only": True,
        },
    }


def observation_rows(
    observations: Sequence[FeedbackObservation],
) -> list[Mapping[str, Any]]:
    """Serialize observations so every aggregate stays traceable to evidence."""
    return [observation.to_dict() for observation in observations]

"""Phase 9 benchmark metrics over collected strategy results.

Every metric is a pure function of its inputs; no metric mutates state and no
metric consults hidden ground truth. Where the benchmark repository defines no
canonical metric, the Phase 9 methodology defines the denominator explicitly
and documents it (see docs/BENCHMARK.md).

False-intervention rate: the repository defines NO canonical false-intervention
threshold, so the Phase 9 methodology reports METRIC DEFINITION AMBIGUITY for
that metric instead of inventing a threshold. The raw foundation any such
metric would need (per-event attempted/recovered flags) is carried on every
BenchmarkEventResult.
"""

from __future__ import annotations

from typing import Sequence

from .benchmark import (
    BenchmarkEventResult,
    BenchmarkReport,
    BenchmarkRunResult,
    BenchmarkStrategyResult,
    STRATEGY_NO_ACTION,
    STRATEGY_RECOVERY_OS,
)
from .models import PaymentEvent

METRIC_DEFINITION_AMBIGUITY = (
    "METRIC DEFINITION AMBIGUITY: the false-intervention rate is not computed "
    "because no canonical false-intervention threshold is defined anywhere in "
    "the repository, so a threshold would be invented rather than measured. "
    "The raw per-event attempted/recovered foundation any such metric needs "
    "is available once a threshold is specified."
)


def strategy_result(
    run: BenchmarkRunResult, strategy: str
) -> BenchmarkStrategyResult:
    """Return a strategy's aggregate result from a run summary."""
    for result in run.strategy_results:
        if result.strategy == strategy:
            return result
    raise LookupError(f"no strategy result for {strategy!r}")


def recovered_revenue(result: BenchmarkStrategyResult) -> int:
    """Simulated recovered revenue in integer paise (Metric A)."""
    return result.recovered_amount_paise


def recovery_rate(result: BenchmarkStrategyResult) -> float:
    """Recovered events divided by the (shared) event count (Metric B).

    Denominator defined by the Phase 9 methodology: every strategy evaluates
    the same shared event set, and each event yields exactly one simulated
    outcome (attempted intervention or the modeled no_action baseline), so the
    denominator is the strategy's event_count.
    """
    return result.recovered_events / result.event_count


def intervention_count(result: BenchmarkStrategyResult) -> int:
    """Total interventions attempted by the strategy (Metric C)."""
    return result.interventions_attempted


def recovery_efficiency(
    result: BenchmarkStrategyResult,
) -> float | None:
    """Simulated recovered revenue per intervention attempted (Metric D).

    Returns None when zero interventions were attempted (never divides by
    zero). Efficiency aggregates over the shared event set.
    """
    if result.interventions_attempted == 0:
        return None
    return result.recovered_amount_paise / result.interventions_attempted


def amount_delta_paise(
    subject: BenchmarkStrategyResult, baseline: BenchmarkStrategyResult
) -> int:
    """subject recovered amount minus baseline recovered amount (paise)."""
    return subject.recovered_amount_paise - baseline.recovered_amount_paise


def incremental_over_no_action(run: BenchmarkRunResult) -> int:
    """RecoveryOS simulated recovered revenue over No Action (Metric E)."""
    return amount_delta_paise(
        strategy_result(run, STRATEGY_RECOVERY_OS),
        strategy_result(run, STRATEGY_NO_ACTION),
    )


def recoveryos_vs_naive_retry(run: BenchmarkRunResult) -> int:
    """RecoveryOS simulated recovered revenue over Naive Retry (Metric F)."""
    return amount_delta_paise(
        strategy_result(run, STRATEGY_RECOVERY_OS),
        strategy_result(run, "naive_retry"),
    )


def fraud_intervention_rate(
    event_results: Sequence[BenchmarkEventResult],
    events: Sequence[PaymentEvent],
) -> float | None:
    """Fraud interventions divided by fraud events (Metric G).

    An intervention on an event whose risk_flag is ``fraud_suspect`` counts as
    a fraud intervention. Returns None when there are no fraud events in the
    shared set (never divides by zero). RecoveryOS's desired value is 0; the
    metric is measured from records, never hardcoded.
    """
    fraud_event_ids = {
        event.event_id for event in events if event.risk_flag == "fraud_suspect"
    }
    denominator = len(fraud_event_ids)
    if denominator == 0:
        return None
    numerator = sum(
        1
        for record in event_results
        if record.attempted and record.event_id in fraud_event_ids
    )
    return numerator / denominator

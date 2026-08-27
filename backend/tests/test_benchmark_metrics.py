"""Phase 9 benchmark metrics tests.

Metrics must be pure functions that never divide by zero, never invent values,
and expose honest (possibly negative or zero) deltas. The false-intervention
rate is intentionally NOT computed because the repository defines no canonical
threshold; the raw per-event foundation that any such metric would need is
verified.
"""

from __future__ import annotations

import pytest

from app.benchmark import (
    STRATEGY_NO_ACTION,
    STRATEGY_NAIVE_RETRY,
    STRATEGY_RECOVERY_OS,
    run_benchmark,
)
from app.benchmark_metrics import (
    METRIC_DEFINITION_AMBIGUITY,
    amount_delta_paise,
    fraud_intervention_rate,
    incremental_over_no_action,
    intervention_count,
    recovery_efficiency,
    recovery_rate,
    recovered_revenue,
    recoveryos_vs_naive_retry,
    strategy_result,
)


def _report(seed: int = 42, count: int = 80):
    return run_benchmark(seed=seed, event_count=count)


def _events_with_fraud(report):
    return [event for event in report.events if event.risk_flag == "fraud_suspect"]


# ---------------------------------------------------------------------------
# Core metrics
# ---------------------------------------------------------------------------


def test_recovered_revenue_matches_aggregate() -> None:
    report = _report()
    no_action = strategy_result(report.run, STRATEGY_NO_ACTION)
    assert recovered_revenue(no_action) == no_action.recovered_amount_paise
    recoveryos = strategy_result(report.run, STRATEGY_RECOVERY_OS)
    assert recovered_revenue(recoveryos) == recoveryos.recovered_amount_paise


def test_recovery_rate_denominator_is_event_count() -> None:
    report = _report()
    for strategy in report.run.strategy_results:
        result = strategy_result(report.run, strategy.strategy)
        expected = result.recovered_events / result.event_count
        assert recovery_rate(result) == expected
        assert 0.0 <= recovery_rate(result) <= 1.0


def test_no_action_intervention_count_is_zero() -> None:
    report = _report()
    no_action = strategy_result(report.run, STRATEGY_NO_ACTION)
    assert intervention_count(no_action) == 0


def test_efficiency_is_none_when_no_interventions() -> None:
    report = _report()
    no_action = strategy_result(report.run, STRATEGY_NO_ACTION)
    assert no_action.interventions_attempted == 0
    assert recovery_efficiency(no_action) is None


def test_efficiency_is_finite_when_interventions_occur() -> None:
    report = _report()
    naive = strategy_result(report.run, STRATEGY_NAIVE_RETRY)
    assert naive.interventions_attempted > 0
    efficiency = recovery_efficiency(naive)
    assert efficiency is not None
    assert efficiency >= 0


# ---------------------------------------------------------------------------
# Deltas: honest, possibly negative or zero
# ---------------------------------------------------------------------------


def test_incremental_over_no_action_is_exact_delta() -> None:
    report = _report()
    recoveryos = strategy_result(report.run, STRATEGY_RECOVERY_OS)
    no_action = strategy_result(report.run, STRATEGY_NO_ACTION)
    expected = recoveryos.recovered_amount_paise - no_action.recovered_amount_paise
    assert incremental_over_no_action(report.run) == expected


def test_recoveryos_vs_naive_retry_is_exact_delta() -> None:
    report = _report()
    recoveryos = strategy_result(report.run, STRATEGY_RECOVERY_OS)
    naive = strategy_result(report.run, STRATEGY_NAIVE_RETRY)
    expected = recoveryos.recovered_amount_paise - naive.recovered_amount_paise
    assert recoveryos_vs_naive_retry(report.run) == expected


def test_delta_is_allowed_to_be_negative_or_zero() -> None:
    report = _report()
    delta = incremental_over_no_action(report.run)
    assert isinstance(delta, int)


def test_amount_delta_is_commutative() -> None:
    report = _report()
    no_action = strategy_result(report.run, STRATEGY_NO_ACTION)
    recoveryos = strategy_result(report.run, STRATEGY_RECOVERY_OS)
    assert amount_delta_paise(recoveryos, no_action) == -amount_delta_paise(
        no_action, recoveryos
    )


# ---------------------------------------------------------------------------
# Fraud intervention rate
# ---------------------------------------------------------------------------


def test_fraud_intervention_rate_is_zero_for_recoveryos() -> None:
    report = _report()
    rate = fraud_intervention_rate(
        report.event_results[STRATEGY_RECOVERY_OS], report.events
    )
    assert rate == 0.0
    assert fraud_intervention_rate(
        report.event_results[STRATEGY_NAIVE_RETRY], report.events
    ) == 0.0


def test_fraud_intervention_rate_is_none_when_no_fraud_events() -> None:
    rate = None
    for seed in range(1, 60):
        report = run_benchmark(seed=seed, event_count=30)
        if not _events_with_fraud(report):
            rate = fraud_intervention_rate(
                report.event_results[STRATEGY_NO_ACTION], report.events
            )
            break
    assert rate is None


# ---------------------------------------------------------------------------
# Metric-definition ambiguity: false-intervention rate foundation
# ---------------------------------------------------------------------------


def test_false_intervention_rate_is_reported_ambiguous_not_invented() -> None:
    assert "ambigu" in METRIC_DEFINITION_AMBIGUITY.lower()
    assert "threshold" in METRIC_DEFINITION_AMBIGUITY.lower()
    assert "foundation" in METRIC_DEFINITION_AMBIGUITY.lower()


def test_per_event_foundation_exists_for_false_intervention_metric() -> None:
    report = _report()
    for strategy in report.run.strategy_results:
        records = report.event_results[strategy.strategy]
        for record in records:
            assert "attempted" in record.to_dict()
            assert "recovered" in record.to_dict()
            assert "recovered_amount_paise" in record.to_dict()
            assert record.recovered == (record.recovered_amount_paise > 0)

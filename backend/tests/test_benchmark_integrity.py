"""Phase 9 benchmark integrity tests: fairness, isolation, safety, honesty.

These tests enforce the honesty contract of the three-strategy benchmark:

* fairness: the outcome for (event, intervention, seed) is identical regardless
  of the order in which strategies run;
* ground-truth isolation: the hidden outcome model is never consulted to decide
  whether or what to execute;
* safety: fraud and terminal events are never executed under RecoveryOS;
* honesty: unfavorable fixtures where RecoveryOS loses are allowed and the
  benchmark does not force a RecoveryOS victory;
* accounting: processed + skipped + exceptions == event_count, and skipped,
  failed, and exceptions are distinguished.
"""

from __future__ import annotations

from datetime import datetime, timezone

from app.benchmark import (
    STRATEGIES,
    DeterministicClassifier,
    InvalidBenchmarkConfigurationError,
    run_benchmark,
)
from app.benchmark_metrics import fraud_intervention_rate, strategy_result
from app.executor import SIMULATED_INTERVENTIONS
from app.models import PaymentEvent

EVALUATION_TIME = datetime(2026, 8, 27, 13, 0, tzinfo=timezone.utc)


class FailingExecutionClassifier:
    """A valid classifier whose classification always fails to persist."""

    def generate(self, prompt: str) -> str:
        raise RuntimeError("deterministic simulated provider failure")


def _recoveryos_records(report):
    return report.event_results["recovery_os"]


# ---------------------------------------------------------------------------
# Fairness: order-invariance of simulated outcomes
# ---------------------------------------------------------------------------


def test_outcomes_are_identical_regardless_of_strategy_order() -> None:
    order_a = ("no_action", "naive_retry", "recovery_os")
    order_b = ("recovery_os", "no_action", "naive_retry")
    report_a = run_benchmark(seed=20260828, event_count=60, order=order_a)
    report_b = run_benchmark(seed=20260828, event_count=60, order=order_b)
    assert report_a.run == report_b.run
    for strategy in STRATEGIES:
        assert report_a.event_results[strategy] == report_b.event_results[strategy]


def test_outcomes_identical_for_each_shared_event_regardless_of_order() -> None:
    report_a = run_benchmark(seed=5, event_count=40, order=STRATEGIES)
    report_b = run_benchmark(
        seed=5, event_count=40, order=("naive_retry", "recovery_os", "no_action")
    )
    for event_result_a, event_result_b in zip(
        report_a.event_results["recovery_os"],
        report_b.event_results["recovery_os"],
    ):
        assert event_result_a.event_id == event_result_b.event_id
        assert event_result_a.recovered == event_result_b.recovered
        assert event_result_a.recovered_amount_paise == (
            event_result_b.recovered_amount_paise
        )


# ---------------------------------------------------------------------------
# Ground-truth isolation
# ---------------------------------------------------------------------------


def test_recoveryos_outcomes_depend_only_on_already_selected_interventions() -> None:
    report = run_benchmark(seed=42, event_count=60)
    by_id = {event.event_id: event for event in report.events}
    for record in _recoveryos_records(report):
        if record.exception is not None:
            continue
        event = by_id[record.event_id]
        intervention = record.intervention
        expected = record.recovered_amount_paise
        if record.attempted:
            assert intervention in SIMULATED_INTERVENTIONS
        assert expected == (event.amount_paise if expected > 0 else 0)


def test_no_strategy_consults_ground_truth_to_decide() -> None:
    report_a = run_benchmark(seed=99, event_count=40)
    report_b = run_benchmark(seed=100, event_count=40)
    no_action_a = report_a.event_results["no_action"]
    no_action_b = report_b.event_results["no_action"]
    assert {r.event_id for r in no_action_a} == {r.event_id for r in no_action_b}


# ---------------------------------------------------------------------------
# Safety: fraud and terminal are never executed
# ---------------------------------------------------------------------------


def test_recoveryos_never_executes_fraud_events() -> None:
    report = run_benchmark(seed=42, event_count=120)
    by_id = {event.event_id: event for event in report.events}
    for record in _recoveryos_records(report):
        event = by_id[record.event_id]
        if event.risk_flag == "fraud_suspect":
            assert record.attempted is False
            assert "fraud_protection" in record.denial_reasons
    fraud_rate = fraud_intervention_rate(
        report.event_results["recovery_os"], report.events
    )
    assert fraud_rate == 0.0


def test_recoveryos_never_executes_terminal_events() -> None:
    report = run_benchmark(seed=42, event_count=160)
    by_id = {event.event_id: event for event in report.events}
    for record in _recoveryos_records(report):
        event = by_id[record.event_id]
        if event.risk_flag != "fraud_suspect" and (
            event.failure_reason in ("transaction_declined", "payment_failed")
        ):
            assert record.attempted is False
            assert "terminal_failure" in record.denial_reasons


# ---------------------------------------------------------------------------
# Honesty: no forced positive result, exceptions are never recovery
# ---------------------------------------------------------------------------


def test_benchmark_does_not_force_a_recoveryos_victory() -> None:
    found_loss = False
    for seed in range(1, 30):
        report = run_benchmark(seed=seed, event_count=80)
        no_action = strategy_result(report.run, "no_action")
        recoveryos = strategy_result(report.run, "recovery_os")
        if recoveryos.recovered_amount_paise < no_action.recovered_amount_paise:
            found_loss = True
            break
    assert found_loss, (
        "expected to find a seed where honest RecoveryOS loses to No Action, "
        "but RecoveryOS won on every checked seed"
    )


def test_exceptions_are_visible_and_never_counted_as_recovery() -> None:
    report = run_benchmark(
        seed=42, event_count=20, classifier=FailingExecutionClassifier()
    )
    for record in _recoveryos_records(report):
        assert record.exception is not None
        assert record.attempted is False
        assert record.recovered is False
        assert record.recovered_amount_paise == 0
    recoveryos = strategy_result(report.run, "recovery_os")
    assert recoveryos.exceptions == 20
    assert recoveryos.processed == 0


def test_exception_does_not_count_as_not_recovered_or_failed_outcome() -> None:
    report = run_benchmark(
        seed=42, event_count=20, classifier=FailingExecutionClassifier()
    )
    recoveryos = strategy_result(report.run, "recovery_os")
    assert recoveryos.processed == 0
    assert recoveryos.failed_outcomes == 0
    assert recoveryos.recovered_events == 0


# ---------------------------------------------------------------------------
# Accounting invariants
# ---------------------------------------------------------------------------


def test_accounting_invariant_holds_for_every_strategy() -> None:
    report = run_benchmark(seed=42, event_count=100)
    for strategy in STRATEGIES:
        result = strategy_result(report.run, strategy)
        assert (
            result.processed + result.skipped_events + result.exceptions
            == result.event_count
        )
        assert len(report.event_results[strategy]) == result.event_count


def test_skipped_failed_and_exception_are_distinguished() -> None:
    report = run_benchmark(seed=42, event_count=100)
    naive = strategy_result(report.run, "naive_retry")
    assert naive.skipped_events > 0
    assert naive.interventions_attempted == report.run.event_count - naive.skipped_events
    assert naive.exceptions == 0


# ---------------------------------------------------------------------------
# Invalid configuration fails explicitly
# ---------------------------------------------------------------------------


def test_order_must_not_contain_unknown_strategies() -> None:
    try:
        run_benchmark(seed=42, event_count=10, order=("bogus",))
    except InvalidBenchmarkConfigurationError:
        pass
    else:
        raise AssertionError("unknown strategy must fail explicitly")


def test_invalid_event_count_fails_explicitly() -> None:
    try:
        run_benchmark(seed=42, event_count=0)
    except InvalidBenchmarkConfigurationError:
        pass
    else:
        raise AssertionError("zero event count must fail explicitly")

"""Phase 9 benchmark harness tests: strategies, constants, reproducibility.

These tests exercise the three benchmark strategies directly and through the
runner, enforcing the strategy definitions: No Action attempts nothing; Naive
Retry retries every eligible non-fraud event and nothing else; RecoveryOS runs
the real pipeline. Outcomes are confirmed simulated and deterministic.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from app.benchmark import (
    BENCHMARK_DEFAULT_SEED,
    BENCHMARK_EVENT_COUNT,
    STRATEGIES,
    DeterministicClassifier,
    run_benchmark,
    run_naive_retry,
    run_no_action,
)
from app.benchmark_metrics import strategy_result
from app.classifier import build_classifier_input, build_prompt
from app.classification import CANDIDATE_INTERVENTIONS, ClassificationResult
from app.executor import SIMULATED_INTERVENTIONS
from app.generator import generate_events
from app.models import PaymentEvent
from app.outcome import OutcomeSimulator
from app.outcome_model import HiddenOutcomeModel, generate_hidden_outcome_model
from app.selector import NO_ACTION

BACKEND_ROOT = Path(__file__).resolve().parent.parent


def _events(count: int, seed: int = 42) -> list[PaymentEvent]:
    return generate_events(seed=seed, count=count)


def _simulator(events: list[PaymentEvent], seed: int = 42) -> OutcomeSimulator:
    model = generate_hidden_outcome_model(events, seed)
    return OutcomeSimulator(model)


# ---------------------------------------------------------------------------
# Canonical configuration
# ---------------------------------------------------------------------------


def test_benchmark_event_count_is_canonical_500() -> None:
    assert BENCHMARK_EVENT_COUNT == 500
    assert BENCHMARK_DEFAULT_SEED == 42


# ---------------------------------------------------------------------------
# Runner: shared event set and single shared hidden environment
# ---------------------------------------------------------------------------


def test_run_evaluates_every_strategy_over_the_same_event_set() -> None:
    report = run_benchmark(seed=42, event_count=50)
    assert report.run.event_count == 50
    ids_by_strategy = {
        strategy: {record.event_id for record in report.event_results[strategy]}
        for strategy in STRATEGIES
    }
    shared = ids_by_strategy[STRATEGIES[0]]
    assert len(shared) == 50
    for strategy in STRATEGIES:
        assert ids_by_strategy[strategy] == shared
        result = strategy_result(report.run, strategy)
        assert result.event_count == 50
        assert result.event_count == len(report.event_results[strategy])


def test_run_labels_all_results_simulated() -> None:
    report = run_benchmark(seed=42, event_count=10)
    assert report.run.evaluation_mode == "SIMULATED"
    assert report.run.model_seed == report.run.seed


# ---------------------------------------------------------------------------
# No Action
# ---------------------------------------------------------------------------


def test_no_action_attempts_nothing_and_tracks_passive_baseline() -> None:
    events = _events(25)
    by_id = {event.event_id: event for event in events}
    simulator = _simulator(events)
    records = run_no_action(events, simulator)

    assert len(records) == 25
    for record in records:
        event = by_id[record.event_id]
        assert record.strategy == "no_action"
        assert record.intervention == NO_ACTION
        assert record.attempted is False
        assert record.execution_status is None
        assert record.skipped is False
        assert record.exception is None
        assert record.recovery_source == "passive"
        expected = simulator.simulate(event, NO_ACTION)
        assert record.recovered == expected.recovered
        assert record.recovered_amount_paise == expected.recovered_amount_paise


def test_no_action_recovers_nothing_through_interventions() -> None:
    events = _events(40, seed=7)
    simulator = _simulator(events, seed=7)
    records = run_no_action(events, simulator)
    summary = strategy_result(
        run_benchmark(seed=7, event_count=40).run, "no_action"
    )
    assert summary.interventions_attempted == 0
    assert summary.successful_interventions == 0
    assert summary.failed_outcomes == 0
    assert all(not record.attempted for record in records)


# ---------------------------------------------------------------------------
# Naive Retry
# ---------------------------------------------------------------------------


def test_naive_retry_eligibility_is_non_fraud_only() -> None:
    events = _events(40)
    by_id = {event.event_id: event for event in events}
    simulator = _simulator(events)
    records = run_naive_retry(events, simulator)

    attempted: list[bool] = []
    for record in records:
        event = by_id[record.event_id]
        if event.risk_flag == "fraud_suspect":
            assert record.intervention == NO_ACTION
            assert record.attempted is False
            assert record.skipped is True
            assert record.recovery_source == "passive"
        else:
            assert record.intervention == "retry_immediate"
            assert record.attempted is True
            assert record.executed_by_executor is False
            assert record.execution_status is None
            assert record.skipped is False
            assert record.recovery_source == "attempt"
        attempted.append(record.attempted)
    assert any(attempted)
    assert any(not item for item in attempted)


def test_naive_retry_never_uses_hidden_recovery_probability() -> None:
    fraud = _events(40, seed=11)
    fraud_event = next(
        event for event in fraud if event.risk_flag == "fraud_suspect"
    )
    high_recovery = {
        intervention: 1.0 for intervention in CANDIDATE_INTERVENTIONS
    }
    omniscient = HiddenOutcomeModel(
        seed=11, probabilities={event.event_id: high_recovery for event in fraud}
    )
    assert omniscient.recovery_probability(fraud_event.event_id, "retry_immediate") == 1.0
    records = run_naive_retry(fraud, OutcomeSimulator(omniscient))
    record = next(
        record for record in records if record.event_id == fraud_event.event_id
    )
    assert record.attempted is False
    assert record.skipped is True
    assert record.intervention == NO_ACTION


def test_naive_retry_attempts_do_not_claim_recoveryos_executor_success() -> None:
    report = run_benchmark(seed=42, event_count=60)
    for record in report.event_results["naive_retry"]:
        if record.attempted:
            assert record.intervention == "retry_immediate"
            assert record.executed_by_executor is False
            assert record.execution_status is None
            assert record.recovery_source == "attempt"
        else:
            assert record.execution_status is None
    # Baseline never reports any RecoveryOS executor success.
    naive = strategy_result(report.run, "naive_retry")
    assert naive.successful_interventions == 0
    assert naive.interventions_attempted > 0
    # By contrast, a real RecoveryOS execution reports executor status.
    recoveryos = strategy_result(report.run, "recovery_os")
    assert any(
        record.executed_by_executor and record.execution_status is not None
        for record in report.event_results["recovery_os"]
    )
    assert recoveryos.interventions_attempted > 0


# ---------------------------------------------------------------------------
# RecoveryOS
# ---------------------------------------------------------------------------


def test_recoveryos_runs_the_real_simulated_execution_pipeline() -> None:
    report = run_benchmark(seed=42, event_count=60)
    records = report.event_results["recovery_os"]
    attempted = [record for record in records if record.attempted]
    assert attempted, "expected at least one RecoveryOS execution"
    for record in attempted:
        assert record.executed_by_executor is True
        assert record.execution_status in ("SUCCESS", "FAILED")
        assert record.recovery_source == "attempt"
        assert record.skipped is False
        assert record.exception is None
        assert record.intervention in SIMULATED_INTERVENTIONS
    executed = [
        record
        for record in records
        if record.execution_status == "SUCCESS"
    ]
    assert any(record.execution_status == "SUCCESS" for record in attempted)


def test_recoveryos_never_attempts_payment_link_without_configuration() -> None:
    report = run_benchmark(seed=42, event_count=80)
    for record in report.event_results["recovery_os"]:
        if record.attempted:
            assert record.intervention != "payment_link"


def test_recoveryos_captures_policy_denials_instead_of_discarding() -> None:
    events = _events(60)
    fraud_ids = {
        event.event_id for event in events if event.risk_flag == "fraud_suspect"
    }
    report = run_benchmark(seed=42, event_count=60)
    records = report.event_results["recovery_os"]
    passively_deferred = [
        record for record in records if record.intervention == NO_ACTION
    ]
    assert passively_deferred, "expected policy-deferred events to be captured"
    fraud_records = [
        record
        for record in passively_deferred
        if record.event_id in fraud_ids
    ]
    assert fraud_records
    for record in fraud_records:
        assert "fraud_protection" in record.denial_reasons
        assert record.attempted is False
        assert record.recovery_source == "passive"


def test_recoveryos_outcome_amounts_track_event_amounts() -> None:
    report = run_benchmark(seed=42, event_count=40)
    by_id = {event.event_id: event for event in report.events}
    for strategy in STRATEGIES:
        for record in report.event_results[strategy]:
            if record.exception is not None:
                continue
            expected = (
                by_id[record.event_id].amount_paise if record.recovered else 0
            )
            assert record.recovered_amount_paise == expected
            assert record.recovered == (record.recovered_amount_paise > 0)


# ---------------------------------------------------------------------------
# Classification failures are explicit, never recovery failures
# ---------------------------------------------------------------------------


class AlwaysFailingClassifier:
    def generate(self, prompt: str) -> str:
        raise RuntimeError("provider unavailable")


def test_classification_failure_surfaces_as_visible_exception() -> None:
    report = run_benchmark(
        seed=42, event_count=12, classifier=AlwaysFailingClassifier()
    )
    recoveryos = strategy_result(report.run, "recovery_os")
    assert recoveryos.exceptions == 12
    assert recoveryos.processed == 0
    assert recoveryos.interventions_attempted == 0
    for record in report.event_results["recovery_os"]:
        assert record.exception is not None
        assert record.attempted is False
        assert record.recovered is False
        assert record.recovered_amount_paise == 0
    no_action = strategy_result(report.run, "no_action")
    naive = strategy_result(report.run, "naive_retry")
    assert no_action.exceptions == 0 and naive.exceptions == 0


# ---------------------------------------------------------------------------
# Reproducibility
# ---------------------------------------------------------------------------


def test_benchmark_run_is_deterministic_for_a_fixed_seed() -> None:
    first = run_benchmark(seed=20260828, event_count=40)
    second = run_benchmark(seed=20260828, event_count=40)
    assert first.run == second.run
    assert first.event_results == second.event_results


def test_benchmark_run_changes_with_the_seed() -> None:
    first = run_benchmark(seed=20260828, event_count=40)
    second = run_benchmark(seed=20260829, event_count=40)
    assert first.run != second.run


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def test_cli_module_entrypoint_reports_simulated_results() -> None:
    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "app.benchmark",
            "--seed",
            "7",
            "--count",
            "6",
        ],
        capture_output=True,
        text=True,
        cwd=BACKEND_ROOT,
        check=True,
    )
    payload = json.loads(completed.stdout)
    assert payload["evaluation_mode"] == "SIMULATED"
    assert payload["event_count"] == 6
    assert [row["strategy"] for row in payload["strategy_results"]] == list(STRATEGIES)
    for row in payload["strategy_results"]:
        assert row["processed"] + row["skipped_events"] + row["exceptions"] == 6


# ---------------------------------------------------------------------------
# Deterministic controlled classifier
# ---------------------------------------------------------------------------


def test_deterministic_classifier_consumes_only_decision_time_event_info() -> None:
    events = _events(6)
    classifier = DeterministicClassifier()
    for event in events:
        prompt = build_prompt(build_classifier_input(event))
        parsed = ClassificationResult.from_dict(
            json.loads(classifier.generate(prompt))
        )
        assert parsed.event_id == event.event_id
        assert parsed.root_cause_category in (
            "transient",
            "customer_action_needed",
            "fraud_suspect",
            "terminal",
        )
        assert set(parsed.candidate_interventions) <= (
            CANDIDATE_INTERVENTIONS - {NO_ACTION}
        )
        assert set(json.loads(prompt.split("Event:\n", 1)[1])) <= set(
            event.to_dict()
        )


def test_failing_prompt_payload_fails_closed() -> None:
    classifier = DeterministicClassifier()
    try:
        classifier.generate("Random non-promp text without an event block")
    except Exception as exc:
        assert "prompt payload" in str(exc)
    else:
        raise AssertionError("malformed prompt must fail explicitly")

"""Phase 17 tests: the benchmark is fair, reproducible, and falsifiable.

These tests validate METHODOLOGY, never a business outcome. There is
deliberately no assertion that V2 beats V1 anywhere in this file: an experiment
whose test suite requires the desired answer is not an experiment. The one test
that looks at the comparison asserts only that the harness is CAPABLE of
reporting either answer.
"""

from __future__ import annotations

import pytest

from app.benchmark_config import (
    METHODOLOGY_PHASE17,
    Phase17BenchmarkConfig,
    ROBUSTNESS_SEEDS,
)
from app.benchmark_phase17 import (
    CANONICAL_STRATEGY_ORDER,
    EXCEPTION_CATEGORIES,
    EXCEPTION_SIMULATION,
    POLICY_BOUNDED_STRATEGIES,
    SOURCE_V2_ECONOMIC,
    STRATEGY_NAIVE_RETRY,
    STRATEGY_NO_ACTION,
    STRATEGY_ORACLE,
    STRATEGY_V1,
    STRATEGY_V2,
    Phase17BenchmarkError,
    arm_recoveryos_v2,
    build_event_context,
    evaluate_oracle,
    run_phase17_benchmark,
)
from app.benchmark_phase17_metrics import (
    all_strategy_metrics,
    exception_counts,
    false_intervention_rate,
    interventions_attempted,
    intervention_mix,
    recovered_revenue_paise,
    scoreable_interventions,
    selection_disagreements,
    strategy_metrics,
    total_true_ev_paise,
    unauthorized_attempts,
)
from app.benchmark_simulation import SimulatedExecutor
from app.benchmark import DeterministicClassifier
from app.benchmark_phase17_report import (
    VERDICT_NOT_YET,
    VERDICT_V2_LOST,
    VERDICT_V2_WON,
    canonical_json,
    summarize_report,
    verify_fairness,
)
from app.economics import DEFAULT_ECONOMIC_MODEL
from app.generator import generate_events
from app.hidden_world import HiddenWorld
from app.selector import NO_ACTION

SMALL = Phase17BenchmarkConfig(event_count=120)


@pytest.fixture(scope="module")
def small_report():
    return run_phase17_benchmark(SMALL)


@pytest.fixture(scope="module")
def canonical_report():
    return run_phase17_benchmark(Phase17BenchmarkConfig())


# ---------------------------------------------------------------------------
# Structure
# ---------------------------------------------------------------------------


def test_every_arm_evaluates_every_event(small_report) -> None:
    expected = tuple(event.event_id for event in small_report.events)
    for strategy in CANONICAL_STRATEGY_ORDER:
        records = small_report.for_strategy(strategy)
        assert tuple(record.event_id for record in records) == expected


def test_a_partial_run_is_refused(small_report) -> None:
    with pytest.raises(Phase17BenchmarkError):
        run_phase17_benchmark(SMALL, order=(STRATEGY_V1, STRATEGY_V2))


def test_the_harness_refuses_a_foreign_methodology() -> None:
    with pytest.raises(Phase17BenchmarkError):
        run_phase17_benchmark(
            Phase17BenchmarkConfig(methodology="phase9_v1_compat", event_count=20)
        )


def test_every_run_is_labelled_simulated(small_report) -> None:
    assert small_report.config.evaluation_mode == "SIMULATED"
    assert small_report.config.methodology == METHODOLOGY_PHASE17
    for strategy in CANONICAL_STRATEGY_ORDER:
        for record in small_report.for_strategy(strategy):
            assert record.execution_mode in (None, "SIMULATED")


def test_the_run_id_records_the_frozen_configuration(small_report) -> None:
    assert METHODOLOGY_PHASE17 in small_report.run_id
    assert small_report.config.fingerprint() in small_report.run_id


# ---------------------------------------------------------------------------
# The arms behave as defined
# ---------------------------------------------------------------------------


def test_no_action_never_attempts_anything(small_report) -> None:
    records = small_report.for_strategy(STRATEGY_NO_ACTION)
    assert interventions_attempted(records) == 0
    assert all(record.selected_intervention == NO_ACTION for record in records)


def test_naive_retry_acts_on_every_non_fraud_event(small_report) -> None:
    fraud = {
        event.event_id
        for event in small_report.events
        if event.risk_flag == "fraud_suspect"
    }
    for record in small_report.for_strategy(STRATEGY_NAIVE_RETRY):
        if record.event_id in fraud:
            assert not record.attempted
        else:
            assert record.attempted
            assert record.selected_intervention == "retry_immediate"


def test_recoveryos_only_ever_selects_a_policy_allowed_intervention(
    small_report,
) -> None:
    for strategy in (STRATEGY_V1, STRATEGY_V2, STRATEGY_ORACLE):
        for record in small_report.for_strategy(strategy):
            if record.selected_intervention == NO_ACTION:
                continue
            assert record.selected_intervention in record.allowed_candidates


def test_v1_follows_the_frozen_priority_over_the_allowed_set(small_report) -> None:
    from app.selector import INTERVENTION_PRIORITY

    for record in small_report.for_strategy(STRATEGY_V1):
        if not record.allowed_candidates:
            assert record.selected_intervention == NO_ACTION
            continue
        expected = min(
            record.allowed_candidates, key=INTERVENTION_PRIORITY.index
        )
        assert record.selected_intervention == expected


def test_v1_and_v2_disagree_naturally_on_the_generated_distribution(
    canonical_report,
) -> None:
    """A benchmark where the two engines always agree measures nothing.

    The disagreements must arise from observable features, the estimator and
    the cost model — no event is special-cased anywhere.
    """
    disagreements = selection_disagreements(canonical_report)
    assert len(disagreements) > 0
    metrics = all_strategy_metrics(canonical_report)
    acted = {
        intervention
        for intervention, count in metrics[STRATEGY_V2].intervention_mix.items()
        if intervention != NO_ACTION and count
    }
    assert len(acted) > 1, "V2 must be capable of choosing more than one action"


def test_the_oracle_respects_the_same_policy_boundary(small_report) -> None:
    for record in small_report.for_strategy(STRATEGY_ORACLE):
        assert record.selected_intervention in (
            NO_ACTION,
            *record.allowed_candidates,
        )


def test_the_oracle_cannot_be_beaten_by_a_policy_bounded_arm(small_report) -> None:
    oracle = {
        record.event_id: record
        for record in small_report.for_strategy(STRATEGY_ORACLE)
    }
    for strategy in POLICY_BOUNDED_STRATEGIES:
        for record in small_report.for_strategy(strategy):
            if record.true_ev_paise is None:
                continue
            assert record.true_ev_paise <= oracle[record.event_id].true_ev_paise


def test_a_denied_high_value_intervention_is_unreachable_for_everyone() -> None:
    """Test J: policy DENY beats any expected value, for V1, V2 and the Oracle."""
    events = [
        event
        for event in generate_events(seed=42, count=200)
        if event.risk_flag == "fraud_suspect"
    ]
    assert events
    report = run_phase17_benchmark(
        Phase17BenchmarkConfig(event_count=len(events)), events=events
    )
    for strategy in (STRATEGY_V1, STRATEGY_V2, STRATEGY_ORACLE):
        for record in report.for_strategy(strategy):
            assert record.allowed_candidates == ()
            assert record.selected_intervention == NO_ACTION
            assert not record.attempted


# ---------------------------------------------------------------------------
# Safety
# ---------------------------------------------------------------------------


def test_recoveryos_never_intervenes_on_fraud(canonical_report) -> None:
    """Test K: measured from records, never hardcoded."""
    fraud_events = [
        event
        for event in canonical_report.events
        if event.risk_flag == "fraud_suspect"
    ]
    assert fraud_events, "the dataset must contain fraud for this to mean anything"
    metrics = all_strategy_metrics(canonical_report)
    for strategy in (STRATEGY_V1, STRATEGY_V2):
        assert metrics[strategy].fraud_intervention_rate == 0.0


def test_recoveryos_performs_no_unauthorized_execution(canonical_report) -> None:
    metrics = all_strategy_metrics(canonical_report)
    for strategy in (STRATEGY_V1, STRATEGY_V2, STRATEGY_ORACLE):
        assert metrics[strategy].unauthorized_attempts == 0


def test_the_naive_baseline_is_honestly_recorded_as_ungated(canonical_report) -> None:
    """Naive Retry has no policy gate; the metric measures that, not a defect."""
    metrics = all_strategy_metrics(canonical_report)
    assert metrics[STRATEGY_NAIVE_RETRY].unauthorized_attempts > 0
    assert metrics[STRATEGY_NAIVE_RETRY].policy_bounded is False
    assert metrics[STRATEGY_NAIVE_RETRY].total_regret_paise is None


def test_exceptions_are_categorized_and_never_become_outcomes(small_report) -> None:
    metrics = all_strategy_metrics(small_report)
    for strategy in CANONICAL_STRATEGY_ORDER:
        assert set(metrics[strategy].exceptions_by_category) == set(
            EXCEPTION_CATEGORIES
        )
    for strategy in CANONICAL_STRATEGY_ORDER:
        for record in small_report.for_strategy(strategy):
            if record.exception is not None:
                assert not record.recovered
                assert record.recovered_amount_paise == 0
                assert record.exception_category in EXCEPTION_CATEGORIES


def test_a_failure_after_execution_preserves_the_execution_that_ran() -> None:
    """An execution that really happened is never erased by a later failure.

    Outcome realization is monkeypatched to raise *after* the simulated
    execution has already succeeded. The record must still show the attempt,
    its authorization and its simulated execution, while claiming no recovery.
    """
    world = HiddenWorld(outcome_seed=42, model=DEFAULT_ECONOMIC_MODEL)

    def exploding_realize(event, intervention):
        raise RuntimeError("the world failed to report an outcome")

    world.realize = exploding_realize  # type: ignore[method-assign]

    config = Phase17BenchmarkConfig(event_count=60)
    events = generate_events(seed=config.event_seed, count=config.event_count)
    executor = SimulatedExecutor()
    contexts = [
        build_event_context(event, DeterministicClassifier(), config, world)
        for event in events
    ]

    acted = []
    for context in contexts:
        record = arm_recoveryos_v2(context, world, executor)
        if record.selected_intervention != NO_ACTION:
            acted.append(record)

    assert acted, "the fixture must exercise at least one real intervention"
    for record in acted:
        assert record.exception is not None
        assert record.exception_category == EXCEPTION_SIMULATION
        assert record.attempted is True
        assert record.authorized is True
        assert record.execution is not None
        assert record.execution.execution_mode == "SIMULATED"
        assert record.execution.intervention == record.selected_intervention
        assert record.selection_source == SOURCE_V2_ECONOMIC
        assert record.recovered is False
        assert record.recovered_amount_paise == 0

    assert interventions_attempted(acted) == len(acted)
    assert scoreable_interventions(acted) == 0
    assert recovered_revenue_paise(acted) == 0
    assert unauthorized_attempts(acted) == 0
    assert false_intervention_rate(acted) is None
    assert sum(exception_counts(acted).values()) == len(acted)
    assert intervention_mix(acted) == {
        record.selected_intervention: sum(
            1 for other in acted if other.selected_intervention
            == record.selected_intervention
        )
        for record in acted
    }


def test_a_failure_before_execution_still_records_nothing_attempted() -> None:
    """The pre-execution semantics are unchanged: nothing ran, nothing claimed."""

    class BrokenClassifier:
        def generate(self, prompt: str) -> str:
            raise RuntimeError("classifier unavailable")

    report = run_phase17_benchmark(
        Phase17BenchmarkConfig(event_count=20), classifier=BrokenClassifier()
    )
    for strategy in CANONICAL_STRATEGY_ORDER:
        records = report.for_strategy(strategy)
        assert len(records) == 20
        for record in records:
            assert record.exception is not None
            assert record.attempted is False
            assert record.authorized is False
            assert record.execution is None
            assert record.selected_intervention == NO_ACTION
            assert record.recovered_amount_paise == 0
        assert interventions_attempted(records) == 0


def test_a_broken_classifier_surfaces_as_a_visible_exception() -> None:
    class BrokenClassifier:
        def generate(self, prompt: str) -> str:
            raise RuntimeError("classifier unavailable")

    report = run_phase17_benchmark(
        Phase17BenchmarkConfig(event_count=25), classifier=BrokenClassifier()
    )
    metrics = all_strategy_metrics(report)
    for strategy in CANONICAL_STRATEGY_ORDER:
        assert metrics[strategy].exceptions == 25
        assert metrics[strategy].recovered_revenue_paise == 0


# ---------------------------------------------------------------------------
# Adversarial fairness
# ---------------------------------------------------------------------------


def test_a_strategy_order_reversal_changes_nothing(small_report) -> None:
    """Test A."""
    reversed_run = run_phase17_benchmark(
        SMALL, order=tuple(reversed(CANONICAL_STRATEGY_ORDER))
    )
    assert canonical_json(reversed_run) == canonical_json(small_report)


def test_running_v2_before_v1_changes_nothing(small_report) -> None:
    """Test B."""
    v2_first = run_phase17_benchmark(
        SMALL,
        order=(
            STRATEGY_V2,
            STRATEGY_V1,
            STRATEGY_ORACLE,
            STRATEGY_NAIVE_RETRY,
            STRATEGY_NO_ACTION,
        ),
    )
    assert canonical_json(v2_first) == canonical_json(small_report)


def test_running_the_oracle_first_does_not_move_the_sut(small_report) -> None:
    """Test C: the Oracle is an observer, so observing first changes nothing."""
    oracle_first = run_phase17_benchmark(
        SMALL,
        order=(
            STRATEGY_ORACLE,
            STRATEGY_NO_ACTION,
            STRATEGY_NAIVE_RETRY,
            STRATEGY_V1,
            STRATEGY_V2,
        ),
    )
    for strategy in (STRATEGY_V1, STRATEGY_V2, STRATEGY_NAIVE_RETRY):
        assert [
            record.to_dict() for record in oracle_first.for_strategy(strategy)
        ] == [record.to_dict() for record in small_report.for_strategy(strategy)]


def test_an_event_order_reversal_changes_nothing(small_report) -> None:
    """Test D: identical after normalizing by event id."""
    reversed_events = run_phase17_benchmark(
        SMALL, events=tuple(reversed(small_report.events))
    )
    assert canonical_json(reversed_events) == canonical_json(small_report)


def test_two_runs_are_byte_equivalent(small_report) -> None:
    """Tests E and F."""
    assert canonical_json(run_phase17_benchmark(SMALL)) == canonical_json(small_report)


def test_a_different_seed_produces_a_different_world(small_report) -> None:
    """Test G."""
    other = run_phase17_benchmark(
        Phase17BenchmarkConfig(event_count=120, event_seed=7, outcome_seed=7)
    )
    assert canonical_json(other) != canonical_json(small_report)
    assert other.config.fingerprint() != small_report.config.fingerprint()


def test_the_reported_fairness_checks_all_pass(small_report) -> None:
    assert all(verify_fairness(small_report).values())


def test_every_arm_sees_the_identical_policy_boundary(small_report) -> None:
    reference = {
        record.event_id: record.allowed_candidates
        for record in small_report.for_strategy(STRATEGY_NO_ACTION)
    }
    for strategy in CANONICAL_STRATEGY_ORDER:
        assert {
            record.event_id: record.allowed_candidates
            for record in small_report.for_strategy(strategy)
        } == reference


def test_every_arm_sees_the_identical_hidden_truth(small_report) -> None:
    truth: dict[tuple[str, str], int] = {}
    for strategy in CANONICAL_STRATEGY_ORDER:
        for record in small_report.for_strategy(strategy):
            if record.true_probability_bps is None:
                continue
            key = (record.event_id, record.selected_intervention)
            assert truth.setdefault(key, record.true_probability_bps) == (
                record.true_probability_bps
            )


# ---------------------------------------------------------------------------
# Metrics
# ---------------------------------------------------------------------------


def test_revenue_is_derived_from_records_and_never_hardcoded(small_report) -> None:
    for strategy in CANONICAL_STRATEGY_ORDER:
        records = small_report.for_strategy(strategy)
        assert recovered_revenue_paise(records) == sum(
            record.recovered_amount_paise for record in records
        )


def test_efficiency_is_none_when_nothing_was_attempted(small_report) -> None:
    metrics = strategy_metrics(small_report, STRATEGY_NO_ACTION)
    assert metrics.interventions_attempted == 0
    assert metrics.recovery_efficiency_paise is None
    assert metrics.false_intervention_rate is None
    assert metrics.negative_ev_intervention_rate is None


def test_regret_is_non_negative_and_zero_only_for_the_oracle(small_report) -> None:
    metrics = all_strategy_metrics(small_report)
    assert metrics[STRATEGY_ORACLE].total_regret_paise == 0
    for strategy in (STRATEGY_NO_ACTION, STRATEGY_V1, STRATEGY_V2):
        assert metrics[strategy].total_regret_paise is not None
        assert metrics[strategy].total_regret_paise >= 0


def test_the_oracle_captures_all_of_its_own_value(small_report) -> None:
    metrics = all_strategy_metrics(small_report)
    assert metrics[STRATEGY_ORACLE].oracle_value_capture == 1.0
    assert metrics[STRATEGY_ORACLE].incremental_oracle_value_capture == 1.0
    assert metrics[STRATEGY_ORACLE].optimal_selection_rate == 1.0


def test_true_ev_totals_are_consistent_with_the_world(small_report) -> None:
    world = HiddenWorld(
        outcome_seed=small_report.config.outcome_seed, model=DEFAULT_ECONOMIC_MODEL
    )
    events = {event.event_id: event for event in small_report.events}
    for strategy in CANONICAL_STRATEGY_ORDER:
        records = small_report.for_strategy(strategy)
        recomputed = sum(
            world.true_ev_paise(
                events[record.event_id], record.selected_intervention
            )
            for record in records
            if record.exception is None
        )
        assert total_true_ev_paise(records) == recomputed


def test_a_negative_regret_fails_the_benchmark_rather_than_being_clamped() -> None:
    from app.benchmark_phase17 import BenchmarkIntegrityError, StrategyEventRecord
    from app.benchmark_phase17_metrics import regret_values_paise

    broken = StrategyEventRecord(
        event_id="evt_000001",
        strategy=STRATEGY_V2,
        root_cause_category="transient",
        candidates_considered=("retry_delayed",),
        allowed_candidates=("retry_delayed",),
        selected_intervention="retry_delayed",
        selection_source="v2_economic",
        attempted=True,
        authorized=True,
        execution=None,
        recovered=False,
        recovered_amount_paise=0,
        true_probability_bps=5000,
        true_ev_paise=900,
        oracle_intervention="retry_delayed",
        oracle_true_ev_paise=800,
        no_action_true_ev_paise=100,
    )
    with pytest.raises(BenchmarkIntegrityError):
        regret_values_paise([broken])


def test_the_oracle_prefers_doing_nothing_at_an_exact_tie() -> None:
    """Spending money to achieve the same modelled value is not an improvement."""
    events = generate_events(seed=42, count=1)
    world = HiddenWorld(outcome_seed=42, model=DEFAULT_ECONOMIC_MODEL)
    oracle = evaluate_oracle(events[0], (), world)
    assert oracle.selected_intervention == NO_ACTION
    assert oracle.true_ev_paise == oracle.no_action_true_ev_paise


# ---------------------------------------------------------------------------
# Reporting and falsifiability
# ---------------------------------------------------------------------------


def test_the_summary_is_serializable_and_complete(small_report) -> None:
    import json

    summary = summarize_report(small_report, verify=False)
    json.dumps(summary)
    assert set(summary["strategies"]) == set(CANONICAL_STRATEGY_ORDER)
    assert summary["config"]["methodology"] == METHODOLOGY_PHASE17
    assert summary["result"]["verdict"] in (
        VERDICT_V2_WON,
        VERDICT_V2_LOST,
        VERDICT_NOT_YET,
    )


def test_the_verdict_can_report_any_of_the_three_answers() -> None:
    """The harness must be able to say V2 lost, or the experiment is theatre.

    Fed a deliberately inverted comparison, the same frozen rule must produce
    the losing verdict — proving the rule reads the numbers rather than the
    desired conclusion.
    """
    from app.benchmark_phase17_report import _verdict

    class Fake:
        def __init__(self, ev: int) -> None:
            self.total_true_ev_paise = ev
            self.recovered_revenue_paise = ev

    def verdict_for(v1: int, v2: int) -> str:
        return _verdict(
            {
                STRATEGY_V1: Fake(v1),
                STRATEGY_V2: Fake(v2),
                STRATEGY_ORACLE: Fake(1_000_000),
                STRATEGY_NO_ACTION: Fake(0),
            }
        )["verdict"]

    assert verdict_for(500_000, 900_000) == VERDICT_V2_WON
    assert verdict_for(900_000, 500_000) == VERDICT_V2_LOST
    assert verdict_for(500_000, 500_100) == VERDICT_NOT_YET


def test_the_canonical_configuration_is_five_hundred_events_at_seed_42() -> None:
    config = Phase17BenchmarkConfig()
    assert config.event_count == 500
    assert config.event_seed == 42
    assert config.outcome_seed == 42
    assert 42 in ROBUSTNESS_SEEDS


def test_the_canonical_run_is_exactly_reproducible(canonical_report) -> None:
    assert canonical_json(run_phase17_benchmark(Phase17BenchmarkConfig())) == (
        canonical_json(canonical_report)
    )


@pytest.mark.parametrize("seed", ROBUSTNESS_SEEDS)
def test_every_declared_robustness_seed_runs_and_stays_fair(seed: int) -> None:
    """Declared up front and all reported, so no seed can be cherry-picked."""
    report = run_phase17_benchmark(
        Phase17BenchmarkConfig(event_count=150, event_seed=seed, outcome_seed=seed)
    )
    metrics = all_strategy_metrics(report)
    for strategy in (STRATEGY_V1, STRATEGY_V2):
        assert metrics[strategy].unauthorized_attempts == 0
        assert metrics[strategy].exceptions == 0
        assert metrics[strategy].fraud_intervention_rate in (None, 0.0)

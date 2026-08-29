"""Phase 19: the deterministic policy replay engine.

These tests are about the properties that make a replay a CONTROLLED EXPERIMENT
rather than a simulation: determinism, reuse of the real decision components,
the policy-before-optimizer ordering, fair comparison across scenarios, and
failure accounting that never turns a failure into a result.
"""

from __future__ import annotations

import pytest

from app.benchmark_config import Phase17BenchmarkConfig
from app.benchmark_simulation import SIMULATED, SimulatedExecutor
from app.classification import ClassificationResult
from app.generator import generate_events
from app.policy import PolicyDecision, PolicyEngine
from app.policy_scenario import (
    aggressive_scenario,
    built_in_scenarios,
    conservative_scenario,
    current_scenario,
    custom_scenario,
)
from app.replay import (
    FAILURE_CATEGORIES,
    FAILURE_CLASSIFICATION,
    REPLAY_MODE_SIMULATED,
    ReplayError,
    ReplayEventRecord,
    ReplayIntegrityError,
    ReplayInterventionLedger,
    build_replay_contexts,
    canonical_event_order,
    replay_config,
    replay_scenario,
    replay_scenarios,
)
from app.selector import NO_ACTION

SMALL = 60


def small_config(**overrides) -> Phase17BenchmarkConfig:
    """A smaller frozen config, so tests stay fast without changing methodology."""
    defaults = {"event_count": SMALL}
    defaults.update(overrides)
    return Phase17BenchmarkConfig(**defaults)


def small_events(seed: int = 42, count: int = SMALL):
    return generate_events(seed=seed, count=count)


def canonical_view(result) -> dict:
    """An order-independent canonical view of a replay, for equality checks."""
    return {record.event_id: record.to_dict() for record in result.records}


# ---------------------------------------------------------------------------
# TEST 1 / TEST 13 — determinism
# ---------------------------------------------------------------------------


def test_the_same_policy_replayed_twice_produces_identical_results():
    config = small_config()
    scenario = current_scenario()

    first = replay_scenario(scenario, config=config)
    second = replay_scenario(scenario, config=config)

    assert canonical_view(first) == canonical_view(second)


def test_replay_is_deterministic_in_every_business_figure():
    config = small_config()
    scenario = aggressive_scenario()

    first = replay_scenario(scenario, config=config)
    second = replay_scenario(scenario, config=config)

    for a, b in zip(first.records, second.records):
        assert a.selected_intervention == b.selected_intervention
        assert a.recovered == b.recovered
        assert a.recovered_amount_paise == b.recovered_amount_paise
        assert a.allowed_candidates == b.allowed_candidates
        assert dict(a.denials) == dict(b.denials)
        assert a.attempted == b.attempted
        assert a.failure_category == b.failure_category


def test_replay_id_is_deterministic_and_carries_no_timestamp():
    config = small_config()
    first = replay_scenario(current_scenario(), config=config)
    second = replay_scenario(current_scenario(), config=config)

    assert first.replay_id == second.replay_id


def test_replay_id_changes_with_the_policy():
    config = small_config()
    reference = replay_scenario(current_scenario(), config=config).replay_id
    other = replay_scenario(conservative_scenario(), config=config).replay_id

    assert reference != other


def test_replay_is_invariant_to_the_order_events_are_supplied_in():
    """Replay accumulates history, so its order must come from the data."""
    config = small_config()
    events = small_events()
    scenario = conservative_scenario()

    forward = replay_scenario(scenario, config=config, events=events)
    backward = replay_scenario(
        scenario, config=config, events=tuple(reversed(events))
    )

    assert canonical_view(forward) == canonical_view(backward)


def test_canonical_event_order_is_total_and_data_derived():
    events = small_events()
    ordered = canonical_event_order(events)

    assert [e.event_id for e in ordered] == [
        e.event_id for e in canonical_event_order(tuple(reversed(events)))
    ]
    assert sorted(e.event_id for e in ordered) == sorted(
        e.event_id for e in events
    )


def test_scenario_execution_order_does_not_change_any_scenario_result():
    """Scenario B must not depend on whether scenario A ran first."""
    config = small_config()
    scenarios = built_in_scenarios()

    forward = {
        r.scenario.scenario_id: canonical_view(r)
        for r in replay_scenarios(scenarios, config=config)
    }
    backward = {
        r.scenario.scenario_id: canonical_view(r)
        for r in replay_scenarios(tuple(reversed(scenarios)), config=config)
    }

    assert forward == backward


# ---------------------------------------------------------------------------
# TEST 2 — a different policy really does change behaviour
# ---------------------------------------------------------------------------


def test_a_stricter_policy_changes_decisions_on_the_canonical_workload():
    """Not a fabricated difference: the limit rule genuinely binds here."""
    results = replay_scenarios((current_scenario(), conservative_scenario()))
    current, conservative = results

    current_by_event = current.by_event()
    changed = [
        event_id
        for event_id, record in conservative.by_event().items()
        if record.selected_intervention
        != current_by_event[event_id].selected_intervention
    ]

    assert changed, "the conservative policy must change at least one decision"


def test_the_stricter_policy_blocks_strictly_more():
    results = replay_scenarios((current_scenario(), conservative_scenario()))
    current, conservative = results

    current_blocks = sum(r.blocked_count for r in current.records)
    conservative_blocks = sum(r.blocked_count for r in conservative.records)

    assert conservative_blocks > current_blocks


def test_the_difference_is_attributable_to_the_configured_rule():
    """The extra denials come from the rule the scenario actually configures."""
    from app.policy import RULE_CUSTOMER_LIMIT
    from app.replay_metrics import blocks_by_rule

    current, conservative = replay_scenarios(
        (current_scenario(), conservative_scenario())
    )

    assert (
        blocks_by_rule(conservative.records)[RULE_CUSTOMER_LIMIT]
        > blocks_by_rule(current.records)[RULE_CUSTOMER_LIMIT]
    )


def test_an_identical_policy_under_a_different_name_changes_nothing():
    """Only the policy is causal; the label is not."""
    twin = custom_scenario(current_scenario().parameters, name="Renamed")
    config = small_config()

    reference = replay_scenario(current_scenario(), config=config)
    other = replay_scenario(twin, config=config)

    assert canonical_view(reference) == canonical_view(other)


# ---------------------------------------------------------------------------
# TEST 8 / 17 — fair comparison, shared world
# ---------------------------------------------------------------------------


def test_every_scenario_replays_the_identical_event_set():
    results = replay_scenarios(built_in_scenarios(), config=small_config())
    event_sets = {tuple(r.event_id for r in result.records) for result in results}

    assert len(event_sets) == 1


def test_compared_scenarios_share_the_hidden_outcome_realization():
    """Same event + same intervention must realize the same outcome."""
    results = replay_scenarios(built_in_scenarios(), config=small_config())

    realized: dict[tuple[str, str], tuple[bool, int]] = {}
    for result in results:
        for record in result.records:
            key = (record.event_id, record.selected_intervention)
            observed = (record.recovered, record.recovered_amount_paise)
            assert realized.setdefault(key, observed) == observed


def test_compared_scenarios_share_the_identical_classification():
    results = replay_scenarios(built_in_scenarios(), config=small_config())

    categories: dict[str, str | None] = {}
    for result in results:
        for record in result.records:
            assert (
                categories.setdefault(
                    record.event_id, record.root_cause_category
                )
                == record.root_cause_category
            )


def test_compared_scenarios_share_the_identical_candidate_recommendations():
    results = replay_scenarios(built_in_scenarios(), config=small_config())

    candidates: dict[str, tuple[str, ...]] = {}
    for result in results:
        for record in result.records:
            assert (
                candidates.setdefault(
                    record.event_id, record.candidates_considered
                )
                == record.candidates_considered
            )


def test_compared_scenarios_share_seeds_and_world_identity():
    results = replay_scenarios(built_in_scenarios(), config=small_config())
    keys = (
        "event_seed",
        "outcome_seed",
        "replication",
        "randomization_version",
        "benchmark_methodology",
        "classification_source",
    )
    identities = {
        tuple(result.identity()[key] for key in keys) for result in results
    }

    assert len(identities) == 1


def test_only_the_policy_fingerprint_differs_between_scenarios():
    results = replay_scenarios(built_in_scenarios(), config=small_config())
    policies = {result.identity()["policy_fingerprint"] for result in results}

    assert len(policies) == len(results)


def test_classifications_are_computed_once_and_shared(monkeypatch):
    """Policy is isolated as the variable, and no classifier runs per scenario."""
    from app import replay as replay_module

    calls = {"count": 0}
    original = replay_module.classify_event

    def counting_classify(event, classifier):
        calls["count"] += 1
        return original(event, classifier)

    monkeypatch.setattr(replay_module, "classify_event", counting_classify)
    replay_scenarios(built_in_scenarios(), config=small_config())

    assert calls["count"] == SMALL


# ---------------------------------------------------------------------------
# The real components are reused
# ---------------------------------------------------------------------------


def test_replay_uses_the_real_policy_engine(monkeypatch):
    from app import replay as replay_module

    calls = {"count": 0}
    original = PolicyEngine.evaluate

    def counting_evaluate(self, input, config):
        calls["count"] += 1
        return original(self, input, config)

    monkeypatch.setattr(replay_module.PolicyEngine, "evaluate", counting_evaluate)
    replay_scenario(current_scenario(), config=small_config())

    assert calls["count"] > 0


def test_replay_uses_the_real_phase18_optimizer(monkeypatch):
    """The optimizer is reused, never reimplemented, and runs on every event."""
    from app import execution_service

    calls = {"count": 0}
    original = execution_service.EconomicInterventionOptimizer.select

    def counting_select(self, event, classification, allowed_candidates):
        calls["count"] += 1
        return original(self, event, classification, allowed_candidates)

    monkeypatch.setattr(
        execution_service.EconomicInterventionOptimizer, "select", counting_select
    )
    replay_scenario(current_scenario(), config=small_config())

    assert calls["count"] == SMALL


def test_replay_passes_the_scenario_config_to_the_engine(monkeypatch):
    """The scenario reaches the engine as configuration, not as a branch."""
    from app import replay as replay_module

    seen = []
    original = PolicyEngine.evaluate

    def recording_evaluate(self, input, config):
        seen.append(config)
        return original(self, input, config)

    monkeypatch.setattr(replay_module.PolicyEngine, "evaluate", recording_evaluate)
    scenario = conservative_scenario()
    replay_scenario(scenario, config=small_config())

    assert seen
    assert all(config is scenario.policy_config for config in seen)


def test_replay_module_contains_no_scenario_branching():
    """A scenario is data; business logic must never switch on its name."""
    from pathlib import Path

    import app.replay as replay_module

    source = Path(replay_module.__file__).read_text()
    for scenario_id in ("conservative", "aggressive"):
        assert f'== "{scenario_id}"' not in source
        assert f"== '{scenario_id}'" not in source


# ---------------------------------------------------------------------------
# TEST 11 / 12 — the policy/optimizer boundary
# ---------------------------------------------------------------------------


def test_the_optimizer_only_ever_receives_policy_allowed_candidates(monkeypatch):
    from app import execution_service

    original = execution_service.EconomicInterventionOptimizer.select
    observed: list[tuple[str, tuple[str, ...]]] = []

    def recording_select(self, event, classification, allowed_candidates):
        observed.append((event.event_id, tuple(allowed_candidates.allowed)))
        return original(self, event, classification, allowed_candidates)

    monkeypatch.setattr(
        execution_service.EconomicInterventionOptimizer, "select", recording_select
    )
    result = replay_scenario(conservative_scenario(), config=small_config())

    by_event = result.by_event()
    for event_id, offered in observed:
        record = by_event[event_id]
        assert set(offered) == set(record.allowed_candidates)
        # Nothing the gate denied was ever offered to the optimizer.
        assert not (set(offered) & set(record.denials))


def test_a_policy_denied_candidate_never_reaches_the_optimizer(monkeypatch):
    from app import execution_service

    original = execution_service.EconomicInterventionOptimizer.select
    offered_anywhere: set[str] = set()
    denied_anywhere: set[str] = set()

    def recording_select(self, event, classification, allowed_candidates):
        offered_anywhere.update(
            f"{event.event_id}:{c}" for c in allowed_candidates.allowed
        )
        return original(self, event, classification, allowed_candidates)

    monkeypatch.setattr(
        execution_service.EconomicInterventionOptimizer, "select", recording_select
    )
    result = replay_scenario(conservative_scenario(), config=small_config())
    for record in result.records:
        denied_anywhere.update(f"{record.event_id}:{c}" for c in record.denials)

    assert denied_anywhere, "the workload must actually deny something"
    assert not (denied_anywhere & offered_anywhere)


def test_an_allowed_candidate_does_reach_the_optimizer(monkeypatch):
    """The boundary excludes denials without starving the optimizer."""
    from app import execution_service

    original = execution_service.EconomicInterventionOptimizer.select
    offered: set[str] = set()

    def recording_select(self, event, classification, allowed_candidates):
        offered.update(f"{event.event_id}:{c}" for c in allowed_candidates.allowed)
        return original(self, event, classification, allowed_candidates)

    monkeypatch.setattr(
        execution_service.EconomicInterventionOptimizer, "select", recording_select
    )
    result = replay_scenario(current_scenario(), config=small_config())

    expected = {
        f"{record.event_id}:{c}"
        for record in result.records
        for c in record.allowed_candidates
    }
    assert expected
    assert expected == offered


def test_the_optimizer_runs_after_the_policy_gate(monkeypatch):
    """Ordering is load bearing: authorize, then optimize."""
    from app import execution_service
    from app import replay as replay_module

    sequence: list[str] = []
    original_evaluate = PolicyEngine.evaluate
    original_select = execution_service.EconomicInterventionOptimizer.select

    def evaluate(self, input, config):
        sequence.append(f"policy:{input.event.event_id}")
        return original_evaluate(self, input, config)

    def select(self, event, classification, allowed_candidates):
        sequence.append(f"optimizer:{event.event_id}")
        return original_select(self, event, classification, allowed_candidates)

    monkeypatch.setattr(replay_module.PolicyEngine, "evaluate", evaluate)
    monkeypatch.setattr(
        execution_service.EconomicInterventionOptimizer, "select", select
    )
    replay_scenario(current_scenario(), config=small_config(event_count=5))

    for index, entry in enumerate(sequence):
        if entry.startswith("optimizer:"):
            event_id = entry.split(":", 1)[1]
            assert f"policy:{event_id}" in sequence[:index]


def test_a_selection_outside_the_allowed_set_is_an_integrity_error(monkeypatch):
    """The harness refuses to report a decision policy did not authorize."""
    from app import replay as replay_module

    monkeypatch.setattr(
        replay_module,
        "select_for_strategy",
        lambda event, classification, decisions, strategy: ("payment_link", None),
    )
    scenario = current_scenario()
    # Fraud events have nothing authorized, so any action is unauthorized.
    events = [
        e for e in small_events() if e.risk_flag == "fraud_suspect"
    ][:1]

    with pytest.raises(ReplayIntegrityError, match="policy did not authorize"):
        replay_scenario(scenario, config=small_config(event_count=1), events=events)


# ---------------------------------------------------------------------------
# Cross-event policy history
# ---------------------------------------------------------------------------


def test_the_ledger_mirrors_the_persisted_history_semantics(db_conn):
    """The in-memory ledger must agree with db.get_policy_history exactly."""
    from app import db
    from app.policy import InterventionAttempt, parse_aware_datetime

    events = small_events(count=3)
    event = events[0]
    for stored in events:
        db.insert_payment_event(db_conn, stored)

    attempt = InterventionAttempt(
        event_id=event.event_id,
        intervention="payment_link",
        customer_id=event.customer_id,
        cost_paise=100,
        attempted_at=event.timestamp,
        status="successful",
    )
    db.insert_intervention_attempt(db_conn, attempt)

    ledger = ReplayInterventionLedger()
    ledger.record(attempt)

    evaluation_time = parse_aware_datetime(event.timestamp)
    persisted = db.get_policy_history(db_conn, event, evaluation_time)
    in_memory = ledger.history_for(event, evaluation_time)

    assert in_memory == persisted


def test_the_ledger_starts_empty():
    ledger = ReplayInterventionLedger()
    event = small_events(count=1)[0]
    from app.policy import parse_aware_datetime

    history = ledger.history_for(event, parse_aware_datetime(event.timestamp))

    assert history.customer_intervention_count_24h == 0
    assert history.existing_daily_spend_paise == 0
    assert history.has_successful_intervention is False
    assert history.most_recent_event_intervention_time is None


def test_the_ledger_rejects_anything_that_is_not_an_attempt():
    with pytest.raises(ReplayIntegrityError):
        ReplayInterventionLedger().record({"event_id": "evt_1"})


def test_attempt_status_is_read_from_execution_not_from_recovery():
    """Ground truth must never feed the duplicate rule."""
    result = replay_scenario(current_scenario(), config=small_config())

    performed = [r for r in result.records if r.attempted]
    assert performed
    # Simulated execution always succeeds, so recovery and execution status
    # genuinely diverge; if history were keyed on recovery they would not.
    assert any(not record.recovered for record in performed)


# ---------------------------------------------------------------------------
# Records carry no hidden ground truth
# ---------------------------------------------------------------------------


def test_no_replay_record_carries_a_hidden_probability():
    result = replay_scenario(current_scenario(), config=small_config())
    forbidden = {
        "true_probability_bps",
        "true_ev_paise",
        "draw_bps",
        "oracle_true_ev_paise",
        "no_action_true_ev_paise",
    }

    assert not (set(ReplayEventRecord.__dataclass_fields__) & forbidden)
    for record in result.records:
        assert not (set(record.to_dict()) & forbidden)


def test_replay_records_are_always_labelled_simulated():
    result = replay_scenario(current_scenario(), config=small_config())

    assert result.replay_mode == REPLAY_MODE_SIMULATED
    for record in result.records:
        assert record.replay_mode == REPLAY_MODE_SIMULATED
        assert record.execution_mode in (None, SIMULATED)


def test_a_record_cannot_be_constructed_claiming_real_execution():
    with pytest.raises(ReplayIntegrityError):
        ReplayEventRecord(
            event_id="evt_1",
            customer_id="cust_1",
            amount_paise=100,
            root_cause_category="transient",
            candidates_considered=("retry_delayed",),
            allowed_candidates=("retry_delayed",),
            denials={},
            selected_intervention="retry_delayed",
            selection_reason="max_expected_value",
            selected_expected_value_paise=1,
            attempted=True,
            authorized=True,
            execution_mode="REAL_RAZORPAY",
            recovered=False,
            recovered_amount_paise=0,
            intervention_cost_paise=0,
        )


def test_a_record_cannot_claim_an_unauthorized_attempt():
    with pytest.raises(ReplayIntegrityError, match="authoritative policy ALLOW"):
        ReplayEventRecord(
            event_id="evt_1",
            customer_id="cust_1",
            amount_paise=100,
            root_cause_category="transient",
            candidates_considered=("retry_delayed",),
            allowed_candidates=(),
            denials={},
            selected_intervention="retry_delayed",
            selection_reason=None,
            selected_expected_value_paise=None,
            attempted=True,
            authorized=False,
            execution_mode=SIMULATED,
            recovered=False,
            recovered_amount_paise=0,
            intervention_cost_paise=0,
        )


# ---------------------------------------------------------------------------
# TEST 14 — failure accounting
# ---------------------------------------------------------------------------


def test_a_classification_failure_is_visible_and_recovers_nothing():
    class BrokenClassifier:
        def generate(self, prompt: str) -> str:
            raise RuntimeError("classifier unavailable")

    events = small_events(count=4)
    contexts = build_replay_contexts(events, BrokenClassifier())
    result = replay_scenario(
        current_scenario(), config=small_config(event_count=4), contexts=contexts
    )

    assert len(result.records) == 4
    for record in result.records:
        assert record.failure_category == FAILURE_CLASSIFICATION
        assert record.failure
        assert record.recovered is False
        assert record.recovered_amount_paise == 0
        assert record.attempted is False


def test_a_failed_event_is_not_counted_as_a_zero_recovery():
    """'Nothing recovered' and 'evaluation failed' stay distinguishable."""
    from app.replay_metrics import recovery_rate, replay_metrics

    class BrokenClassifier:
        def generate(self, prompt: str) -> str:
            raise RuntimeError("classifier unavailable")

    contexts = build_replay_contexts(small_events(count=4), BrokenClassifier())
    result = replay_scenario(
        current_scenario(), config=small_config(event_count=4), contexts=contexts
    )
    metrics = replay_metrics(result)

    assert metrics.failures == 4
    assert metrics.processed == 0
    # No denominator survives, so the rate is None rather than a flattering 0.
    assert recovery_rate(result.records) is None


def test_a_simulation_failure_keeps_the_intervention_that_really_ran():
    """A failure must not erase state the pipeline genuinely reached."""

    class BrokenWorld:
        def realize(self, event, intervention):
            raise RuntimeError("outcome unavailable")

    from app import replay as replay_module

    original = replay_module.HiddenWorld
    try:
        replay_module.HiddenWorld = lambda **kwargs: BrokenWorld()
        result = replay_scenario(
            current_scenario(), config=small_config(event_count=8)
        )
    finally:
        replay_module.HiddenWorld = original

    performed = [r for r in result.records if r.attempted]
    assert performed, "the workload must perform at least one intervention"
    for record in performed:
        assert record.failure_category == "simulation_failure"
        assert record.selected_intervention != NO_ACTION
        assert record.execution_mode == SIMULATED
        assert record.recovered_amount_paise == 0


def test_every_failure_carries_a_known_category():
    class BrokenClassifier:
        def generate(self, prompt: str) -> str:
            raise RuntimeError("nope")

    contexts = build_replay_contexts(small_events(count=3), BrokenClassifier())
    result = replay_scenario(
        current_scenario(), config=small_config(event_count=3), contexts=contexts
    )

    for record in result.records:
        assert record.failure_category in FAILURE_CATEGORIES


def test_a_failure_can_never_report_recovery():
    with pytest.raises(ReplayIntegrityError, match="never reports recovery"):
        ReplayEventRecord(
            event_id="evt_1",
            customer_id="cust_1",
            amount_paise=500,
            root_cause_category="transient",
            candidates_considered=(),
            allowed_candidates=(),
            denials={},
            selected_intervention=NO_ACTION,
            selection_reason=None,
            selected_expected_value_paise=None,
            attempted=False,
            authorized=False,
            execution_mode=None,
            recovered=True,
            recovered_amount_paise=500,
            intervention_cost_paise=0,
            failure="boom",
            failure_category="replay_failure",
        )


def test_the_canonical_run_has_no_unexplained_failures():
    result = replay_scenario(current_scenario(), config=small_config())
    assert all(record.failure is None for record in result.records)


# ---------------------------------------------------------------------------
# Configuration binding
# ---------------------------------------------------------------------------


def test_replay_config_binds_only_the_policy():
    base = small_config()
    bound = replay_config(conservative_scenario(), base)

    assert bound.policy_config == conservative_scenario().policy_config
    for field in (
        "methodology",
        "event_count",
        "event_seed",
        "outcome_seed",
        "replication",
        "evaluation_mode",
        "randomization_version",
        "economic_model",
    ):
        assert getattr(bound, field) == getattr(base, field)


def test_replay_config_does_not_mutate_the_base_configuration():
    base = small_config()
    before = base.policy_config
    replay_config(aggressive_scenario(), base)

    assert base.policy_config is before


def test_replay_config_fingerprint_changes_only_with_the_policy():
    base = small_config()
    a = replay_config(current_scenario(), base).fingerprint()
    b = replay_config(conservative_scenario(), base).fingerprint()
    again = replay_config(current_scenario(), base).fingerprint()

    assert a == again
    assert a != b


def test_replay_rejects_a_non_scenario():
    with pytest.raises(ReplayError):
        replay_scenario(current_scenario().policy_config)


def test_replay_scenarios_requires_at_least_one_scenario():
    with pytest.raises(ReplayError):
        replay_scenarios(())


def test_replay_uses_the_injected_executor():
    """The execution boundary is injectable, so tests can prove what ran."""

    class CountingExecutor(SimulatedExecutor):
        def __init__(self) -> None:
            self.calls = 0

        def execute(self, event, intervention, decision=None, **kwargs):
            self.calls += 1
            return super().execute(event, intervention, decision, **kwargs)

    executor = CountingExecutor()
    result = replay_scenario(
        current_scenario(), config=small_config(), executor=executor
    )

    assert executor.calls == sum(1 for r in result.records if r.attempted)
    assert executor.calls > 0


# ---------------------------------------------------------------------------
# Contexts
# ---------------------------------------------------------------------------


def test_contexts_are_built_in_canonical_order():
    contexts = build_replay_contexts(small_events())
    ordered = canonical_event_order(small_events())

    assert [c.event.event_id for c in contexts] == [e.event_id for e in ordered]


def test_contexts_evaluate_each_event_at_its_own_timestamp():
    contexts = build_replay_contexts(small_events(count=5))

    for context in contexts:
        assert context.evaluation_time.isoformat() == context.event.timestamp


def test_contexts_produce_a_real_classification():
    contexts = build_replay_contexts(small_events(count=5))

    for context in contexts:
        assert isinstance(context.classification, ClassificationResult)
        assert context.classification.event_id == context.event.event_id


def test_sharing_contexts_guarantees_the_same_decisions_input():
    contexts = build_replay_contexts(small_events())
    config = small_config()

    a = replay_scenario(current_scenario(), config=config, contexts=contexts)
    b = replay_scenario(current_scenario(), config=config, contexts=contexts)

    assert canonical_view(a) == canonical_view(b)


def test_every_allowed_candidate_is_backed_by_a_real_allow_decision():
    """Sanity: the allowed set is derived from decisions, not asserted."""
    result = replay_scenario(current_scenario(), config=small_config())

    for record in result.records:
        assert not (set(record.allowed_candidates) & set(record.denials))
        assert isinstance(record.allowed_candidates, tuple)


def test_decisions_are_real_policy_decisions(monkeypatch):
    from app import replay as replay_module

    seen: list[object] = []
    original = PolicyEngine.evaluate

    def recording(self, input, config):
        decision = original(self, input, config)
        seen.append(decision)
        return decision

    monkeypatch.setattr(replay_module.PolicyEngine, "evaluate", recording)
    replay_scenario(current_scenario(), config=small_config(event_count=5))

    assert seen
    assert all(isinstance(decision, PolicyDecision) for decision in seen)

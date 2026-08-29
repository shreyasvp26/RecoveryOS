"""Phase 19 safety invariants — the guarantees replay is not allowed to break.

Replay is SIMULATION ONLY. These tests prove structurally, not by convention,
that it cannot reach Razorpay, cannot create a Payment Link, cannot perform a
production execution, cannot mutate the active policy or the historical audit,
and cannot disable a safety protection however the scenario is configured.
"""

from __future__ import annotations

import ast
import sqlite3
from pathlib import Path

import pytest

from app import db
from app.benchmark_config import Phase17BenchmarkConfig
from app.benchmark_simulation import SIMULATED, SimulatedExecutor
from app.classification import ClassificationResult
from app.generator import generate_events
from app.models import PaymentEvent
from app.policy import (
    RULE_DUPLICATE,
    RULE_FRAUD,
    RULE_TERMINAL,
    InterventionAttempt,
    PolicyEngine,
    PolicyInput,
    parse_aware_datetime,
)
from app.policy_scenario import (
    CUSTOM_MAX_MAX_INTERVENTIONS,
    IMMUTABLE_PROTECTIONS,
    PolicyScenarioError,
    aggressive_scenario,
    built_in_scenarios,
    current_scenario,
    custom_scenario,
)
from app.replay import (
    REPLAY_MODE_SIMULATED,
    ReplayInterventionLedger,
    build_replay_contexts,
    replay_scenario,
    replay_scenarios,
)
from app.replay_metrics import blocks_by_rule, replay_metrics
from app.selector import NO_ACTION

SMALL = 60


def small_config(**overrides) -> Phase17BenchmarkConfig:
    defaults = {"event_count": SMALL}
    defaults.update(overrides)
    return Phase17BenchmarkConfig(**defaults)


def most_permissive_scenario():
    """The most permissive policy the lab will accept at all."""
    return custom_scenario(
        {
            "max_interventions_per_customer_24h": CUSTOM_MAX_MAX_INTERVENTIONS,
            "event_cooldown_minutes": 1,
            "daily_spend_cap_paise": 0,
        },
        name="Maximally permissive",
    )


# ---------------------------------------------------------------------------
# NON-NEGOTIABLE 1 — replay never performs real Razorpay execution
# ---------------------------------------------------------------------------


class RazorpaySpy:
    """A stand-in that records any attempt to reach the provider."""

    def __init__(self) -> None:
        self.calls: list[tuple] = []

    def create_payment_link(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        raise AssertionError("replay must never create a Payment Link")


def test_replay_never_calls_the_razorpay_client(monkeypatch):
    spy = RazorpaySpy()
    monkeypatch.setattr(
        "app.razorpay_client.RazorpayPaymentLinkClient.create_payment_link",
        lambda self, *a, **k: spy.create_payment_link(*a, **k),
    )
    monkeypatch.setattr("app.config.build_razorpay_client", lambda: spy)

    replay_scenarios(built_in_scenarios(), config=small_config())

    assert spy.calls == []


def test_replay_never_creates_a_payment_link_even_when_it_selects_one(
    monkeypatch,
):
    """payment_link is genuinely selected here, and still nothing is created."""
    spy = RazorpaySpy()
    monkeypatch.setattr(
        "app.razorpay_client.RazorpayPaymentLinkClient.create_payment_link",
        lambda self, *a, **k: spy.create_payment_link(*a, **k),
    )

    result = replay_scenario(current_scenario(), config=small_config())
    selected_links = [
        r for r in result.records if r.selected_intervention == "payment_link"
    ]

    assert selected_links, "the workload must actually select payment_link"
    assert spy.calls == []
    for record in selected_links:
        assert record.execution_mode == SIMULATED


def test_replay_never_builds_a_razorpay_client(monkeypatch):
    def forbidden():
        raise AssertionError("replay must never construct a Razorpay client")

    monkeypatch.setattr("app.config.build_razorpay_client", forbidden)
    replay_scenarios(built_in_scenarios(), config=small_config())


def test_replay_never_reads_razorpay_credentials(monkeypatch):
    for getter in (
        "get_razorpay_key_id",
        "get_razorpay_key_secret",
        "get_razorpay_webhook_secret",
    ):
        monkeypatch.setattr(
            f"app.config.{getter}",
            lambda: (_ for _ in ()).throw(
                AssertionError(f"replay must never read {getter}")
            ),
        )
    replay_scenarios(built_in_scenarios(), config=small_config())


def test_replay_never_uses_the_production_bounded_executor(monkeypatch):
    """The production executor is the only path that can reach a provider."""

    def forbidden(*args, **kwargs):
        raise AssertionError("replay must never use the production executor")

    monkeypatch.setattr("app.executor.BoundedExecutor.execute", forbidden)
    replay_scenarios(built_in_scenarios(), config=small_config())


def test_the_replay_module_does_not_import_razorpay_or_the_executor():
    """Structural, not behavioural: the path cannot reach a provider at all."""
    source = Path(__import__("app.replay", fromlist=["x"]).__file__).read_text()
    tree = ast.parse(source)

    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module)
        elif isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)

    for forbidden in ("razorpay", ".razorpay_client", ".executor", "httpx", "requests"):
        assert forbidden not in imported, forbidden


def test_the_simulated_executor_has_no_provider_dependency():
    """Replay's execution boundary is the benchmark's offline simulator.

    Checked against the imports rather than the text, so the module stays free
    to EXPLAIN in prose why it does not touch a provider.
    """
    source = Path(
        __import__("app.benchmark_simulation", fromlist=["x"]).__file__
    ).read_text()
    tree = ast.parse(source)

    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module)
        elif isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)

    for forbidden in ("razorpay", ".razorpay_client", ".executor", "httpx", "requests"):
        assert forbidden not in imported, forbidden


def test_every_execution_in_a_replay_is_simulated():
    for result in replay_scenarios(built_in_scenarios(), config=small_config()):
        assert result.replay_mode == REPLAY_MODE_SIMULATED
        for record in result.records:
            assert record.replay_mode == REPLAY_MODE_SIMULATED
            assert record.execution_mode in (None, SIMULATED)
            if record.attempted:
                assert record.execution_mode == SIMULATED


def test_replay_performs_no_production_execution(monkeypatch):
    """Nothing goes through the database-backed execution service."""

    def forbidden(*args, **kwargs):
        raise AssertionError("replay must never call execute_event")

    monkeypatch.setattr("app.execution_service.execute_event", forbidden)
    replay_scenarios(built_in_scenarios(), config=small_config())


# ---------------------------------------------------------------------------
# NON-NEGOTIABLE 12/13/14 — the immutable protections
# ---------------------------------------------------------------------------


def _decision_for(event: PaymentEvent, classification, intervention, scenario):
    ledger = ReplayInterventionLedger()
    evaluation_time = parse_aware_datetime(event.timestamp)
    return PolicyEngine().evaluate(
        PolicyInput(
            event=event,
            classification=classification,
            proposed_intervention=intervention,
            history=ledger.history_for(event, evaluation_time),
            evaluation_time=evaluation_time,
        ),
        scenario.policy_config,
    )


def test_no_scenario_can_disable_fraud_protection():
    """TEST 4 — including the most permissive policy the lab will accept."""
    contexts = build_replay_contexts(generate_events(seed=42, count=200))
    fraud = [c for c in contexts if c.event.risk_flag == "fraud_suspect"]
    assert fraud

    for scenario in (*built_in_scenarios(), most_permissive_scenario()):
        for context in fraud:
            decision = _decision_for(
                context.event, context.classification, "payment_link", scenario
            )
            assert decision.allowed is False
            assert decision.denial_reason == RULE_FRAUD


def test_no_scenario_ever_intervenes_on_a_fraud_event():
    for result in replay_scenarios(
        (*built_in_scenarios(), most_permissive_scenario()),
        config=small_config(event_count=200),
    ):
        for record in result.records:
            if record.root_cause_category == "fraud_suspect":
                assert record.attempted is False
                assert record.selected_intervention == NO_ACTION
        assert replay_metrics(result).fraud_interventions == 0


def test_no_scenario_can_disable_terminal_protection():
    """TEST 5."""
    contexts = build_replay_contexts(generate_events(seed=42, count=200))
    terminal = [
        c
        for c in contexts
        if c.classification
        and c.classification.root_cause_category == "terminal"
    ]
    assert terminal

    for scenario in (*built_in_scenarios(), most_permissive_scenario()):
        for context in terminal:
            decision = _decision_for(
                context.event, context.classification, "retry_delayed", scenario
            )
            assert decision.allowed is False
            assert decision.denial_reason == RULE_TERMINAL


def test_no_scenario_ever_intervenes_on_a_terminal_event():
    for result in replay_scenarios(
        (*built_in_scenarios(), most_permissive_scenario()),
        config=small_config(event_count=200),
    ):
        for record in result.records:
            if record.root_cause_category == "terminal":
                assert record.attempted is False
        assert replay_metrics(result).terminal_interventions == 0


def test_no_scenario_can_bypass_duplicate_protection():
    """TEST 6 — a successful intervention on an event blocks a second one."""
    events = generate_events(seed=42, count=200)
    contexts = build_replay_contexts(events)
    usable = next(
        c
        for c in contexts
        if c.event.risk_flag == "normal"
        and c.classification.root_cause_category != "terminal"
    )
    event = usable.event

    for scenario in (*built_in_scenarios(), most_permissive_scenario()):
        ledger = ReplayInterventionLedger()
        ledger.record(
            InterventionAttempt(
                event_id=event.event_id,
                intervention="payment_link",
                customer_id=event.customer_id,
                cost_paise=0,
                attempted_at=event.timestamp,
                status="successful",
            )
        )
        evaluation_time = parse_aware_datetime(event.timestamp)
        decision = PolicyEngine().evaluate(
            PolicyInput(
                event=event,
                classification=usable.classification,
                proposed_intervention="retry_delayed",
                history=ledger.history_for(event, evaluation_time),
                evaluation_time=evaluation_time,
            ),
            scenario.policy_config,
        )
        assert decision.allowed is False
        assert decision.denial_reason == RULE_DUPLICATE


def test_a_replay_refuses_to_evaluate_the_same_event_twice():
    """The harness itself is the outer half of duplicate protection.

    Even before the engine's duplicate rule is reached, a replay that saw one
    event twice could double-count an intervention and a recovery, so the
    result type refuses to represent it at all.
    """
    from app.replay import ReplayIntegrityError

    usable = next(
        c
        for c in build_replay_contexts(generate_events(seed=42, count=200))
        if c.event.risk_flag == "normal"
        and c.classification.root_cause_category != "terminal"
    )
    contexts = build_replay_contexts([usable.event, usable.event])

    with pytest.raises(ReplayIntegrityError, match="at most once"):
        replay_scenario(
            most_permissive_scenario(),
            config=small_config(event_count=2),
            contexts=contexts,
        )


def test_a_successful_attempt_blocks_a_second_one_on_the_same_event():
    """The ledger feeds the engine's duplicate rule exactly as production does."""
    usable = next(
        c
        for c in build_replay_contexts(generate_events(seed=42, count=200))
        if c.event.risk_flag == "normal"
        and c.classification.root_cause_category != "terminal"
    )
    event = usable.event
    evaluation_time = parse_aware_datetime(event.timestamp)

    ledger = ReplayInterventionLedger()
    before = ledger.history_for(event, evaluation_time)
    assert before.has_successful_intervention is False

    ledger.record(
        InterventionAttempt(
            event_id=event.event_id,
            intervention="payment_link",
            customer_id=event.customer_id,
            cost_paise=0,
            attempted_at=event.timestamp,
            status="successful",
        )
    )
    after = ledger.history_for(event, evaluation_time)

    assert after.has_successful_intervention is True


def test_the_immutable_protections_deny_identically_under_every_scenario():
    """A scenario can move thresholds; it cannot move a locked stop."""
    results = replay_scenarios(
        (*built_in_scenarios(), most_permissive_scenario()),
        config=small_config(event_count=200),
    )
    counts = [blocks_by_rule(result.records) for result in results]

    for protection in IMMUTABLE_PROTECTIONS:
        assert len({count[protection] for count in counts}) == 1, protection


def test_the_policy_engine_reads_no_configuration_for_a_locked_stop():
    """Structural: the fraud and terminal branches take no config at all."""
    source = Path(__import__("app.policy", fromlist=["x"]).__file__).read_text()

    assert 'if event.risk_flag == "fraud_suspect":' in source
    assert 'if classification.root_cause_category == "terminal":' in source
    assert "if history.has_successful_intervention:" in source


# ---------------------------------------------------------------------------
# NON-NEGOTIABLE 2 — replay never mutates the active policy
# ---------------------------------------------------------------------------


def test_replay_does_not_mutate_the_active_policy_configuration():
    """TEST 9."""
    from app import config as config_module

    before = config_module.build_policy_config()
    replay_scenarios(
        (*built_in_scenarios(), most_permissive_scenario()),
        config=small_config(),
    )
    after = config_module.build_policy_config()

    assert before == after
    assert after.max_interventions_per_customer_24h == (
        config_module.DEFAULT_POLICY_MAX_INTERVENTIONS_PER_CUSTOMER_24H
    )


def test_replay_does_not_mutate_the_environment(monkeypatch):
    import os

    before = dict(os.environ)
    replay_scenarios(built_in_scenarios(), config=small_config())

    assert dict(os.environ) == before


def test_replay_does_not_mutate_the_frozen_benchmark_policy():
    from app.benchmark_config import frozen_policy_config

    before = frozen_policy_config()
    replay_scenarios(built_in_scenarios(), config=small_config())
    after = frozen_policy_config()

    assert before == after


def test_replay_does_not_mutate_the_base_configuration_object():
    base = small_config()
    before = base.fingerprint()
    replay_scenarios(built_in_scenarios(), config=base)

    assert base.fingerprint() == before


def test_a_scenarios_policy_config_is_not_shared_between_scenarios():
    scenarios = built_in_scenarios()
    configs = [s.policy_config for s in scenarios]

    assert len({id(config) for config in configs}) == len(configs)


# ---------------------------------------------------------------------------
# NON-NEGOTIABLE 3 — replay never mutates historical audit records
# ---------------------------------------------------------------------------


def _audit_snapshot(conn: sqlite3.Connection) -> dict[str, list]:
    tables = [
        row[0]
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
        )
    ]
    return {
        table: conn.execute(f"SELECT * FROM {table}").fetchall()
        for table in tables
    }


def test_replay_does_not_write_to_any_database_table(db_conn):
    """TEST 10 — the whole database is byte-identical afterwards."""
    events = generate_events(seed=42, count=5)
    for event in events:
        db.insert_payment_event(db_conn, event)
    db.insert_intervention_attempt(
        db_conn,
        InterventionAttempt(
            event_id=events[0].event_id,
            intervention="payment_link",
            customer_id=events[0].customer_id,
            cost_paise=100,
            attempted_at=events[0].timestamp,
            status="successful",
        ),
    )
    db_conn.commit()

    before = _audit_snapshot(db_conn)
    replay_scenarios(built_in_scenarios(), config=small_config())
    after = _audit_snapshot(db_conn)

    assert before == after


def test_replay_never_opens_a_database_connection(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("replay must never touch the database")

    monkeypatch.setattr("app.db.connect_database", forbidden)
    monkeypatch.setattr("app.db.connect", forbidden)
    replay_scenarios(built_in_scenarios(), config=small_config())


def test_replay_never_inserts_an_intervention_attempt(monkeypatch):
    """Production policy history must be unreachable from replay."""

    def forbidden(*args, **kwargs):
        raise AssertionError("replay must never persist an intervention attempt")

    monkeypatch.setattr("app.db.insert_intervention_attempt", forbidden)
    monkeypatch.setattr("app.db.insert_execution_outcome", forbidden)
    monkeypatch.setattr("app.db.insert_policy_decision", forbidden)
    monkeypatch.setattr("app.db.insert_optimizer_decision", forbidden)
    replay_scenarios(built_in_scenarios(), config=small_config())


def test_the_replay_module_does_not_import_the_persistence_layer():
    source = Path(__import__("app.replay", fromlist=["x"]).__file__).read_text()
    tree = ast.parse(source)

    imported = {
        node.module
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module
    }
    assert ".db" not in imported
    assert "sqlite3" not in imported


def test_replay_does_not_read_production_policy_history(monkeypatch):
    """History comes from the replay's own ledger, never from persisted state."""

    def forbidden(*args, **kwargs):
        raise AssertionError("replay must derive history from its own ledger")

    monkeypatch.setattr("app.db.get_policy_history", forbidden)
    replay_scenarios(built_in_scenarios(), config=small_config())


# ---------------------------------------------------------------------------
# NON-NEGOTIABLE 4/6/7 — benchmark ground truth is untouched
# ---------------------------------------------------------------------------


def test_replay_does_not_change_the_hidden_world_fingerprint():
    from app.hidden_world import hidden_world_fingerprint

    before = hidden_world_fingerprint()
    replay_scenarios(built_in_scenarios(), config=small_config())

    assert hidden_world_fingerprint() == before


def test_replay_does_not_change_the_event_generator_fingerprint():
    from app.generator import event_generator_fingerprint

    before = event_generator_fingerprint()
    replay_scenarios(built_in_scenarios(), config=small_config())

    assert event_generator_fingerprint() == before


def test_the_canonical_benchmark_is_reproducible_after_replay():
    """TEST 15 — Phase 19 changes no canonical benchmark number."""
    from app.benchmark_phase17 import run_phase17_benchmark
    from app.benchmark_phase17_report import canonical_json

    config = Phase17BenchmarkConfig(event_count=40)
    before = canonical_json(run_phase17_benchmark(config))
    replay_scenarios(built_in_scenarios(), config=small_config())
    after = canonical_json(run_phase17_benchmark(config))

    assert before == after


def test_the_canonical_benchmark_configuration_is_unchanged():
    """The frozen canonical parameters are exactly what Phase 17 published."""
    from app.benchmark_config import (
        CANONICAL_EVENT_COUNT,
        CANONICAL_EVENT_SEED,
        CANONICAL_OUTCOME_SEED,
        METHODOLOGY_PHASE17,
    )

    config = Phase17BenchmarkConfig()
    assert config.methodology == METHODOLOGY_PHASE17
    assert config.event_count == CANONICAL_EVENT_COUNT == 500
    assert config.event_seed == CANONICAL_EVENT_SEED == 42
    assert config.outcome_seed == CANONICAL_OUTCOME_SEED == 42


def test_replay_realizes_outcomes_from_the_same_world_as_the_benchmark():
    """No second ground truth: replay and the benchmark share the world."""
    from app.hidden_world import HiddenWorld

    config = small_config()
    world = HiddenWorld(
        outcome_seed=config.outcome_seed,
        model=config.economic_model,
        replication=config.replication,
    )
    result = replay_scenario(current_scenario(), config=config)

    for record in result.records:
        expected = world.realize(
            next(
                e
                for e in generate_events(
                    seed=config.event_seed, count=config.event_count
                )
                if e.event_id == record.event_id
            ),
            record.selected_intervention,
        )
        assert record.recovered == expected.recovered
        assert record.recovered_amount_paise == expected.recovered_amount_paise


def test_outcome_realization_does_not_depend_on_scenario_execution_order():
    """TEST 8 — the same event/action realizes identically in every scenario."""
    scenarios = (*built_in_scenarios(), most_permissive_scenario())
    forward = replay_scenarios(scenarios, config=small_config())
    backward = replay_scenarios(tuple(reversed(scenarios)), config=small_config())

    realized = {}
    for result in (*forward, *backward):
        for record in result.records:
            key = (record.event_id, record.selected_intervention)
            observed = (record.recovered, record.recovered_amount_paise)
            assert realized.setdefault(key, observed) == observed


# ---------------------------------------------------------------------------
# TEST 7 — invalid policy never reaches execution
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "parameters",
    [
        {
            "max_interventions_per_customer_24h": -1,
            "event_cooldown_minutes": 30,
            "daily_spend_cap_paise": 5_000_000,
        },
        {
            "max_interventions_per_customer_24h": 2,
            "event_cooldown_minutes": -30,
            "daily_spend_cap_paise": 5_000_000,
        },
        {
            "max_interventions_per_customer_24h": 2,
            "event_cooldown_minutes": 30,
            "daily_spend_cap_paise": -1,
        },
        {
            "max_interventions_per_customer_24h": "two",
            "event_cooldown_minutes": 30,
            "daily_spend_cap_paise": 5_000_000,
        },
    ],
)
def test_an_invalid_policy_is_rejected_before_anything_runs(parameters, monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("a malformed scenario must never reach execution")

    monkeypatch.setattr(SimulatedExecutor, "execute", forbidden)

    with pytest.raises(PolicyScenarioError):
        custom_scenario(parameters)


def test_a_malformed_scenario_cannot_be_replayed():
    from app.replay import ReplayError

    with pytest.raises((ReplayError, PolicyScenarioError)):
        replay_scenario({"scenario_id": "custom"})


# ---------------------------------------------------------------------------
# Authorization boundary
# ---------------------------------------------------------------------------


def test_replay_executes_only_under_an_authoritative_allow():
    for result in replay_scenarios(built_in_scenarios(), config=small_config()):
        for record in result.records:
            if record.attempted:
                assert record.authorized is True
                assert record.selected_intervention in record.allowed_candidates


def test_replay_requires_authorization_from_the_simulator(monkeypatch):
    """Unlike the Naive Retry benchmark arm, replay never opts out."""
    seen: list[bool] = []
    original = SimulatedExecutor.execute

    def recording(self, event, intervention, decision=None, **kwargs):
        seen.append(kwargs.get("require_authorization", True))
        return original(self, event, intervention, decision, **kwargs)

    monkeypatch.setattr(SimulatedExecutor, "execute", recording)
    replay_scenario(current_scenario(), config=small_config())

    assert seen
    assert all(seen), "replay must always require authorization"


def test_aggressive_widens_thresholds_without_widening_authorization():
    """More permissive must not mean more unauthorized action."""
    for result in replay_scenarios(
        (current_scenario(), aggressive_scenario(), most_permissive_scenario()),
        config=small_config(event_count=200),
    ):
        metrics = replay_metrics(result)
        assert metrics.unauthorized_attempts == 0
        assert metrics.fraud_interventions == 0
        assert metrics.terminal_interventions == 0


# ---------------------------------------------------------------------------
# Hidden ground truth isolation
# ---------------------------------------------------------------------------


def test_the_hidden_world_is_never_passed_to_a_decision_component(monkeypatch):
    """No decision module may receive a hidden probability."""
    from app.hidden_world import HiddenWorld

    original_evaluate = PolicyEngine.evaluate

    def guarded_evaluate(self, input, config):
        for value in (input.event, input.classification, config):
            assert not isinstance(value, HiddenWorld)
        return original_evaluate(self, input, config)

    monkeypatch.setattr(PolicyEngine, "evaluate", guarded_evaluate)
    replay_scenario(current_scenario(), config=small_config())


def test_the_optimizer_never_sees_ground_truth(monkeypatch):
    from app import execution_service
    from app.hidden_world import HiddenWorld

    original = execution_service.EconomicInterventionOptimizer.select

    def guarded(self, event, classification, allowed_candidates):
        assert not isinstance(getattr(self, "_estimator", None), HiddenWorld)
        assert isinstance(classification, ClassificationResult)
        return original(self, event, classification, allowed_candidates)

    monkeypatch.setattr(
        execution_service.EconomicInterventionOptimizer, "select", guarded
    )
    replay_scenario(current_scenario(), config=small_config())


def test_the_world_is_consulted_only_after_the_decision(monkeypatch):
    """Ground truth is read once per event, after selection and execution."""
    from app import replay as replay_module
    from app.hidden_world import HiddenWorld

    order: list[str] = []
    original_select = replay_module.select_for_strategy
    original_realize = HiddenWorld.realize

    def select(event, classification, decisions, strategy):
        order.append(f"select:{event.event_id}")
        return original_select(event, classification, decisions, strategy)

    def realize(self, event, intervention):
        order.append(f"realize:{event.event_id}")
        return original_realize(self, event, intervention)

    monkeypatch.setattr(replay_module, "select_for_strategy", select)
    monkeypatch.setattr(HiddenWorld, "realize", realize)
    replay_scenario(current_scenario(), config=small_config(event_count=10))

    for index, entry in enumerate(order):
        if entry.startswith("realize:"):
            event_id = entry.split(":", 1)[1]
            assert f"select:{event_id}" in order[:index]


def test_the_world_is_realized_exactly_once_per_event(monkeypatch):
    from app.hidden_world import HiddenWorld

    calls: list[str] = []
    original = HiddenWorld.realize

    def counting(self, event, intervention):
        calls.append(event.event_id)
        return original(self, event, intervention)

    monkeypatch.setattr(HiddenWorld, "realize", counting)
    replay_scenario(current_scenario(), config=small_config())

    assert len(calls) == len(set(calls)) == SMALL

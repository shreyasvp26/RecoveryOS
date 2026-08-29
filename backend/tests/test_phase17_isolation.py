"""Phase 17 tests: hidden ground truth cannot reach the system under test.

Phase 16 proved the decision engine could not reach the Phase 8 evaluation
layer. Phase 17 adds a NEW hidden world, a NEW oracle and a NEW benchmark, so
the same guarantee has to be re-established against the new modules — otherwise
the isolation tests would keep passing while the leak moved next door.

Everything here is asserted against the code itself. Behavioural evidence that
the optimizer currently does not read ground truth is much weaker than proof
that it structurally cannot.
"""

from __future__ import annotations

import ast
import pathlib

import pytest

APP_DIR = pathlib.Path(__file__).resolve().parent.parent / "app"

# The system under test: everything RecoveryOS would actually ship.
SUT_MODULES: tuple[str, ...] = (
    "classifier",
    "classification",
    "policy",
    "selector",
    "estimator",
    "economics",
    "optimizer",
    "executor",
    "execution_service",
)

# The evaluation layer, which owns ground truth. Includes both the frozen
# Phase 9 world and the Phase 17 world.
EVALUATION_MODULES: frozenset[str] = frozenset(
    {
        "benchmark",
        "benchmark_metrics",
        "benchmark_store",
        "benchmark_config",
        "benchmark_phase17",
        "benchmark_phase17_metrics",
        "benchmark_phase17_report",
        "benchmark_simulation",
        "outcome",
        "outcome_model",
        "hidden_world",
    }
)

# Names that only ever appear where hidden truth is being handled.
GROUND_TRUTH_TERMS: tuple[str, ...] = (
    "HiddenWorld",
    "HiddenOutcome",
    "true_probability_bps",
    "true_expected_value_paise",
    "deterministic_draw_bps",
    "OracleEvaluation",
    "evaluate_oracle",
    "oracle_true_ev_paise",
    "true_ev_paise",
    "hidden_probability",
    "ground_truth",
)


def _module_path(name: str) -> pathlib.Path:
    return APP_DIR / f"{name}.py"


def _imports(name: str) -> set[str]:
    tree = ast.parse(_module_path(name).read_text())
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            found.add(node.module.split(".")[0])
    return found


def _app_imports(name: str) -> set[str]:
    return {
        imported for imported in _imports(name) if _module_path(imported).exists()
    }


def _transitive_app_imports(name: str) -> set[str]:
    seen: set[str] = set()
    pending = [name]
    while pending:
        for imported in _app_imports(pending.pop()):
            if imported not in seen:
                seen.add(imported)
                pending.append(imported)
    return seen


def _code_only(name: str) -> str:
    """Executable source with docstrings removed, so prose is not evidence."""
    tree = ast.parse(_module_path(name).read_text())
    for node in ast.walk(tree):
        if not isinstance(
            node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)
        ):
            continue
        body = node.body
        if (
            body
            and isinstance(body[0], ast.Expr)
            and isinstance(body[0].value, ast.Constant)
            and isinstance(body[0].value.value, str)
        ):
            node.body = body[1:] or [ast.Pass()]
    return ast.unparse(tree)


# ---------------------------------------------------------------------------
# The SUT cannot reach the evaluation layer
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("module", SUT_MODULES)
def test_the_sut_cannot_import_the_evaluation_layer(module: str) -> None:
    reachable = ({module} | _transitive_app_imports(module)) & EVALUATION_MODULES
    assert not reachable, f"{module} can reach {sorted(reachable)}"


@pytest.mark.parametrize(
    "module", ("optimizer", "estimator", "economics", "classifier", "policy")
)
@pytest.mark.parametrize(
    "evaluation_module",
    ("hidden_world", "benchmark_phase17", "outcome_model", "outcome", "benchmark"),
)
def test_no_decision_module_names_an_evaluation_module(
    module: str, evaluation_module: str
) -> None:
    source = _code_only(module)
    assert f"from .{evaluation_module}" not in source
    assert f"import {evaluation_module}" not in source


@pytest.mark.parametrize("module", SUT_MODULES)
def test_the_sut_never_names_hidden_ground_truth(module: str) -> None:
    source = _code_only(module)
    for term in GROUND_TRUTH_TERMS:
        assert term not in source, f"{module} references hidden truth: {term}"


def test_the_oracle_is_benchmark_only() -> None:
    """The Oracle is an upper bound to measure against, never a strategy."""
    for module in (*SUT_MODULES, "dashboard", "main", "ingestion", "webhook_service"):
        source = _code_only(module)
        for term in ("Oracle", "oracle"):
            assert term not in source, f"{module} references the Oracle: {term}"


def test_the_production_api_never_exposes_hidden_truth() -> None:
    for module in ("dashboard", "models", "classification", "policy", "executor"):
        source = _code_only(module)
        for term in GROUND_TRUTH_TERMS:
            assert term not in source, f"{module} would expose {term}"


# ---------------------------------------------------------------------------
# The hidden world cannot see the system under test
# ---------------------------------------------------------------------------


def test_the_hidden_world_does_not_depend_on_a_strategy() -> None:
    source = _code_only("hidden_world")
    for term in (
        "strategy",
        "select_intervention",
        "EconomicInterventionOptimizer",
        "RecoveryProbabilityEstimator",
        "selected_intervention",
    ):
        assert term not in source, f"hidden_world depends on the SUT: {term}"


def test_the_hidden_world_does_not_import_a_decision_module() -> None:
    forbidden = {"optimizer", "estimator", "classifier", "selector", "policy"}
    reachable = _app_imports("hidden_world") & forbidden
    # selector is imported only for the NO_ACTION constant, which is a shared
    # vocabulary term and carries no decision logic.
    assert reachable <= {"selector"}, f"hidden_world imports {sorted(reachable)}"
    assert "select_intervention" not in _code_only("hidden_world")


def test_the_hidden_world_uses_no_wall_clock_or_global_randomness() -> None:
    source = _code_only("hidden_world")
    for term in ("random.seed", "datetime.now", "utcnow", "time.time", "uuid"):
        assert term not in source, f"hidden_world is nondeterministic via {term}"
    assert "random" not in _imports("hidden_world")


def test_the_hidden_world_ignores_event_identity_features() -> None:
    """Identity may key the coin flip; it must never key the probability."""
    tree = ast.parse(_module_path("hidden_world").read_text())
    probability_functions = {
        "true_probability_bps",
        "_customer_history_bps",
        "true_expected_value_paise",
    }
    for node in ast.walk(tree):
        if not isinstance(node, ast.FunctionDef) or node.name not in probability_functions:
            continue
        body = ast.unparse(node)
        for identity in ("event_id", "order_id", "payment_id", "customer_id", "timestamp"):
            assert identity not in body, (
                f"{node.name} reads event identity: {identity}"
            )


# ---------------------------------------------------------------------------
# Hidden truth does not leak into ordinary result objects
# ---------------------------------------------------------------------------


def test_production_result_types_carry_no_hidden_fields() -> None:
    from app.classification import ClassificationResult
    from app.executor import ExecutionOutcome
    from app.models import PaymentEvent
    from app.optimizer import OptimizerDecision
    from app.policy import PolicyDecision

    forbidden = {
        "hidden_probability",
        "true_probability_bps",
        "true_ev_paise",
        "oracle_value",
        "oracle_true_ev_paise",
        "recovered",
    }
    for cls in (
        PaymentEvent,
        ClassificationResult,
        PolicyDecision,
        ExecutionOutcome,
        OptimizerDecision,
    ):
        assert not (set(getattr(cls, "__annotations__", {})) & forbidden), (
            f"{cls.__name__} carries hidden benchmark truth"
        )


def test_the_dashboard_payload_contains_no_hidden_truth() -> None:
    import json

    from app import db
    from app.dashboard import build_dashboard_summary

    conn = db.connect(":memory:")
    db.init_db(conn)
    try:
        payload = json.dumps(build_dashboard_summary(conn))
    finally:
        conn.close()
    for term in (
        "true_probability_bps",
        "true_ev_paise",
        "oracle",
        "hidden_probability",
        "draw_bps",
    ):
        assert term not in payload, f"the operator dashboard exposes {term}"


# ---------------------------------------------------------------------------
# Strategy selection cannot mutate the world
# ---------------------------------------------------------------------------


def test_asking_the_world_a_question_changes_no_answer() -> None:
    from app.economics import DEFAULT_ECONOMIC_MODEL
    from app.generator import generate_events
    from app.hidden_world import HiddenWorld

    events = generate_events(seed=42, count=50)
    world = HiddenWorld(outcome_seed=42, model=DEFAULT_ECONOMIC_MODEL)
    interventions = ("retry_immediate", "retry_delayed", "payment_link", "no_action")

    before = {
        (event.event_id, i): world.realize(event, i).to_dict()
        for event in events
        for i in interventions
    }
    # Simulate a full benchmark's worth of interrogation in a different order.
    for event in reversed(events):
        for i in reversed(interventions):
            world.realize(event, i)
            world.true_ev_paise(event, i)
    after = {
        (event.event_id, i): world.realize(event, i).to_dict()
        for event in events
        for i in interventions
    }
    assert before == after


def test_the_world_holds_no_mutable_per_event_state() -> None:
    from app.economics import DEFAULT_ECONOMIC_MODEL
    from app.hidden_world import HiddenWorld

    world = HiddenWorld(outcome_seed=42, model=DEFAULT_ECONOMIC_MODEL)
    assert not [
        name
        for name, value in vars(world).items()
        if isinstance(value, (dict, list, set))
    ]


# ---------------------------------------------------------------------------
# Phase 16 guarantees are preserved, not replaced
# ---------------------------------------------------------------------------


def test_the_phase_9_hidden_model_is_still_frozen() -> None:
    """Phase 17 adds a new world; it does not rewrite the historical one."""
    source = (APP_DIR / "outcome_model.py").read_text()
    assert "rng.random() for intervention in _DRAW_ORDER" in source
    for term in ("hidden_world", "benchmark_phase17", "optimizer", "estimator"):
        assert f"from .{term}" not in source


def test_the_v1_selector_priority_is_unchanged() -> None:
    from app.selector import INTERVENTION_PRIORITY

    assert INTERVENTION_PRIORITY == (
        "retry_delayed",
        "payment_link",
        "reminder",
        "alternate_method_prompt",
        "retry_immediate",
    )

"""Phase 16 tests: the V2 decision engine is structurally isolated.

Proves by source and import-graph inspection that the optimizer, estimator, and
economic model cannot reach the benchmark's hidden ground truth, cannot reach
the network or an LLM, cannot introduce nondeterminism, and cannot execute an
intervention. These are architectural guarantees, so they are asserted against
the code itself rather than against observed behaviour alone.
"""

from __future__ import annotations

import ast
import pathlib

import pytest

APP_DIR = pathlib.Path(__file__).resolve().parent.parent / "app"

# The V2 decision engine. Nothing here may depend on evaluation-layer state.
DECISION_ENGINE_MODULES: tuple[str, ...] = ("economics", "estimator", "optimizer")

# Modules owned by the evaluation layer, which holds the hidden outcome model.
EVALUATION_MODULES: frozenset[str] = frozenset(
    {"benchmark", "benchmark_metrics", "benchmark_store", "outcome", "outcome_model"}
)

# Sources of nondeterminism, I/O, and execution.
FORBIDDEN_STDLIB: frozenset[str] = frozenset(
    {
        "random",
        "secrets",
        "socket",
        "http",
        "urllib",
        "requests",
        "httpx",
        "razorpay",
        "time",
        "uuid",
        "os",
        "subprocess",
        "sqlite3",
    }
)

# Modules that perform execution or provider access.
EXECUTION_MODULES: frozenset[str] = frozenset(
    {"executor", "execution_service", "razorpay_client", "razorpay_webhook", "db"}
)


def _module_path(name: str) -> pathlib.Path:
    return APP_DIR / f"{name}.py"


def _imports(name: str) -> set[str]:
    """Return every module name imported by an app module, absolute or relative."""
    tree = ast.parse(_module_path(name).read_text())
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                found.add(alias.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom):
            if node.module:
                found.add(node.module.split(".")[0])
    return found


def _app_imports(name: str) -> set[str]:
    """Return the sibling app modules a module imports."""
    return {
        imported
        for imported in _imports(name)
        if _module_path(imported).exists()
    }


def _code_only(name: str) -> str:
    """Return a module's executable source with docstrings and comments removed.

    Prose that explains what the module deliberately does NOT do must not be
    mistaken for the module actually doing it.
    """
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
    # ast.unparse drops comments, so only real code remains.
    return ast.unparse(tree)


def _transitive_app_imports(name: str) -> set[str]:
    """Return the full transitive closure of app modules reachable from a module."""
    seen: set[str] = set()
    pending = [name]
    while pending:
        current = pending.pop()
        for imported in _app_imports(current):
            if imported not in seen:
                seen.add(imported)
                pending.append(imported)
    return seen


# ---------------------------------------------------------------------------
# Benchmark / hidden ground-truth isolation
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("module", DECISION_ENGINE_MODULES)
def test_decision_engine_does_not_import_the_evaluation_layer(module: str) -> None:
    assert not (_transitive_app_imports(module) & EVALUATION_MODULES), (
        f"{module} can reach the evaluation layer: "
        f"{sorted(_transitive_app_imports(module) & EVALUATION_MODULES)}"
    )


@pytest.mark.parametrize("module", DECISION_ENGINE_MODULES)
def test_decision_engine_never_names_hidden_ground_truth(module: str) -> None:
    """No hidden-probability lookup, benchmark label, or ground-truth access."""
    source = _code_only(module)
    for term in (
        "HiddenOutcomeModel",
        "hidden_outcome",
        "ground_truth",
        "generate_hidden_outcome_model",
        "RecoveryOutcome",
        "OutcomeSimulator",
        "recovered_amount_paise",
    ):
        assert term not in source, f"{module} references {term}"


@pytest.mark.parametrize("module", DECISION_ENGINE_MODULES)
def test_decision_engine_never_indexes_probabilities_by_event_id(module: str) -> None:
    """An event_id -> probability lookup is the exact shape of a truth leak."""
    tree = ast.parse(_module_path(module).read_text())
    for node in ast.walk(tree):
        if isinstance(node, ast.Subscript):
            target = ast.unparse(node.slice)
            assert "event_id" not in target, (
                f"{module} subscripts by event_id: {ast.unparse(node)}"
            )


def test_the_hidden_outcome_model_is_unchanged_by_phase_16() -> None:
    """Phase 16 must not touch benchmark ground-truth generation."""
    source = (APP_DIR / "outcome_model.py").read_text()
    assert "rng.random() for intervention in _DRAW_ORDER" in source
    for term in DECISION_ENGINE_MODULES:
        assert f"from .{term}" not in source
        assert f"import {term}" not in source


# ---------------------------------------------------------------------------
# Determinism, network, and LLM isolation
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("module", DECISION_ENGINE_MODULES)
def test_decision_engine_imports_no_nondeterminism_or_io(module: str) -> None:
    assert not (_imports(module) & FORBIDDEN_STDLIB), (
        f"{module} imports {sorted(_imports(module) & FORBIDDEN_STDLIB)}"
    )


@pytest.mark.parametrize("module", DECISION_ENGINE_MODULES)
def test_nondeterminism_and_io_are_unreachable_transitively(module: str) -> None:
    """A clean direct import list proves nothing if a dependency is dirty.

    Randomness, a clock, a socket, or a database driver reached through any
    intermediate app module would make the decision engine nondeterministic
    just as effectively as importing it directly.
    """
    for reachable in {module} | _transitive_app_imports(module):
        leaked = _imports(reachable) & FORBIDDEN_STDLIB
        assert not leaked, (
            f"{module} reaches {sorted(leaked)} through {reachable}"
        )


@pytest.mark.parametrize("module", DECISION_ENGINE_MODULES)
def test_decision_engine_makes_no_llm_call(module: str) -> None:
    """The LLM boundary must be unreachable, not merely unused."""
    assert "classifier" not in _transitive_app_imports(module)
    for term in ("OmniRoute", "omniroute"):
        assert term not in _code_only(module), (
            f"{module} references the LLM boundary: {term}"
        )


@pytest.mark.parametrize("module", DECISION_ENGINE_MODULES)
def test_decision_engine_uses_no_wall_clock_time(module: str) -> None:
    source = _code_only(module)
    for term in ("datetime.now", "utcnow", "time.time", "monotonic"):
        assert term not in source, f"{module} reads the clock: {term}"


@pytest.mark.parametrize("module", DECISION_ENGINE_MODULES)
def test_decision_engine_does_no_float_money_arithmetic(module: str) -> None:
    """Money is integer paise; no float literal may participate in it."""
    tree = ast.parse(_module_path(module).read_text())
    floats = [
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant) and isinstance(node.value, float)
    ]
    assert floats == [], f"{module} contains float literals: {floats}"


# ---------------------------------------------------------------------------
# The optimizer selects; it never executes and never authorizes
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("module", DECISION_ENGINE_MODULES)
def test_decision_engine_cannot_execute_or_persist(module: str) -> None:
    reachable = _transitive_app_imports(module) & EXECUTION_MODULES
    assert not reachable, f"{module} can reach execution/persistence: {sorted(reachable)}"


@pytest.mark.parametrize("module", DECISION_ENGINE_MODULES)
def test_decision_engine_never_names_the_executor(module: str) -> None:
    source = _code_only(module)
    for term in ("BoundedExecutor", "ExecutionOutcome", ".execute(", "razorpay"):
        assert term not in source, f"{module} references execution: {term}"


def test_the_optimizer_does_not_reimplement_policy_rules() -> None:
    """Denial logic belongs to the policy engine and is never duplicated."""
    source = _code_only("optimizer")
    for rule in (
        "fraud_suspect",
        "risk_flag",
        "cooldown_minutes",
        "max_interventions_per_customer",
        "daily_spend_cap",
        "has_successful_intervention",
        "PolicyEngine",
        "PolicyInput",
        "PolicyConfig",
        "PolicyHistory",
    ):
        assert rule not in source, f"optimizer duplicates policy logic: {rule}"


def test_the_optimizer_reads_only_the_allowed_flag_of_a_decision() -> None:
    """It consumes the policy verdict, never the reasoning behind it."""
    source = _code_only("optimizer")
    assert "decision.allowed" in source
    assert "denial_reason" not in source


# ---------------------------------------------------------------------------
# No silent failure
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("module", DECISION_ENGINE_MODULES)
def test_decision_engine_never_swallows_an_exception(module: str) -> None:
    tree = ast.parse(_module_path(module).read_text())
    for node in ast.walk(tree):
        if isinstance(node, ast.ExceptHandler):
            pytest.fail(f"{module} contains an exception handler: {ast.unparse(node)}")


# ---------------------------------------------------------------------------
# Phase 18: the audit contract inherits the decision engine's isolation
#
# The optimizer's decision is now recorded durably. The record must be able to
# describe the decision without acquiring any capability the decision engine
# itself is denied: it must not reach ground truth, must not become an
# execution path, and must not introduce a second economic calculation.
# ---------------------------------------------------------------------------

AUDIT_MODULE = "optimizer_audit"


def test_the_audit_contract_cannot_reach_the_evaluation_layer() -> None:
    reachable = _transitive_app_imports(AUDIT_MODULE) & EVALUATION_MODULES
    assert not reachable, f"{AUDIT_MODULE} can reach {sorted(reachable)}"


def test_the_audit_contract_never_names_hidden_ground_truth() -> None:
    source = _code_only(AUDIT_MODULE)
    for term in (
        "HiddenOutcomeModel",
        "hidden_outcome",
        "hidden_probability",
        "true_probability",
        "true_expected_value",
        "ground_truth",
        "oracle",
        "Oracle",
        "outcome_draw",
        "RecoveryOutcome",
        "OutcomeSimulator",
        "recovered_amount_paise",
    ):
        assert term not in source, f"{AUDIT_MODULE} references {term}"


def test_the_audit_contract_cannot_execute_or_reach_a_provider() -> None:
    reachable = _transitive_app_imports(AUDIT_MODULE) & EXECUTION_MODULES
    assert not reachable, (
        f"{AUDIT_MODULE} can reach execution/persistence: {sorted(reachable)}"
    )
    source = _code_only(AUDIT_MODULE)
    for term in ("BoundedExecutor", "ExecutionOutcome", ".execute(", "razorpay"):
        assert term not in source, f"{AUDIT_MODULE} references execution: {term}"


def test_the_audit_contract_introduces_no_nondeterminism_or_io() -> None:
    for reachable in {AUDIT_MODULE} | _transitive_app_imports(AUDIT_MODULE):
        leaked = _imports(reachable) & FORBIDDEN_STDLIB
        assert not leaked, (
            f"{AUDIT_MODULE} reaches {sorted(leaked)} through {reachable}"
        )


def test_the_audit_contract_does_not_reimplement_the_economic_equation() -> None:
    """It records the optimizer's arithmetic; it must never redo it.

    A second implementation of the expected-value equation would let the audit
    trail disagree with the decision it claims to describe.
    """
    tree = ast.parse(_module_path(AUDIT_MODULE).read_text())
    for node in ast.walk(tree):
        if isinstance(node, ast.BinOp) and isinstance(
            node.op, (ast.Add, ast.Sub, ast.Mult, ast.FloorDiv, ast.Div, ast.Mod)
        ):
            pytest.fail(
                f"{AUDIT_MODULE} performs arithmetic: {ast.unparse(node)}"
            )
    source = _code_only(AUDIT_MODULE)
    for term in ("evaluate_candidate", "EconomicModel", "PROBABILITY_SCALE"):
        assert term not in source, f"{AUDIT_MODULE} recomputes economics: {term}"


def test_the_audit_contract_never_swallows_an_exception() -> None:
    """It may translate a failure, but it may never absorb one."""
    tree = ast.parse(_module_path(AUDIT_MODULE).read_text())
    for node in ast.walk(tree):
        if not isinstance(node, ast.ExceptHandler):
            continue
        raises = any(isinstance(inner, ast.Raise) for inner in ast.walk(node))
        assert raises, f"{AUDIT_MODULE} swallows: {ast.unparse(node)}"


def test_the_audit_contract_cannot_reach_policy_rules() -> None:
    """It records that policy allowed something, never why or whether."""
    source = _code_only(AUDIT_MODULE)
    for term in ("PolicyEngine", "PolicyInput", "PolicyConfig", "denial_reason"):
        assert term not in source, f"{AUDIT_MODULE} duplicates policy logic: {term}"

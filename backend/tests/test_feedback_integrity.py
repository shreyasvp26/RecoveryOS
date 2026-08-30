"""Phase 22 integrity tests — the feedback layer must acquire no authority.

These are structural tests. They assert what the Phase 22 modules are allowed
to depend on, because the dangerous failure here is not a wrong number: it is
a measurement layer that quietly gains the ability to execute, to authorize,
or to read benchmark ground truth.
"""

from __future__ import annotations

import ast
import inspect
from pathlib import Path

import pytest

from app import outcome_feedback, recovery_intelligence
from app.routes import intelligence

PHASE_22_MODULES = (outcome_feedback, recovery_intelligence, intelligence)

# Modules the feedback layer must never reach, and why.
FORBIDDEN_IMPORTS: dict[str, str] = {
    "app.hidden_world": "hidden benchmark ground truth",
    "app.outcome_model": "hidden benchmark outcome probabilities",
    "app.outcome": "simulated benchmark recovery draws",
    "app.benchmark": "benchmark evaluation",
    "app.benchmark_simulation": "benchmark simulation",
    "app.benchmark_phase17": "benchmark evaluation",
    "app.executor": "execution authority",
    "app.execution_service": "execution authority",
    "app.razorpay_webhook": "webhook verification authority",
    "app.webhook_service": "webhook processing authority",
    "app.policy": "policy authority",
    "app.policy_scenario": "policy simulation authority",
    "app.estimator": "recovery probability estimation",
    "app.optimizer": "economic decision authority",
    "app.classifier": "diagnosis authority",
    "app.replay": "policy replay simulation",
}


def _imported_modules(module) -> set[str]:
    """Every module name a Phase 22 module imports, resolved to app.* form."""
    source = Path(inspect.getfile(module)).read_text(encoding="utf-8")
    tree = ast.parse(source)
    package = module.__name__.rsplit(".", 1)[0]
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                base = package
                for _ in range(node.level - 1):
                    base = base.rsplit(".", 1)[0]
                resolved = f"{base}.{node.module}" if node.module else base
            else:
                resolved = node.module or ""
            names.add(resolved)
            for alias in node.names:
                names.add(f"{resolved}.{alias.name}")
    return names


@pytest.mark.parametrize("module", PHASE_22_MODULES, ids=lambda m: m.__name__)
def test_feedback_modules_import_no_forbidden_authority(module):
    imported = _imported_modules(module)
    for forbidden, why in FORBIDDEN_IMPORTS.items():
        assert forbidden not in imported, (
            f"{module.__name__} imports {forbidden} ({why}); the feedback "
            "layer must not acquire it"
        )


@pytest.mark.parametrize("module", PHASE_22_MODULES, ids=lambda m: m.__name__)
def test_feedback_modules_reference_no_benchmark_symbols(module):
    """No benchmark vocabulary appears in Phase 22 source at all."""
    source = Path(inspect.getfile(module)).read_text(encoding="utf-8").lower()
    for needle in ("hidden_world", "hiddenoutcomemodel", "outcome_model", "ground_truth"):
        assert needle not in source, (
            f"{module.__name__} references {needle}; benchmark truth must never "
            "reach operational feedback"
        )


def test_feedback_modules_never_write_to_the_database():
    """Every db call the feedback layer makes is a read."""
    for module in (outcome_feedback, recovery_intelligence):
        source = Path(inspect.getfile(module)).read_text(encoding="utf-8")
        for forbidden in ("insert_", "update_", "upsert_", "delete", "claim_", "commit"):
            assert forbidden not in source, (
                f"{module.__name__} appears to mutate persisted state via "
                f"{forbidden!r}"
            )


def test_the_intelligence_router_exposes_only_get():
    methods = {
        method
        for route in intelligence.router.routes
        for method in getattr(route, "methods", set())
    }
    assert methods <= {"GET", "HEAD"}, f"unexpected write methods: {methods}"


def test_the_intelligence_route_is_registered_on_the_app():
    from app.main import app as fastapi_app

    paths = fastapi_app.openapi()["paths"]
    assert "/recovery-intelligence" in paths
    assert set(paths["/recovery-intelligence"]) == {"get"}


def test_simulated_execution_can_never_become_an_observed_recovery():
    """Structural: the recovered branch is unreachable for a simulated mode."""
    observation = outcome_feedback.build_observation(
        {
            "event_id": "evt",
            "amount_paise": 100_000,
            "payment_method": "upi",
            "bank": "HDFC",
            "failure_reason": "bank_timeout",
        },
        {
            "event_id": "evt",
            "intervention": "payment_link",
            "execution_mode": "SIMULATED",
            "status": "SUCCESS",
            # Even if a link id and a matching verified recovery were somehow
            # present, a simulated execution yields no operational observation.
            "payment_link_id": "plink",
            "detail": None,
            "reported_at": "2026-08-30T09:00:00+00:00",
        },
        [],
        {
            "plink": {
                "delivery_id": "delivery",
                "payment_link_id": "plink",
                "amount_paid_paise": 100_000,
                "recovered_at": "2026-08-30T09:30:00+00:00",
            }
        },
    )
    assert observation.calibration_eligible is False
    assert observation.verified_recovery is False
    assert observation.terminal is False
    assert observation.reason == outcome_feedback.REASON_SIMULATED_EXECUTION
    assert observation.recovered is None
    assert observation.recovered_amount_paise is None


def test_estimator_and_optimizer_state_is_untouched_by_analytics(db_conn):
    """Reading intelligence changes no module-level decision configuration."""
    from app import economics, estimator

    before = (
        dict(economics.DEFAULT_ECONOMIC_MODEL.assumptions),
        repr(getattr(estimator, "DEFAULT_ESTIMATOR_MODEL", None)),
    )
    recovery_intelligence.build_recovery_intelligence(db_conn)
    after = (
        dict(economics.DEFAULT_ECONOMIC_MODEL.assumptions),
        repr(getattr(estimator, "DEFAULT_ESTIMATOR_MODEL", None)),
    )
    assert before == after

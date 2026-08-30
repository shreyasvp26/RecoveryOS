"""Phase 23 integrity tests — the estimator-evidence layer acquires no authority.

These are structural tests. The dangerous failure for the calibration layer is
not a wrong posterior: it is a measurement layer that quietly gains the ability
to execute, to authorize, or to read benchmark ground truth. The calibration and
adaptive-estimator modules may only depend on the frozen estimator/economics
taxonomy, calibration evidence, and persistence reads/writes of immutable
snapshots — never on any execution or decision authority.
"""

from __future__ import annotations

import ast
import inspect
from pathlib import Path

import pytest

from app import adaptive_estimation, calibration, calibration_service

PHASE_23_MODULES = (calibration, adaptive_estimation, calibration_service)

# Authority / simulation modules the calibration layer must never reach.
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
    "app.policy_scenario": "policy simulation authority",
    "app.policy": "policy authorization boundary",
    "app.optimizer": "economic decision authority",
    "app.classifier": "diagnosis authority",
    "app.replay": "policy replay simulation",
}


def _imported_modules(module) -> set[str]:
    """Every module name a Phase 23 module imports, resolved to app.* form."""
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


@pytest.mark.parametrize("module", PHASE_23_MODULES, ids=lambda m: m.__name__)
def test_calibration_modules_import_no_forbidden_authority(module):
    imported = _imported_modules(module)
    for forbidden, why in FORBIDDEN_IMPORTS.items():
        assert forbidden not in imported, (
            f"{module.__name__} imports {forbidden} ({why}); the calibration "
            "layer must not acquire it"
        )


def test_calibration_service_depends_only_on_persistence_and_calibration():
    imported = _imported_modules(calibration_service)
    # It may persist immutable evidence/snapshots via db and reuse the pure
    # calibration module; nothing else from the execution/decision chain.
    allowed_prefixes = ("app.db", "app.calibration", "app.adaptive_estimation")
    suspicious = {
        name for name in imported if name.startswith("app.") and not name.startswith(allowed_prefixes)
    }
    assert suspicious == set(), sorted(suspicious)


@pytest.mark.parametrize("module", PHASE_23_MODULES, ids=lambda m: m.__name__)
def test_calibration_modules_reference_no_benchmark_symbols(module):
    source = Path(inspect.getfile(module)).read_text(encoding="utf-8").lower()
    for needle in ("hidden_world", "outcome_model", "ground_truth", "hiddenoutcome"):
        assert needle not in source, (
            f"{module.__name__} references {needle}; benchmark truth must never "
            "reach operational calibration"
        )


def test_calibration_modules_offer_no_execute_or_authorize_surface():
    for module in PHASE_23_MODULES:
        for attr in ("execute", "authorize", "authorize_decision", "run_policy"):
            assert not hasattr(module, attr), (
                f"{module.__name__} surprisingly exposes {attr!r}"
            )

"""Phase 17 tests: batch execution is simulated, offline, and authorized.

The central regression here is ``payment_link``: before Phase 17 a credential-
less batch run recorded every payment link as ``configuration_missing``/FAILED,
which silently deleted an entire intervention from the comparison. These tests
pin the repair AND pin the boundary that must not move with it — the production
Razorpay path stays exactly as it was.
"""

from __future__ import annotations

import ast
import pathlib

import pytest

from app.benchmark_simulation import (
    SIMULATED,
    STATUS_SUCCESS,
    SimulatedAuthorizationError,
    SimulatedExecutionError,
    SimulatedExecutor,
)
from app.economics import EXECUTABLE_INTERVENTIONS
from app.models import CustomerHistory, PaymentEvent
from app.policy import PolicyDecision
from app.selector import NO_ACTION

APP_DIR = pathlib.Path(__file__).resolve().parent.parent / "app"

EXECUTABLE = (
    "retry_immediate",
    "retry_delayed",
    "reminder",
    "alternate_method_prompt",
    "payment_link",
)


def event(event_id: str = "evt_sim_0001") -> PaymentEvent:
    return PaymentEvent(
        event_id=event_id,
        order_id="order_sim_0001",
        payment_id="pay_sim_0001",
        customer_id="cust_0001",
        amount_paise=250_000,
        currency="INR",
        payment_method="card",
        failure_reason="bank_timeout",
        bank="HDFC",
        risk_flag="normal",
        customer_history=CustomerHistory(
            prior_successful_payments=4,
            prior_failed_payments=1,
            has_active_subscription=False,
        ),
        timestamp="2026-08-01T10:00:00+00:00",
    )


def allow(intervention: str, event_id: str = "evt_sim_0001") -> PolicyDecision:
    return PolicyDecision(
        event_id=event_id,
        proposed_intervention=intervention,
        allowed=True,
        denial_reason=None,
        policy_rules_applied=("fraud_check_passed",),
        evaluated_at="2026-08-27T13:00:00+00:00",
    )


def deny(intervention: str, event_id: str = "evt_sim_0001") -> PolicyDecision:
    return PolicyDecision(
        event_id=event_id,
        proposed_intervention=intervention,
        allowed=False,
        denial_reason="fraud_protection",
        policy_rules_applied=("fraud_protection",),
        evaluated_at="2026-08-27T13:00:00+00:00",
    )


# ---------------------------------------------------------------------------
# Every executable intervention is simulatable
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("intervention", EXECUTABLE)
def test_every_executable_intervention_simulates(intervention: str) -> None:
    result = SimulatedExecutor().execute(event(), intervention, allow(intervention))
    assert result.execution_mode == SIMULATED
    assert result.status == STATUS_SUCCESS
    assert result.authorized is True


def test_the_simulator_covers_the_whole_executable_taxonomy() -> None:
    assert set(EXECUTABLE) == set(EXECUTABLE_INTERVENTIONS)


def test_payment_link_simulates_without_any_razorpay_credential(monkeypatch) -> None:
    """The Phase 17 payment-link regression, stated exactly.

    No client, no credentials in the environment, no network — and the result
    is a valid SIMULATED execution rather than ``configuration_missing``.
    """
    for name in ("RAZORPAY_KEY_ID", "RAZORPAY_KEY_SECRET", "RAZORPAY_WEBHOOK_SECRET"):
        monkeypatch.delenv(name, raising=False)
    result = SimulatedExecutor().execute(
        event(), "payment_link", allow("payment_link")
    )
    assert result.execution_mode == SIMULATED
    assert result.status == STATUS_SUCCESS
    assert result.detail is None


def test_a_simulated_execution_can_never_claim_a_provider_mode() -> None:
    from app.benchmark_simulation import SimulatedExecution

    with pytest.raises(SimulatedExecutionError):
        SimulatedExecution(
            event_id="evt_sim_0001",
            intervention="payment_link",
            execution_mode="REAL_RAZORPAY",
            status=STATUS_SUCCESS,
            authorized=True,
        )


# ---------------------------------------------------------------------------
# Offline by construction
# ---------------------------------------------------------------------------


def _imports(module: str) -> set[str]:
    tree = ast.parse((APP_DIR / f"{module}.py").read_text())
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            found.add(node.module.split(".")[0])
    return found


@pytest.mark.parametrize(
    "module", ("benchmark_simulation", "benchmark_phase17", "hidden_world")
)
def test_the_benchmark_cannot_reach_a_payment_provider(module: str) -> None:
    forbidden = {
        "razorpay",
        "razorpay_client",
        "razorpay_webhook",
        "requests",
        "httpx",
        "urllib",
        "socket",
        "http",
    }
    assert not (_imports(module) & forbidden), (
        f"{module} can reach a provider or the network: "
        f"{sorted(_imports(module) & forbidden)}"
    )


def test_forcing_a_real_razorpay_path_through_the_simulator_is_impossible() -> None:
    """There is no argument, flag, or client parameter that could enable one."""
    import inspect

    parameters = set(inspect.signature(SimulatedExecutor.execute).parameters)
    assert not parameters & {"razorpay_client", "client", "execution_mode", "mode"}


# ---------------------------------------------------------------------------
# Authorization boundary
# ---------------------------------------------------------------------------


def test_a_denied_intervention_is_never_simulated() -> None:
    with pytest.raises(SimulatedAuthorizationError):
        SimulatedExecutor().execute(event(), "payment_link", deny("payment_link"))


def test_a_missing_decision_is_never_simulated() -> None:
    with pytest.raises(SimulatedAuthorizationError):
        SimulatedExecutor().execute(event(), "retry_delayed", None)


def test_a_decision_for_another_event_authorizes_nothing() -> None:
    with pytest.raises(SimulatedAuthorizationError):
        SimulatedExecutor().execute(
            event(), "retry_delayed", allow("retry_delayed", "evt_elsewhere")
        )


def test_a_decision_for_another_intervention_authorizes_nothing() -> None:
    with pytest.raises(SimulatedAuthorizationError):
        SimulatedExecutor().execute(event(), "payment_link", allow("retry_delayed"))


def test_bypassing_authorization_is_explicit_and_permanently_recorded() -> None:
    """The naive baseline's escape hatch cannot be used without leaving a mark."""
    result = SimulatedExecutor().execute(
        event(), "retry_immediate", None, require_authorization=False
    )
    assert result.authorized is False


def test_no_action_is_never_executed() -> None:
    with pytest.raises(SimulatedExecutionError):
        SimulatedExecutor().execute(event(), NO_ACTION, None)


def test_an_unknown_intervention_is_rejected() -> None:
    with pytest.raises(SimulatedExecutionError):
        SimulatedExecutor().execute(event(), "call_the_customer", None)


# ---------------------------------------------------------------------------
# The production path is untouched
# ---------------------------------------------------------------------------


def test_the_production_executor_still_pins_payment_link_to_razorpay() -> None:
    from app.executor import BoundedExecutor

    outcome = BoundedExecutor().execute(
        event(), "payment_link", allow("payment_link"), razorpay_client=None
    )
    assert outcome.execution_mode == "REAL_RAZORPAY"
    assert outcome.status == "FAILED"
    assert outcome.detail is not None
    assert outcome.detail.startswith("configuration_missing")


def test_the_production_executor_does_not_import_the_simulator() -> None:
    for module in ("executor", "execution_service", "razorpay_client"):
        assert "benchmark_simulation" not in _imports(module)

"""Phase 8 integrity tests: hidden ground truth never reaches the decision path.

The System Under Test (classifier -> policy -> selector -> executor -> Razorpay
boundary) must never see hidden recovery probabilities. These tests enforce
that contract behaviorally and at the source/API level.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.classification import CANDIDATE_INTERVENTIONS, ClassificationResult
from app.classifier import SYSTEM_INSTRUCTION, build_classifier_input, build_prompt
from app.config import build_policy_config
from app.executor import BoundedExecutor
from app.generator import generate_events
from app.main import app
from app.models import CustomerHistory, PaymentEvent
from app.outcome import OutcomeSimulator, RecoveryOutcome
from app.outcome_model import HiddenOutcomeModel, generate_hidden_outcome_model
from app.policy import PolicyDecision, PolicyEngine, PolicyHistory, PolicyInput
from app.razorpay_client import PaymentLinkResult
from app.routes.events import get_classifier, get_now, get_razorpay_client
from app.selector import NO_ACTION, select_intervention

client = TestClient(app)

EVALUATION_TIME = datetime(2026, 8, 27, 13, 0, tzinfo=timezone.utc)

# Terms that belong exclusively to the evaluation layer.
_HIDDEN_TERMS: tuple[str, ...] = (
    "benchmark",
    "best_intervention",
    "ground_truth",
    "hidden",
    "outcome_model",
    "recovered",
    "recovery_probability",
    "simulated_revenue",
    "strategy_ground_truth",
    "true_outcome",
)

_SUT_MODULES: tuple[str, ...] = (
    "classifier",
    "classification",
    "executor",
    "execution_service",
    "policy",
    "razorpay_client",
    "routes/events",
    "selector",
)

# The only app modules the evaluation boundary may import from.
_EVALUATION_IMPORT_WHITELIST: tuple[str, ...] = (
    "classification",
    "models",
    "outcome_model",
)

# Interventions the executor can run without a payment provider.
_RUNNABLE: tuple[str, ...] = (
    "retry_immediate",
    "retry_delayed",
    "reminder",
    "alternate_method_prompt",
)


def _event(event_id: str, risk_flag: str = "normal") -> PaymentEvent:
    return PaymentEvent(
        event_id=event_id,
        order_id=f"order_{event_id}",
        payment_id=f"pay_{event_id}",
        customer_id=f"cust_{event_id}",
        amount_paise=75000,
        currency="INR",
        payment_method="card",
        failure_reason="bank_timeout",
        bank="HDFC",
        risk_flag=risk_flag,
        customer_history=CustomerHistory(4, 1, True),
        timestamp="2026-08-27T12:00:00+00:00",
    )


def _classification(event_id: str, root: str = "transient") -> ClassificationResult:
    return ClassificationResult(
        event_id=event_id,
        root_cause_category=root,
        confidence=0.9,
        reasoning="transient bank timeout; retry or send a payment link.",
        candidate_interventions=tuple(
            sorted(CANDIDATE_INTERVENTIONS - {NO_ACTION})
        ),
    )


def _empty_history() -> PolicyHistory:
    return PolicyHistory(
        customer_intervention_count_24h=0,
        most_recent_event_intervention_time=None,
        has_successful_intervention=False,
        existing_daily_spend_paise=0,
    )


def _decisions_for(event, classification) -> dict[str, PolicyDecision]:
    config = build_policy_config()
    return {
        intervention: PolicyEngine().evaluate(
            PolicyInput(
                event=event,
                classification=classification,
                proposed_intervention=intervention,
                history=_empty_history(),
                evaluation_time=EVALUATION_TIME,
            ),
            config,
        )
        for intervention in sorted(CANDIDATE_INTERVENTIONS - {NO_ACTION})
    }


def _module_source(namespace: str) -> str:
    root = Path(__file__).resolve().parent.parent / "app"
    return (root / f"{namespace}.py").read_text()


def _assert_no_hidden_terms(text: str, context: str) -> None:
    lowered = text.lower()
    for term in _HIDDEN_TERMS:
        assert term not in lowered, (
            f"{context} exposes hidden-ground-truth term {term!r}"
        )


def _collect_keys(obj: object, keys: list[str]) -> None:
    if isinstance(obj, dict):
        for key in obj:
            keys.append(str(key))
            _collect_keys(obj[key], keys)
    elif isinstance(obj, list):
        for item in obj:
            _collect_keys(item, keys)


# ---------------------------------------------------------------------------
# Classifier isolation
# ---------------------------------------------------------------------------


def test_classifier_input_has_no_hidden_ground_truth() -> None:
    event = _event("evt_classifier_iso")
    payload = build_classifier_input(event)
    _assert_no_hidden_terms(json.dumps(payload), "classifier input")
    assert set(payload) <= set(event.to_dict())


def test_classifier_prompt_and_instruction_are_outcome_free() -> None:
    event = _event("evt_prompt_iso")
    prompt = build_prompt(build_classifier_input(event))
    _assert_no_hidden_terms(prompt, "classifier prompt")
    _assert_no_hidden_terms(SYSTEM_INSTRUCTION, "system instruction")
    assert "future outcome" in SYSTEM_INSTRUCTION


# ---------------------------------------------------------------------------
# Source-level isolation
# ---------------------------------------------------------------------------


def test_sut_modules_never_import_or_reference_evaluation_layer() -> None:
    references = (
        "OutcomeSimulator",
        "generate_hidden_outcome_model",
        "HiddenOutcomeModel",
        "OutcomeModelError",
        "RecoveryOutcome",
        "simulate_intervention_outcome",
    )
    for namespace in _SUT_MODULES:
        source = _module_source(namespace)
        assert "from .outcome" not in source, namespace
        assert "from ..outcome" not in source, namespace
        assert "from app.outcome" not in source, namespace
        for reference in references:
            assert reference not in source, namespace


def test_evaluation_modules_import_only_from_whitelist() -> None:
    for namespace in ("outcome", "outcome_model"):
        source = _module_source(namespace)
        for line in source.splitlines():
            stripped = line.strip()
            match = re.match(r"from \.(\w+)", stripped)
            if match:
                imported = match.group(1)
                assert imported in _EVALUATION_IMPORT_WHITELIST, (
                    f"{namespace} imports non-whitelisted module {imported!r}"
                )


# ---------------------------------------------------------------------------
# Policy isolation: the hidden model never influences authorization
# ---------------------------------------------------------------------------


def test_policy_decisions_are_unchanged_by_hidden_model() -> None:
    event = _event("evt_policy_iso")
    classification = _classification(event.event_id)

    def decisions() -> list[dict]:
        return [
            decision.to_dict()
            for decision in _decisions_for(event, classification).values()
        ]

    before = decisions()
    generate_hidden_outcome_model([event], 42)
    after = decisions()
    assert before == after


def test_fraud_and_terminal_are_denied_even_with_certain_hidden_recovery() -> None:
    config = build_policy_config()
    for risk_flag, root in (("fraud_suspect", "transient"), ("normal", "terminal")):
        event = _event(f"evt_deny_{risk_flag}_{root}", risk_flag=risk_flag)
        classification = _classification(event.event_id, root=root)
        certain = HiddenOutcomeModel(
            seed=1,
            probabilities={
                event.event_id: {
                    intervention: 1.0 for intervention in CANDIDATE_INTERVENTIONS
                }
            },
        )
        assert certain.recovery_probability(event.event_id, "retry_delayed") == 1.0
        for intervention in sorted(CANDIDATE_INTERVENTIONS - {NO_ACTION}):
            decision = PolicyEngine().evaluate(
                PolicyInput(
                    event=event,
                    classification=classification,
                    proposed_intervention=intervention,
                    history=_empty_history(),
                    evaluation_time=EVALUATION_TIME,
                ),
                config,
            )
            assert decision.allowed is False
            assert decision.denial_reason in ("fraud_protection", "terminal_failure")


def test_possible_recovery_never_authorizes_no_action_selection() -> None:
    event = _event("evt_deny_select", risk_flag="fraud_suspect")
    classification = _classification(event.event_id)
    high_recovery = HiddenOutcomeModel(
        seed=1,
        probabilities={
            event.event_id: {i: 0.99 for i in CANDIDATE_INTERVENTIONS}
        },
    )
    assert high_recovery.recovery_probability(event.event_id, "retry_delayed") == 0.99

    decisions = _decisions_for(event, classification)
    selection = select_intervention(classification.candidate_interventions, decisions)
    assert selection.selected_intervention == NO_ACTION
    assert selection.is_actionable is False


# ---------------------------------------------------------------------------
# Selector isolation
# ---------------------------------------------------------------------------


def test_selection_is_unchanged_by_hidden_model() -> None:
    event = _event("evt_selector_iso")
    classification = _classification(event.event_id)

    def select() -> str:
        return select_intervention(
            classification.candidate_interventions,
            _decisions_for(event, classification),
        ).selected_intervention

    assert select() == select()
    generate_hidden_outcome_model([event], 42)
    assert select() == select()


def test_nothing_ever_executes_for_no_action() -> None:
    event = _event("evt_never_exec")
    classification = _classification(event.event_id)
    decisions = _decisions_for(event, classification)
    selection = select_intervention(classification.candidate_interventions, decisions)
    assert selection.is_actionable is True
    assert selection.selected_intervention != NO_ACTION

    model = generate_hidden_outcome_model([event], 42)
    outcome = OutcomeSimulator(model).simulate(event, NO_ACTION)
    assert outcome.intervention == NO_ACTION
    assert isinstance(outcome.recovered, bool)


# ---------------------------------------------------------------------------
# Executor isolation: execution success is not recovery success
# ---------------------------------------------------------------------------


def test_executor_result_is_unchanged_by_hidden_model() -> None:
    event = _event("evt_executor_iso")
    intervention = "retry_delayed"
    decision = _decisions_for(event, _classification(event.event_id))[intervention]
    before = BoundedExecutor().execute(event, intervention, decision, None).to_dict()
    generate_hidden_outcome_model([event], 42)
    after = BoundedExecutor().execute(event, intervention, decision, None).to_dict()
    assert before == after
    assert after["status"] == "SUCCESS"
    _assert_no_hidden_terms(json.dumps(after), "execution outcome")


def test_execution_success_does_not_imply_recovery() -> None:
    events = [_event(f"evt_proof_{i}") for i in range(24)]
    model = generate_hidden_outcome_model(events, 20260828)
    simulator = OutcomeSimulator(model)

    simulated: list[tuple[PaymentEvent, str, RecoveryOutcome]] = [
        (event, intervention, simulator.simulate(event, intervention))
        for event in events
        for intervention in _RUNNABLE
    ]
    assert any(outcome.recovered for _, _, outcome in simulated)
    assert any(not outcome.recovered for _, _, outcome in simulated)

    event, intervention = next(
        (event, intervention)
        for event, intervention, outcome in simulated
        if not outcome.recovered
    )
    decision = _decisions_for(event, _classification(event.event_id))[intervention]
    execution = BoundedExecutor().execute(event, intervention, decision, None)
    assert execution.status == "SUCCESS"
    assert execution.execution_mode == "SIMULATED"
    assert execution.payment_link_id is None
    assert simulator.simulate(event, intervention).recovered is False


# ---------------------------------------------------------------------------
# API isolation
# ---------------------------------------------------------------------------


class StubClassifier:
    def __init__(self, responses: list[str]) -> None:
        self.responses = list(responses)

    def generate(self, prompt: str) -> str:
        return self.responses.pop(0)


class StubPaymentLinkClient:
    def create_payment_link(self, **kwargs):
        return PaymentLinkResult(id="plink_iso", short_url="https://rzp.io/l/iso123")


@pytest.fixture(autouse=True)
def _reset_dependency_overrides():
    yield
    app.dependency_overrides.clear()


def _seed_api_event(monkeypatch, tmp_path, risk_flag: str = "normal") -> None:
    monkeypatch.setenv(
        "DATABASE_URL", f"sqlite:///{tmp_path / 'iso_integrity.db'}"
    )
    event_payload = {
        "event_id": "evt_iso_api",
        "order_id": "order_iso_api",
        "payment_id": "pay_iso_api",
        "customer_id": "cust_iso_api",
        "amount_paise": 75000,
        "currency": "INR",
        "payment_method": "card",
        "failure_reason": "bank_timeout",
        "bank": "HDFC",
        "risk_flag": risk_flag,
        "customer_history": {
            "prior_successful_payments": 4,
            "prior_failed_payments": 1,
            "has_active_subscription": True,
        },
        "timestamp": "2026-08-27T12:00:00+00:00",
    }
    response = client.post("/events", json=event_payload)
    assert response.status_code == 201


def test_api_responses_never_expose_hidden_ground_truth(
    monkeypatch, tmp_path
) -> None:
    _seed_api_event(monkeypatch, tmp_path)
    classification = {
        "event_id": "evt_iso_api",
        "root_cause_category": "transient",
        "confidence": 0.9,
        "reasoning": "transient bank timeout; retry or send a payment link.",
        "candidate_interventions": ["retry_delayed", "payment_link"],
    }
    app.dependency_overrides[get_classifier] = lambda: StubClassifier(
        [json.dumps(classification)]
    )
    app.dependency_overrides[get_now] = lambda: EVALUATION_TIME
    app.dependency_overrides[get_razorpay_client] = lambda: None

    bodies: list[tuple[str, dict]] = []
    classify = client.post("/events/evt_iso_api/classify")
    assert classify.status_code == 200
    bodies.append(("classify", classify.json()))

    policy_response = client.post(
        "/events/evt_iso_api/policy",
        json={"proposed_intervention": "retry_delayed"},
    )
    assert policy_response.status_code == 200
    bodies.append(("policy", policy_response.json()))

    execute_response = client.post("/events/evt_iso_api/execute")
    assert execute_response.status_code == 200
    bodies.append(("execute", execute_response.json()))

    for path, body in bodies:
        keys: list[str] = []
        _collect_keys(body, keys)
        lowered = json.dumps(body).lower()
        for key in keys:
            assert key not in _HIDDEN_TERMS and "recovery_probability" not in key
        _assert_no_hidden_terms(lowered, path)


# ---------------------------------------------------------------------------
# End-to-end regeneration (manual verification G shape, in-memory)
# ---------------------------------------------------------------------------


def test_full_harness_regeneration_is_deterministic_without_manual_records() -> None:
    def regenerate(seed: int) -> dict[str, object]:
        events = generate_events(seed=seed, count=25)
        model = generate_hidden_outcome_model(events, seed)
        simulator = OutcomeSimulator(model)
        by_id = {event.event_id: event for event in events}
        outcomes = [
            simulator.simulate(by_id[event_id], intervention).to_dict()
            for event_id in sorted(by_id)
            for intervention in sorted(CANDIDATE_INTERVENTIONS)
        ]
        return {"model": model.to_dict(), "outcomes": outcomes}

    assert regenerate(20260828) == regenerate(20260828)
    assert regenerate(20260828) != regenerate(20260829)

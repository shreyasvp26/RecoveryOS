"""Phase 5 tests for the AI classifier boundary (stubbed model layer)."""

from __future__ import annotations

import json

import httpx
import pytest

from app.classifier import (
    ClassificationValidationError,
    OmniRouteClassifier,
    OmniRouteError,
    build_classifier_input,
    build_omniroute_adapter,
    build_prompt,
    classify_event,
)
from app.generator import generate_events

_LOCKED_EVENT_FIELDS = frozenset(
    {
        "event_id",
        "order_id",
        "payment_id",
        "customer_id",
        "amount_paise",
        "currency",
        "payment_method",
        "failure_reason",
        "bank",
        "risk_flag",
        "customer_history",
        "timestamp",
    }
)

_FORBIDDEN_FUTURE_FIELDS = frozenset(
    {
        "true_recovery_probability",
        "recovery_probability",
        "best_intervention",
        "true_outcome",
        "benchmark_score",
        "simulated_revenue",
        "recovery_amount",
        "recovery_label",
        "strategy_ground_truth",
        "expected_recovery",
    }
)

VALID_RESULT = {
    "event_id": "evt_000001",
    "root_cause_category": "transient",
    "confidence": 0.9,
    "reasoning": "The payment gateway returned a transient timeout.",
    "candidate_interventions": ["retry_delayed", "payment_link"],
}


class StubAdapter:
    """Return a fixed sequence of raw model outputs (raises when exhausted)."""

    def __init__(self, *responses: str) -> None:
        self.responses = list(responses)
        self.calls = 0

    def generate(self, prompt: str) -> str:
        self.calls += 1
        if not self.responses:
            raise OmniRouteError("stub adapter exhausted")
        return self.responses.pop(0)


def event() -> object:
    return generate_events(seed=42, count=1)[0]


def test_valid_structured_response_produces_classification() -> None:
    adapter = StubAdapter(json.dumps(VALID_RESULT))
    result = classify_event(event(), adapter)
    assert result.event_id == "evt_000001"
    assert result.root_cause_category == "transient"
    assert result.candidate_interventions == ("retry_delayed", "payment_link")
    assert adapter.calls == 1


def test_invalid_root_cause_is_rejected() -> None:
    bad = dict(VALID_RESULT, root_cause_category="unknown")
    adapter = StubAdapter(json.dumps(bad), json.dumps(bad))
    with pytest.raises(ClassificationValidationError):
        classify_event(event(), adapter)
    assert adapter.calls == 2


def test_invalid_intervention_is_rejected() -> None:
    bad = dict(VALID_RESULT, candidate_interventions=["send_whatsapp_payment_link"])
    adapter = StubAdapter(json.dumps(bad), json.dumps(bad))
    with pytest.raises(ClassificationValidationError):
        classify_event(event(), adapter)
    assert adapter.calls == 2


def test_invalid_confidence_is_rejected() -> None:
    bad = dict(VALID_RESULT, confidence=1.7)
    adapter = StubAdapter(json.dumps(bad), json.dumps(bad))
    with pytest.raises(ClassificationValidationError):
        classify_event(event(), adapter)


def test_missing_required_field_is_rejected() -> None:
    bad = {key: value for key, value in VALID_RESULT.items() if key != "reasoning"}
    adapter = StubAdapter(json.dumps(bad), json.dumps(bad))
    with pytest.raises(ClassificationValidationError):
        classify_event(event(), adapter)


def test_malformed_json_retries_once_and_valid_retry_succeeds() -> None:
    adapter = StubAdapter("{not valid json", json.dumps(VALID_RESULT))
    result = classify_event(event(), adapter)
    assert result.event_id == "evt_000001"
    assert adapter.calls == 2


def test_repeated_malformed_json_is_explicit_failure() -> None:
    adapter = StubAdapter("{not valid json", "[1, 2, 3]")
    with pytest.raises(ClassificationValidationError):
        classify_event(event(), adapter)
    assert adapter.calls == 2


def test_model_timeout_or_error_is_explicit_llm_failure() -> None:
    adapter = StubAdapter()
    with pytest.raises(OmniRouteError):
        classify_event(event(), adapter)
    assert adapter.calls == 1


def test_event_id_mismatch_is_rejected() -> None:
    bad = dict(VALID_RESULT, event_id="evt_999999")
    adapter = StubAdapter(json.dumps(bad), json.dumps(bad))
    with pytest.raises(ClassificationValidationError):
        classify_event(event(), adapter)


def test_classifier_input_contains_only_decision_time_fields() -> None:
    evt = event()
    payload = build_classifier_input(evt)
    assert payload == evt.to_dict()
    assert set(payload) == _LOCKED_EVENT_FIELDS
    assert _FORBIDDEN_FUTURE_FIELDS.isdisjoint(payload)


def test_prompt_is_built_only_from_event_payload() -> None:
    evt = event()
    prompt = build_prompt(build_classifier_input(evt))
    for field in evt.to_dict():
        assert field in prompt


def test_classifier_modules_have_no_future_layer_dependency() -> None:
    import inspect

    import app.classification as classification_module
    import app.classifier as classifier_module

    forbidden_imports = (
        "from .db",
        "from .benchmark",
        "from .outcome",
        "from .policy",
        "from .executor",
        "from .ingestion",
        "from .generator",
        "import razorpay",
    )
    for module in (classifier_module, classification_module):
        source = inspect.getsource(module)
        for fragment in forbidden_imports:
            assert fragment not in source, (module.__name__, fragment)


def test_build_adapter_requires_api_key(monkeypatch) -> None:
    monkeypatch.delenv("OMNIROUTE_API_KEY", raising=False)
    with pytest.raises(OmniRouteError):
        build_omniroute_adapter()


def test_adapter_rejects_missing_api_key() -> None:
    with pytest.raises(OmniRouteError):
        OmniRouteClassifier(api_key="", model="m", base_url="https://example.test/v1")


def test_adapter_extracts_content_from_provider_response() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"choices": [{"message": {"content": "hello"}}]})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    adapter = OmniRouteClassifier(
        api_key="test", model="m", base_url="https://example.test/v1", client=client
    )
    assert adapter.generate("p") == "hello"


def test_adapter_surfaces_provider_status_errors() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="boom")

    client = httpx.Client(transport=httpx.MockTransport(handler))
    adapter = OmniRouteClassifier(
        api_key="test", model="m", base_url="https://example.test/v1", client=client
    )
    with pytest.raises(OmniRouteError):
        adapter.generate("p")


def test_adapter_surfaces_unparseable_provider_body() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    adapter = OmniRouteClassifier(
        api_key="test", model="m", base_url="https://example.test/v1", client=client
    )
    with pytest.raises(OmniRouteError):
        adapter.generate("p")

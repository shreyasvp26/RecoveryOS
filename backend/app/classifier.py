"""AI classification boundary.

Phase 5: turns a PaymentEvent into an advisory structured ClassificationResult
through an LLM adapter. The classifier is advisory only — it never authorizes,
selects, or executes an action, and it only ever receives decision-time event
information. Model failures are explicit; malformed structured output is
retried at most once and never coerced into a fake classification.
"""

from __future__ import annotations

import json
from typing import Any, Protocol

import httpx

from .classification import (
    CANDIDATE_INTERVENTIONS,
    ROOT_CAUSE_CATEGORIES,
    ClassificationResult,
)
from .config import (
    get_omniroute_api_key,
    get_omniroute_base_url,
    get_omniroute_model,
)
from .models import PaymentEvent

SYSTEM_INSTRUCTION = (
    "You are a payment-failure diagnostic for a revenue recovery system. "
    "Diagnose the likely root cause of the failed payment using ONLY the event "
    "information provided. Respond with a single JSON object only. Your reply is "
    "strictly advisory: you may recommend candidate interventions, but you never "
    "authorize or select an action, and you must not assume or invent information "
    "about any future outcome."
)


class ClassificationError(Exception):
    """Base class for all explicit classification failures."""


class ClassificationValidationError(ClassificationError):
    """Model output was malformed or failed schema validation."""


class OmniRouteError(ClassificationError):
    """The OmniRoute provider call failed or is not configured."""


class ClassifierAdapter(Protocol):
    """Minimal model-access interface satisfied by the adapter and test stubs."""

    def generate(self, prompt: str) -> str: ...


def build_classifier_input(event: PaymentEvent) -> dict[str, Any]:
    """Return the decision-time-only payload the model may see.

    Derived exclusively from the locked PaymentEvent contract; benchmark ground
    truth and future outcome information are structurally absent.
    """
    return event.to_dict()


def build_prompt(payload: dict[str, Any]) -> str:
    """Build a deterministic-structure classification prompt from an event payload."""
    categories = ", ".join(sorted(ROOT_CAUSE_CATEGORIES))
    interventions = ", ".join(sorted(CANDIDATE_INTERVENTIONS))
    return (
        "Classify this failed payment event. Return EXACTLY one JSON object with "
        "these keys: \"event_id\", \"root_cause_category\" (one of: "
        f"{categories}), \"confidence\" (a number from 0 to 1), \"reasoning\" "
        "(a concise explanation), \"candidate_interventions\" (a list drawn "
        f"from: {interventions}).\n"
        "Event:\n"
        + json.dumps(payload, indent=2)
    )


def _parse_json_object(text: Any) -> dict[str, Any]:
    """Strictly parse model output as a JSON object. No fenced-text guessing."""
    if not isinstance(text, str) or not text.strip():
        raise ClassificationValidationError("model returned empty output")
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ClassificationValidationError("model output is not valid JSON") from exc
    if not isinstance(data, dict):
        raise ClassificationValidationError("model output is not a JSON object")
    return data


def _parse_to_result(text: Any, event: PaymentEvent) -> ClassificationResult:
    """Validate a raw model response against the classification contract."""
    data = _parse_json_object(text)
    try:
        result = ClassificationResult.from_dict(data)
    except ValueError as exc:
        raise ClassificationValidationError(
            f"model output failed schema validation: {exc}"
        ) from exc
    if result.event_id != event.event_id:
        raise ClassificationValidationError(
            f"model returned event_id {result.event_id!r}, expected {event.event_id!r}"
        )
    return result


def classify_event(event: PaymentEvent, adapter: ClassifierAdapter) -> ClassificationResult:
    """Classify a PaymentEvent through the provided adapter.

    At most ONE retry is permitted when the model's output cannot be parsed or
    fails schema validation. Provider/transport errors are never retried and are
    always surfaced as explicit failures.
    """
    prompt = build_prompt(build_classifier_input(event))
    try:
        return _parse_to_result(adapter.generate(prompt), event)
    except ClassificationValidationError:
        try:
            return _parse_to_result(adapter.generate(prompt), event)
        except ClassificationValidationError as exc:
            raise ClassificationValidationError(
                "no valid classification after the single permitted retry"
            ) from exc


class OmniRouteClassifier:
    """Thin HTTP adapter over an OpenAI-compatible OmniRoute completions endpoint."""

    def __init__(
        self,
        *,
        api_key: str,
        model: str,
        base_url: str,
        timeout_seconds: float = 30.0,
        client: httpx.Client | None = None,
    ) -> None:
        if not api_key:
            raise OmniRouteError("OmniRoute API key is required")
        self._model = model
        self._base_url = base_url.rstrip("/")
        self._owns_client = client is None
        self._client = client or httpx.Client(
            timeout=timeout_seconds,
            headers={"Authorization": f"Bearer {api_key}"},
        )

    def close(self) -> None:
        """Close the underlying HTTP client if this adapter owns it."""
        if self._owns_client:
            self._client.close()

    def generate(self, prompt: str) -> str:
        body = {
            "model": self._model,
            "messages": [
                {"role": "system", "content": SYSTEM_INSTRUCTION},
                {"role": "user", "content": prompt},
            ],
            "temperature": 0.0,
        }
        try:
            response = self._client.post(
                f"{self._base_url}/chat/completions", json=body
            )
        except httpx.HTTPError as exc:
            raise OmniRouteError(f"provider request failed: {exc}") from exc
        if response.status_code != 200:
            raise OmniRouteError(f"provider returned status {response.status_code}")
        try:
            content = response.json()["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError, ValueError) as exc:
            raise OmniRouteError("provider returned an unparseable response") from exc
        if not isinstance(content, str) or not content.strip():
            raise OmniRouteError("provider returned an empty response")
        return content


def build_omniroute_adapter() -> OmniRouteClassifier:
    """Build the configured OmniRoute adapter, failing explicitly when misconfigured."""
    api_key = get_omniroute_api_key()
    if not api_key:
        raise OmniRouteError("OMNIROUTE_API_KEY is not configured")
    return OmniRouteClassifier(
        api_key=api_key,
        model=get_omniroute_model(),
        base_url=get_omniroute_base_url(),
    )

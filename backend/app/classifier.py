"""AI classification boundary.

Phase 5: turns a PaymentEvent into an advisory structured ClassificationResult
through an LLM adapter. The classifier is advisory only — it never authorizes,
selects, or executes an action, and it only ever receives decision-time event
information. Model failures are explicit; malformed structured output is
retried at most once and never coerced into a fake classification.
"""

from __future__ import annotations

import json
import urllib.parse
from typing import Any, Protocol

import httpx

from .classification import (
    CANDIDATE_INTERVENTIONS,
    ROOT_CAUSE_CATEGORIES,
    ClassificationError,
    ClassificationResult,
    ClassificationValidationError,
)
from .config import (
    get_gemini_api_key,
    get_gemini_base_url,
    get_gemini_model,
)
from .models import PaymentEvent

DEFAULT_BASE_URL = "https://generativelanguage.googleapis.com/v1beta"
DEFAULT_MODEL = "gemini-2.5-flash"
DEFAULT_TIMEOUT_SECONDS = 15.0

GEMINI_API_KEY_ENV = "GEMINI_API_KEY"
GEMINI_BASE_URL_ENV = "GEMINI_BASE_URL"
GEMINI_MODEL_ENV = "GEMINI_MODEL"

SYSTEM_INSTRUCTION = (
    "You are a payment-failure diagnostic for a revenue recovery system. "
    "Diagnose the likely root cause of the failed payment using ONLY the event "
    "information provided. Respond with a single JSON object only. Your reply is "
    "strictly advisory: you may recommend candidate interventions, but you never "
    "authorize or select an action, and you must not assume or invent information "
    "about any future outcome."
)


class GeminiClassifierError(ClassificationError):
    """The Google Gemini API call failed or is misconfigured."""


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

    At most ONE retry is permitted when the model's output cannot be parsed,
    fails schema validation, or encounters a transient provider error (GeminiClassifierError).
    """
    prompt = build_prompt(build_classifier_input(event))
    try:
        return _parse_to_result(adapter.generate(prompt), event)
    except (GeminiClassifierError, ClassificationValidationError):
        try:
            return _parse_to_result(adapter.generate(prompt), event)
        except GeminiClassifierError:
            raise
        except ClassificationValidationError as exc:
            raise ClassificationValidationError(
                "no valid classification after the single permitted retry"
            ) from exc


def _validate_base_url(base_url: str) -> str:
    """Validate and normalize the outbound provider base URL.

    ``GEMINI_BASE_URL`` is operator-controlled configuration, but this is
    the single outbound HTTP sink in the classification path, so the sink is
    validated at the boundary: HTTPS is required, ``http`` is tolerated only
    for loopback (local development against a local model gateway), the URL
    must include a host, and the normalized form (trailing slash stripped) is
    what gets concatenated into the request path. Invalid values fail here,
    explicitly, rather than producing an unexpected outbound request.
    """
    if not isinstance(base_url, str) or not base_url.strip():
        raise GeminiClassifierError("Gemini base URL is required")
    url = base_url.strip().rstrip("/")
    parsed = urllib.parse.urlsplit(url)
    scheme = parsed.scheme.lower()
    host = (parsed.hostname or "").lower()
    if scheme not in ("https", "http"):
        raise GeminiClassifierError(
            f"Gemini base URL scheme must be https:// (or http:// for "
            f"localhost), got {parsed.scheme!r}"
        )
    if not host:
        raise GeminiClassifierError("Gemini base URL must include a host")
    if scheme == "http" and host not in ("localhost", "127.0.0.1", "::1"):
        raise GeminiClassifierError(
            "Gemini base URL over http is only allowed for localhost"
        )
    return url


class GeminiClassifier:
    """Thin HTTP adapter over the Google Gemini REST generateContent endpoint."""

    def __init__(
        self,
        *,
        api_key: str,
        model: str = DEFAULT_MODEL,
        base_url: str = DEFAULT_BASE_URL,
        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
        client: httpx.Client | None = None,
    ) -> None:
        if not api_key:
            raise GeminiClassifierError("GEMINI_API_KEY is required")
        self._api_key = api_key
        self._model = model
        self._base_url = _validate_base_url(base_url)
        self._owns_client = client is None
        self._client = client or httpx.Client(
            timeout=timeout_seconds,
            headers={
                "x-goog-api-key": self._api_key,
                "Content-Type": "application/json",
            },
        )

    def close(self) -> None:
        """Close the underlying HTTP client if this adapter owns it."""
        if self._owns_client:
            self._client.close()

    def generate(self, prompt: str) -> str:
        url = f"{self._base_url}/models/{self._model}:generateContent"
        body = {
            "systemInstruction": {
                "parts": [{"text": SYSTEM_INSTRUCTION}],
            },
            "contents": [
                {
                    "role": "user",
                    "parts": [{"text": prompt}],
                }
            ],
            "generationConfig": {
                "temperature": 0,
                "responseMimeType": "application/json",
            },
        }
        headers = {
            "x-goog-api-key": self._api_key,
            "Content-Type": "application/json",
        }
        try:
            response = self._client.post(url, json=body, headers=headers)
        except httpx.HTTPError as exc:
            raise GeminiClassifierError(f"provider request failed: {exc}") from exc
        if response.status_code != 200:
            raise GeminiClassifierError(f"provider returned status {response.status_code}")
        try:
            candidates = response.json()["candidates"]
            content = candidates[0]["content"]["parts"][0]["text"]
        except (KeyError, IndexError, TypeError, ValueError) as exc:
            raise GeminiClassifierError("provider returned an unparseable response") from exc
        if not isinstance(content, str) or not content.strip():
            raise GeminiClassifierError("provider returned an empty response")
        return content


def build_gemini_adapter(
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
) -> GeminiClassifier:
    """Build the configured Gemini adapter, failing explicitly when misconfigured."""
    api_key = get_gemini_api_key()
    if not api_key:
        raise GeminiClassifierError("GEMINI_API_KEY is not configured")
    return GeminiClassifier(
        api_key=api_key,
        model=get_gemini_model(),
        base_url=get_gemini_base_url(),
        timeout_seconds=timeout_seconds,
    )

"""Phase 19 Policy Lab HTTP boundaries.

Two endpoints, because the Policy Lab needs exactly two things: the shape of
the policy form, and the result of comparing scenarios over one workload.
Listing, validating, running, comparing and inspecting event-level deltas are
all served by those two, so no endpoint exists that a client would never call.

These routes hold no business logic and no policy rules. They wire HTTP to
``policy_scenario`` (validation) and ``replay``/``replay_metrics`` (evaluation).

SERVER-SIDE VALIDATION
----------------------
Every scenario, built-in or custom, is resolved through
``policy_scenario.resolve_scenario``, which is the only path into a scenario.
A malformed or out-of-bounds policy is refused with 422 and never reaches
evaluation. Frontend validation is a convenience and is never trusted.

NO DATABASE
-----------
Replay is computed on demand and persists nothing. There is deliberately no
connection dependency here: the endpoints cannot read or write operational
state, so a replay cannot contaminate production audit records even by
accident. Results are reproducible from their identity rather than from
storage, because a replay is a pure function of its scenario and configuration.

NOTHING EXECUTES
----------------
No route in this module can perform a payment action. Replay runs entirely
through the benchmark's offline simulator; Razorpay is never contacted and no
Payment Link is ever created.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, status
from fastapi.responses import JSONResponse

from ..policy_scenario import (
    SCENARIO_CURRENT,
    PolicyScenario,
    PolicyScenarioError,
    resolve_scenario,
    scenario_catalog,
)
from ..replay import ReplayError, replay_scenarios
from ..replay_metrics import compare_replays

router = APIRouter(tags=["policy-lab"])

# A comparison replays the full canonical workload once per scenario. The
# canonical dataset is 500 events and one scenario costs a small fraction of a
# second, so this ceiling is generous for the Policy Lab while keeping a single
# request bounded.
MAX_SCENARIOS_PER_COMPARISON = 6


@router.get("/replay/scenarios")
def list_scenarios() -> dict[str, Any]:
    """The built-in scenarios, the custom bounds, and the locked protections.

    Everything a client needs to render the policy form, taken from the real
    engine constants and the shipped configuration, so a UI built on this
    payload cannot show a knob RecoveryOS does not have.
    """
    return scenario_catalog()


@router.post("/replay/compare")
def compare_scenarios(payload: dict[str, Any]) -> JSONResponse:
    """Replay several policy scenarios over one shared workload and compare.

    Request::

        {
          "scenarios": [
            {"scenario_id": "current"},
            {"scenario_id": "conservative"},
            {"scenario_id": "custom", "name": "...", "parameters": {...}}
          ],
          "reference_scenario_id": "current"
        }

    Every scenario is replayed over the SAME events with the SAME
    classifications, the same hidden world and the same seed, so the response
    is a causal reading of what the policy changed. Results are simulated
    evaluations and are labelled as such throughout.
    """
    if not isinstance(payload, dict):
        return _invalid("request body must be an object")

    definitions = payload.get("scenarios")
    if definitions is None:
        return _invalid("scenarios is required")
    if not isinstance(definitions, list) or not definitions:
        return _invalid("scenarios must be a non-empty list")
    if len(definitions) > MAX_SCENARIOS_PER_COMPARISON:
        return _invalid(
            f"at most {MAX_SCENARIOS_PER_COMPARISON} scenarios can be compared "
            f"in one request, got {len(definitions)}"
        )

    reference_id = payload.get("reference_scenario_id", SCENARIO_CURRENT)
    if not isinstance(reference_id, str) or not reference_id.strip():
        return _invalid("reference_scenario_id must be a non-empty string")

    scenarios: list[PolicyScenario] = []
    for index, definition in enumerate(definitions):
        try:
            scenarios.append(resolve_scenario(definition))
        except PolicyScenarioError as exc:
            # A malformed policy is refused here, before anything is evaluated.
            return _invalid(str(exc), index=index)

    identifiers = [scenario.scenario_id for scenario in scenarios]
    if len(set(identifiers)) != len(identifiers):
        return _invalid(
            "each scenario may appear at most once in a comparison; got "
            f"{identifiers}"
        )
    if reference_id not in identifiers:
        return _invalid(
            f"reference_scenario_id {reference_id!r} must be one of the "
            f"requested scenarios {identifiers}"
        )

    try:
        results = replay_scenarios(scenarios)
        comparison = compare_replays(results, reference_id)
    except (ReplayError, PolicyScenarioError) as exc:
        return _invalid(str(exc))
    except ValueError as exc:
        # A comparison that cannot be shown fair is refused, not returned with
        # a caveat.
        return JSONResponse(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            content={"status": "replay_comparison_failure", "detail": str(exc)},
        )

    return JSONResponse(
        status_code=status.HTTP_200_OK,
        content={"status": "replay_success", **comparison},
    )


def _invalid(detail: str, *, index: int | None = None) -> JSONResponse:
    """Refuse a request explicitly; nothing is evaluated."""
    content: dict[str, Any] = {
        "status": "invalid_scenario",
        "detail": detail,
    }
    if index is not None:
        content["scenario_index"] = index
    return JSONResponse(
        status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, content=content
    )

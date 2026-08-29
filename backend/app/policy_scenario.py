"""Policy scenarios for the Phase 19 What-If Decision Lab.

WHAT A SCENARIO IS
------------------
A scenario is DATA, not a code path. It is a validated ``PolicyConfig`` plus an
identity, and the replay engine hands it to the SAME ``PolicyEngine`` the
production pipeline uses. There is no ``if scenario == "aggressive"`` anywhere
in RecoveryOS, and there is no second copy of any policy rule: changing the
scenario changes the configuration the frozen engine reads, and nothing else.

    scenario -> validated PolicyConfig -> existing PolicyEngine -> decision

ACTIVE POLICY VS REPLAY POLICY
------------------------------
A scenario is never written back anywhere. It does not touch the environment,
``config.build_policy_config()``, the database, or any module-level state, so
constructing or replaying one cannot change what the real system would do to
the next real payment. ``CURRENT`` is a READ of the shipped defaults, taken
fresh on every call.

WHICH KNOBS ARE REAL
--------------------
Only the three parameters ``PolicyConfig`` actually exposes are configurable
here: the rolling 24h per-customer intervention limit, the per-event cooldown,
and the daily spend cap. No knob is invented for the lab, and no knob is
exposed that the frozen policy engine would ignore.

WHICH PROTECTIONS ARE IMMUTABLE
-------------------------------
Fraud, terminal-failure and duplicate protection are NOT configuration. They
are unconditional branches inside ``PolicyEngine.evaluate`` with no parameter
attached, so no scenario — built-in or operator-defined — has anything to turn
off. ``IMMUTABLE_PROTECTIONS`` documents that fact, and a custom payload that
tries to name one is rejected at the boundary rather than silently ignored, so
an operator gets an explicit refusal instead of the false impression that a
safety rule was reconfigured.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any, Mapping

from .config import (
    DEFAULT_POLICY_DAILY_SPEND_CAP_PAISE,
    DEFAULT_POLICY_EVENT_COOLDOWN_MINUTES,
    DEFAULT_POLICY_MAX_INTERVENTIONS_PER_CUSTOMER_24H,
)
from .policy import (
    RULE_COOLDOWN,
    RULE_CUSTOMER_LIMIT,
    RULE_DUPLICATE,
    RULE_FRAUD,
    RULE_SPEND_CAP,
    RULE_TERMINAL,
    PolicyConfig,
    PolicyValidationError,
)

SCENARIO_CURRENT = "current"
SCENARIO_CONSERVATIVE = "conservative"
SCENARIO_AGGRESSIVE = "aggressive"
SCENARIO_CUSTOM = "custom"

BUILT_IN_SCENARIO_IDS: tuple[str, ...] = (
    SCENARIO_CURRENT,
    SCENARIO_CONSERVATIVE,
    SCENARIO_AGGRESSIVE,
)

SCENARIO_LABELS: Mapping[str, str] = {
    SCENARIO_CURRENT: "Current Policy",
    SCENARIO_CONSERVATIVE: "Conservative",
    SCENARIO_AGGRESSIVE: "Aggressive",
    SCENARIO_CUSTOM: "Custom",
}

# The three genuinely configurable controls, named exactly as PolicyConfig
# names them so the lab cannot drift from the engine's own vocabulary.
CONFIGURABLE_PARAMETERS: tuple[str, ...] = (
    "max_interventions_per_customer_24h",
    "event_cooldown_minutes",
    "daily_spend_cap_paise",
)

# Rules that exist unconditionally in PolicyEngine.evaluate and carry no
# configuration at all. Listed so the API and the UI can state WHICH stops are
# immutable rather than merely promising that some are.
IMMUTABLE_PROTECTIONS: tuple[str, ...] = (
    RULE_FRAUD,
    RULE_TERMINAL,
    RULE_DUPLICATE,
)

# Rules whose behaviour a scenario can move, paired with the parameter that
# moves each one.
CONFIGURABLE_RULES: Mapping[str, str] = {
    RULE_CUSTOMER_LIMIT: "max_interventions_per_customer_24h",
    RULE_COOLDOWN: "event_cooldown_minutes",
    RULE_SPEND_CAP: "daily_spend_cap_paise",
}

# ---------------------------------------------------------------------------
# Deterministic derivation of the built-in scenarios
# ---------------------------------------------------------------------------
#
# Conservative and Aggressive are DERIVED from whatever the current policy
# actually is, by a single documented factor, rather than being hand-picked
# business numbers. That keeps them stable, reproducible and honest: if the
# shipped defaults ever change, the two scenarios move with them and remain
# "half as permissive" and "twice as permissive" by construction.
#
# The factor applies to permissiveness, so it multiplies the two allowances
# (intervention limit, spend cap) and divides into the one restraint
# (cooldown), with floor division and a floor of the engine's own minimum of 1.
#
#   conservative: limit // 2,  cooldown * 2,  cap // 2
#   aggressive:   limit * 2,   cooldown // 2, cap * 2
#
# Against the shipped defaults (limit 2, cooldown 30 min, cap 5,000,000 paise)
# this yields conservative (1, 60, 2,500,000) and aggressive (4, 15, 10,000,000).
SCENARIO_DERIVATION_FACTOR = 2

# Guardrails for an operator-defined scenario, expressed as multiples of the
# shipped defaults so they too move with the policy rather than being magic
# numbers. The cooldown ceiling is 24 hours because the per-customer rule
# reasons over a rolling 24h window: a cooldown longer than that window cannot
# be interpreted against it.
CUSTOM_MIN_MAX_INTERVENTIONS = 1
CUSTOM_MAX_MAX_INTERVENTIONS = DEFAULT_POLICY_MAX_INTERVENTIONS_PER_CUSTOMER_24H * 5
CUSTOM_MIN_COOLDOWN_MINUTES = 1
CUSTOM_MAX_COOLDOWN_MINUTES = 24 * 60
CUSTOM_MIN_SPEND_CAP_PAISE = 0
CUSTOM_MAX_SPEND_CAP_PAISE = DEFAULT_POLICY_DAILY_SPEND_CAP_PAISE * 20

CUSTOM_BOUNDS: Mapping[str, Mapping[str, int]] = {
    "max_interventions_per_customer_24h": {
        "minimum": CUSTOM_MIN_MAX_INTERVENTIONS,
        "maximum": CUSTOM_MAX_MAX_INTERVENTIONS,
    },
    "event_cooldown_minutes": {
        "minimum": CUSTOM_MIN_COOLDOWN_MINUTES,
        "maximum": CUSTOM_MAX_COOLDOWN_MINUTES,
    },
    "daily_spend_cap_paise": {
        "minimum": CUSTOM_MIN_SPEND_CAP_PAISE,
        "maximum": CUSTOM_MAX_SPEND_CAP_PAISE,
    },
}


class PolicyScenarioError(Exception):
    """A policy scenario is malformed and is never replayed."""


def _require_bounded_int(value: Any, name: str) -> int:
    """Return ``value`` as a plain int inside its configured bounds, or fail.

    Rejects rather than clamps. A request for a limit of -1 is a broken
    request, and quietly turning it into 1 would replay a policy the operator
    never asked for and then label the result with their name for it.
    """
    bounds = CUSTOM_BOUNDS[name]
    # bool is an int subclass; True must not be accepted as the number 1.
    if isinstance(value, bool) or not isinstance(value, int):
        raise PolicyScenarioError(
            f"{name} must be an integer, got {type(value).__name__}"
        )
    if not (bounds["minimum"] <= value <= bounds["maximum"]):
        raise PolicyScenarioError(
            f"{name} must satisfy {bounds['minimum']} <= value <= "
            f"{bounds['maximum']}, got {value}"
        )
    return value


@dataclass(frozen=True)
class PolicyScenario:
    """One named, validated policy configuration the lab can replay.

    Holds a real ``PolicyConfig`` — the same type ``PolicyEngine.evaluate``
    already takes — so there is no scenario-shaped parallel configuration for
    the engine to disagree with.
    """

    scenario_id: str
    name: str
    policy_config: PolicyConfig
    derived_from: str | None = None
    derivation: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.scenario_id, str) or not self.scenario_id.strip():
            raise PolicyScenarioError("scenario_id must be a non-empty string")
        if not isinstance(self.name, str) or not self.name.strip():
            raise PolicyScenarioError("name must be a non-empty string")
        if not isinstance(self.policy_config, PolicyConfig):
            raise PolicyScenarioError("policy_config must be a PolicyConfig")
        for field_name in ("derived_from", "derivation"):
            value = getattr(self, field_name)
            if value is not None and (
                not isinstance(value, str) or not value.strip()
            ):
                raise PolicyScenarioError(
                    f"{field_name} must be None or a non-empty string"
                )

    @property
    def parameters(self) -> dict[str, int]:
        """The three configurable controls as plain integers."""
        return {
            "max_interventions_per_customer_24h": (
                self.policy_config.max_interventions_per_customer_24h
            ),
            "event_cooldown_minutes": self.policy_config.event_cooldown_minutes,
            "daily_spend_cap_paise": self.policy_config.daily_spend_cap_paise,
        }

    def to_dict(self) -> dict[str, Any]:
        """Serialize the scenario, including what cannot be configured.

        ``immutable_protections`` travels with the scenario deliberately: a
        client rendering a policy form needs to show the locked stops next to
        the editable ones, and it must read that list from the engine's own
        constants rather than hardcoding its own idea of what is safe.
        """
        return {
            "scenario_id": self.scenario_id,
            "name": self.name,
            "derived_from": self.derived_from,
            "derivation": self.derivation,
            "parameters": self.parameters,
            "intervention_cost_paise": dict(
                sorted(self.policy_config.intervention_cost_paise.items())
            ),
            "immutable_protections": list(IMMUTABLE_PROTECTIONS),
            "configurable_parameters": list(CONFIGURABLE_PARAMETERS),
            "policy_fingerprint": self.fingerprint(),
        }

    def fingerprint(self) -> str:
        """A stable digest of everything that can change a policy decision.

        Deterministic across processes: canonical JSON with sorted keys and
        blake2b, matching the Phase 17 convention. Python's ``hash`` is
        unusable for a persisted or compared identity because it is randomized
        per process.

        Covers ONLY the policy configuration, not the scenario's name, so two
        scenarios that would decide identically share a fingerprint however
        they are labelled. That is what makes "A and B differ only in policy"
        checkable rather than assumed.
        """
        payload = {
            "parameters": self.parameters,
            "intervention_cost_paise": dict(
                sorted(self.policy_config.intervention_cost_paise.items())
            ),
            "immutable_protections": list(IMMUTABLE_PROTECTIONS),
        }
        encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        return hashlib.blake2b(encoded.encode("utf-8"), digest_size=16).hexdigest()


def _scenario_config(
    max_interventions: int, cooldown_minutes: int, spend_cap_paise: int
) -> PolicyConfig:
    """Build a PolicyConfig, surfacing the engine's own validation failures.

    ``PolicyConfig.__post_init__`` is the authoritative validator; this only
    translates its error into the scenario vocabulary so an operator sees one
    consistent failure type.
    """
    try:
        return PolicyConfig(
            max_interventions_per_customer_24h=max_interventions,
            event_cooldown_minutes=cooldown_minutes,
            daily_spend_cap_paise=spend_cap_paise,
        )
    except PolicyValidationError as exc:
        raise PolicyScenarioError(str(exc)) from exc


def current_scenario() -> PolicyScenario:
    """The reference scenario: the policy the shipped system actually uses.

    Reads the same module-level defaults ``config.py`` resolves the live
    policy from, and the same ones ``benchmark_config.frozen_policy_config``
    pins the benchmark to, so the lab's reference arm is genuinely the current
    policy rather than a copy of it that could drift.

    Deliberately NOT ``config.build_policy_config()``: that reads environment
    variables, and a replay whose reference policy depends on the shell the
    server was launched from is not reproducible or comparable.
    """
    return PolicyScenario(
        scenario_id=SCENARIO_CURRENT,
        name=SCENARIO_LABELS[SCENARIO_CURRENT],
        policy_config=_scenario_config(
            DEFAULT_POLICY_MAX_INTERVENTIONS_PER_CUSTOMER_24H,
            DEFAULT_POLICY_EVENT_COOLDOWN_MINUTES,
            DEFAULT_POLICY_DAILY_SPEND_CAP_PAISE,
        ),
        derivation="the shipped RecoveryOS policy defaults, read unchanged",
    )


def conservative_scenario() -> PolicyScenario:
    """A stricter policy, derived from the current one by the fixed factor."""
    base = current_scenario().policy_config
    factor = SCENARIO_DERIVATION_FACTOR
    return PolicyScenario(
        scenario_id=SCENARIO_CONSERVATIVE,
        name=SCENARIO_LABELS[SCENARIO_CONSERVATIVE],
        policy_config=_scenario_config(
            max(1, base.max_interventions_per_customer_24h // factor),
            base.event_cooldown_minutes * factor,
            base.daily_spend_cap_paise // factor,
        ),
        derived_from=SCENARIO_CURRENT,
        derivation=(
            f"current policy made {factor}x less permissive: limit // {factor}, "
            f"cooldown * {factor}, spend cap // {factor}"
        ),
    )


def aggressive_scenario() -> PolicyScenario:
    """A more permissive policy, derived from the current one by the factor.

    More permissive means WIDER BOUNDED THRESHOLDS, never fewer protections:
    the fraud, terminal and duplicate stops are not parameters and are
    untouched by this or any other scenario.
    """
    base = current_scenario().policy_config
    factor = SCENARIO_DERIVATION_FACTOR
    return PolicyScenario(
        scenario_id=SCENARIO_AGGRESSIVE,
        name=SCENARIO_LABELS[SCENARIO_AGGRESSIVE],
        policy_config=_scenario_config(
            base.max_interventions_per_customer_24h * factor,
            max(1, base.event_cooldown_minutes // factor),
            base.daily_spend_cap_paise * factor,
        ),
        derived_from=SCENARIO_CURRENT,
        derivation=(
            f"current policy made {factor}x more permissive: limit * {factor}, "
            f"cooldown // {factor}, spend cap * {factor}"
        ),
    )


_BUILT_IN_BUILDERS = {
    SCENARIO_CURRENT: current_scenario,
    SCENARIO_CONSERVATIVE: conservative_scenario,
    SCENARIO_AGGRESSIVE: aggressive_scenario,
}


def built_in_scenarios() -> tuple[PolicyScenario, ...]:
    """Every built-in scenario, in canonical presentation order."""
    return tuple(
        _BUILT_IN_BUILDERS[scenario_id]() for scenario_id in BUILT_IN_SCENARIO_IDS
    )


def get_scenario(scenario_id: str) -> PolicyScenario:
    """Return a built-in scenario by id, or fail explicitly."""
    builder = _BUILT_IN_BUILDERS.get(scenario_id)
    if builder is None:
        raise PolicyScenarioError(
            f"unknown scenario {scenario_id!r}; expected one of "
            f"{sorted(_BUILT_IN_BUILDERS)} or a custom scenario definition"
        )
    return builder()


def custom_scenario(
    parameters: Mapping[str, Any], *, name: str = SCENARIO_LABELS[SCENARIO_CUSTOM]
) -> PolicyScenario:
    """Validate and build an operator-defined scenario.

    Every configurable parameter must be present and inside its bounds. Two
    kinds of request are refused outright rather than partially honoured:

    * an unknown parameter, because silently dropping it would replay a policy
      the operator did not ask for while labelling it with their name;
    * a parameter naming an immutable protection, because accepting it would
      imply the lab had reconfigured a safety stop that has no configuration.
    """
    if not isinstance(parameters, Mapping):
        raise PolicyScenarioError("custom policy parameters must be an object")
    if not isinstance(name, str) or not name.strip():
        raise PolicyScenarioError("name must be a non-empty string")

    supplied = set(parameters)
    protection_attempts = sorted(
        key
        for key in supplied
        if any(protection in key for protection in IMMUTABLE_PROTECTIONS)
        or key in {"fraud_protection", "terminal_protection", "duplicate_protection"}
    )
    if protection_attempts:
        raise PolicyScenarioError(
            f"{protection_attempts} cannot be configured: fraud, terminal-failure "
            "and duplicate protection are unconditional and have no setting"
        )
    unknown = sorted(supplied - set(CONFIGURABLE_PARAMETERS))
    if unknown:
        raise PolicyScenarioError(
            f"unknown policy parameters {unknown}; configurable parameters are "
            f"{list(CONFIGURABLE_PARAMETERS)}"
        )
    missing = sorted(set(CONFIGURABLE_PARAMETERS) - supplied)
    if missing:
        raise PolicyScenarioError(
            f"custom policy is missing required parameters {missing}"
        )

    return PolicyScenario(
        scenario_id=SCENARIO_CUSTOM,
        name=name,
        policy_config=_scenario_config(
            _require_bounded_int(
                parameters["max_interventions_per_customer_24h"],
                "max_interventions_per_customer_24h",
            ),
            _require_bounded_int(
                parameters["event_cooldown_minutes"], "event_cooldown_minutes"
            ),
            _require_bounded_int(
                parameters["daily_spend_cap_paise"], "daily_spend_cap_paise"
            ),
        ),
        derivation="operator-defined, validated against the custom bounds",
    )


def resolve_scenario(definition: Mapping[str, Any]) -> PolicyScenario:
    """Build a scenario from an API-shaped definition.

    ``{"scenario_id": "current"}`` selects a built-in; ``{"scenario_id":
    "custom", "name": ..., "parameters": {...}}`` defines one. This is the
    single entry point the HTTP boundary uses, so validation cannot be skipped
    by reaching a builder directly.
    """
    if not isinstance(definition, Mapping):
        raise PolicyScenarioError("scenario definition must be an object")
    scenario_id = definition.get("scenario_id")
    if not isinstance(scenario_id, str) or not scenario_id.strip():
        raise PolicyScenarioError("scenario_id must be a non-empty string")

    if scenario_id != SCENARIO_CUSTOM:
        if "parameters" in definition:
            raise PolicyScenarioError(
                f"built-in scenario {scenario_id!r} has fixed parameters; use "
                f"scenario_id {SCENARIO_CUSTOM!r} to supply your own"
            )
        return get_scenario(scenario_id)

    parameters = definition.get("parameters")
    if parameters is None:
        raise PolicyScenarioError("a custom scenario requires parameters")
    name = definition.get("name", SCENARIO_LABELS[SCENARIO_CUSTOM])
    return custom_scenario(parameters, name=name)


def scenario_catalog() -> dict[str, Any]:
    """Everything a client needs to render the policy form, from real values.

    The bounds, the locked protections and the current parameter values all
    come from the engine and the shipped configuration, so a UI built on this
    payload cannot display a knob RecoveryOS does not have or a default it does
    not use.
    """
    return {
        "scenarios": [scenario.to_dict() for scenario in built_in_scenarios()],
        "custom": {
            "scenario_id": SCENARIO_CUSTOM,
            "name": SCENARIO_LABELS[SCENARIO_CUSTOM],
            "bounds": {
                name: dict(bounds) for name, bounds in CUSTOM_BOUNDS.items()
            },
            "defaults": current_scenario().parameters,
        },
        "configurable_parameters": list(CONFIGURABLE_PARAMETERS),
        "configurable_rules": dict(CONFIGURABLE_RULES),
        "immutable_protections": list(IMMUTABLE_PROTECTIONS),
        "reference_scenario_id": SCENARIO_CURRENT,
    }

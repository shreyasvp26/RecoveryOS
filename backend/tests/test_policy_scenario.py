"""Phase 19: the policy scenario foundation.

A scenario is validated configuration for the frozen policy engine, so these
tests are about identity, derivation, bounds, and — most importantly — the
things a scenario is structurally unable to express.
"""

from __future__ import annotations

import pytest

from app.config import (
    DEFAULT_POLICY_DAILY_SPEND_CAP_PAISE,
    DEFAULT_POLICY_EVENT_COOLDOWN_MINUTES,
    DEFAULT_POLICY_MAX_INTERVENTIONS_PER_CUSTOMER_24H,
)
from app.policy import PolicyConfig
from app.policy_scenario import (
    BUILT_IN_SCENARIO_IDS,
    CONFIGURABLE_PARAMETERS,
    CUSTOM_MAX_COOLDOWN_MINUTES,
    CUSTOM_MAX_MAX_INTERVENTIONS,
    CUSTOM_MAX_SPEND_CAP_PAISE,
    IMMUTABLE_PROTECTIONS,
    SCENARIO_AGGRESSIVE,
    SCENARIO_CONSERVATIVE,
    SCENARIO_CURRENT,
    SCENARIO_CUSTOM,
    PolicyScenario,
    PolicyScenarioError,
    aggressive_scenario,
    built_in_scenarios,
    conservative_scenario,
    current_scenario,
    custom_scenario,
    get_scenario,
    resolve_scenario,
    scenario_catalog,
)


_POLICY_ENV = (
    "POLICY_MAX_INTERVENTIONS_PER_CUSTOMER_24H",
    "POLICY_EVENT_COOLDOWN_MINUTES",
    "POLICY_DAILY_SPEND_CAP_PAISE",
)


def _valid_custom(**overrides) -> dict[str, int]:
    parameters = dict(current_scenario().parameters)
    parameters.update(overrides)
    return parameters


# ---------------------------------------------------------------------------
# The reference scenario really is the current policy
# ---------------------------------------------------------------------------


def test_current_scenario_matches_the_shipped_defaults_when_unconfigured(
    monkeypatch,
):
    """With no POLICY_* override the active policy IS the shipped default."""
    for name in _POLICY_ENV:
        monkeypatch.delenv(name, raising=False)

    scenario = current_scenario()

    assert scenario.scenario_id == SCENARIO_CURRENT
    assert scenario.parameters == {
        "max_interventions_per_customer_24h": (
            DEFAULT_POLICY_MAX_INTERVENTIONS_PER_CUSTOMER_24H
        ),
        "event_cooldown_minutes": DEFAULT_POLICY_EVENT_COOLDOWN_MINUTES,
        "daily_spend_cap_paise": DEFAULT_POLICY_DAILY_SPEND_CAP_PAISE,
    }


def test_current_scenario_carries_a_real_policy_config():
    """The scenario holds the engine's own type, not a parallel structure."""
    assert isinstance(current_scenario().policy_config, PolicyConfig)


def test_current_scenario_agrees_with_the_benchmark_policy_when_unconfigured(
    monkeypatch,
):
    """Unconfigured, the two coincide — which is why they can be confused.

    They are nevertheless resolved through separate paths, and the tests below
    prove they are permitted to diverge.
    """
    from app.benchmark_config import frozen_policy_config

    for name in _POLICY_ENV:
        monkeypatch.delenv(name, raising=False)

    benchmark = frozen_policy_config()
    current = current_scenario().policy_config

    assert (
        current.max_interventions_per_customer_24h
        == benchmark.max_interventions_per_customer_24h
    )
    assert current.event_cooldown_minutes == benchmark.event_cooldown_minutes
    assert current.daily_spend_cap_paise == benchmark.daily_spend_cap_paise


# ---------------------------------------------------------------------------
# Deterministic derivation
# ---------------------------------------------------------------------------


def test_conservative_is_derived_strictly_from_the_current_policy():
    base = current_scenario().parameters
    conservative = conservative_scenario().parameters

    assert conservative["max_interventions_per_customer_24h"] <= (
        base["max_interventions_per_customer_24h"]
    )
    assert conservative["event_cooldown_minutes"] > base["event_cooldown_minutes"]
    assert conservative["daily_spend_cap_paise"] < base["daily_spend_cap_paise"]
    assert conservative_scenario().derived_from == SCENARIO_CURRENT


def test_aggressive_is_derived_more_permissively_from_the_current_policy():
    base = current_scenario().parameters
    aggressive = aggressive_scenario().parameters

    assert aggressive["max_interventions_per_customer_24h"] > (
        base["max_interventions_per_customer_24h"]
    )
    assert aggressive["event_cooldown_minutes"] < base["event_cooldown_minutes"]
    assert aggressive["daily_spend_cap_paise"] > base["daily_spend_cap_paise"]
    assert aggressive_scenario().derived_from == SCENARIO_CURRENT


def test_derivation_is_reproducible():
    """Built twice, a scenario is identical — no clock, no randomness."""
    for builder in (current_scenario, conservative_scenario, aggressive_scenario):
        assert builder().to_dict() == builder().to_dict()


def test_derivation_is_documented_on_every_built_in_scenario():
    for scenario in built_in_scenarios():
        assert scenario.derivation


def test_built_in_scenarios_are_in_canonical_order():
    assert tuple(s.scenario_id for s in built_in_scenarios()) == BUILT_IN_SCENARIO_IDS


# ---------------------------------------------------------------------------
# Identity / fingerprint
# ---------------------------------------------------------------------------


def test_scenarios_with_different_policies_have_different_fingerprints():
    fingerprints = {s.fingerprint() for s in built_in_scenarios()}
    assert len(fingerprints) == len(BUILT_IN_SCENARIO_IDS)


def test_fingerprint_is_stable_across_construction():
    assert current_scenario().fingerprint() == current_scenario().fingerprint()


def test_fingerprint_ignores_the_scenario_label():
    """Identity is the policy, not the name it was given."""
    renamed = custom_scenario(_valid_custom(), name="Something Else")
    assert renamed.fingerprint() == custom_scenario(_valid_custom()).fingerprint()


def test_fingerprint_tracks_every_configurable_parameter():
    baseline = custom_scenario(_valid_custom()).fingerprint()
    for parameter, changed in (
        ("max_interventions_per_customer_24h", 3),
        ("event_cooldown_minutes", 45),
        ("daily_spend_cap_paise", 1234),
    ):
        other = custom_scenario(_valid_custom(**{parameter: changed}))
        assert other.fingerprint() != baseline, parameter


def test_fingerprint_is_a_stable_hex_digest_not_python_hash():
    fingerprint = current_scenario().fingerprint()
    assert len(fingerprint) == 32
    int(fingerprint, 16)


# ---------------------------------------------------------------------------
# Serialization
# ---------------------------------------------------------------------------


def test_serialization_exposes_the_locked_protections():
    payload = current_scenario().to_dict()
    assert payload["immutable_protections"] == list(IMMUTABLE_PROTECTIONS)
    assert payload["configurable_parameters"] == list(CONFIGURABLE_PARAMETERS)


def test_serialization_carries_the_policy_fingerprint():
    scenario = conservative_scenario()
    assert scenario.to_dict()["policy_fingerprint"] == scenario.fingerprint()


def test_serialization_uses_integer_paise_only():
    payload = current_scenario().to_dict()
    value = payload["parameters"]["daily_spend_cap_paise"]
    assert isinstance(value, int) and not isinstance(value, bool)


# ---------------------------------------------------------------------------
# Custom scenario validation
# ---------------------------------------------------------------------------


def test_custom_scenario_accepts_values_inside_the_bounds():
    scenario = custom_scenario(
        _valid_custom(max_interventions_per_customer_24h=3)
    )
    assert scenario.scenario_id == SCENARIO_CUSTOM
    assert scenario.parameters["max_interventions_per_customer_24h"] == 3


@pytest.mark.parametrize(
    "overrides",
    [
        {"max_interventions_per_customer_24h": 0},
        {"max_interventions_per_customer_24h": -1},
        {"max_interventions_per_customer_24h": CUSTOM_MAX_MAX_INTERVENTIONS + 1},
        {"event_cooldown_minutes": 0},
        {"event_cooldown_minutes": -30},
        {"event_cooldown_minutes": CUSTOM_MAX_COOLDOWN_MINUTES + 1},
        {"daily_spend_cap_paise": -1},
        {"daily_spend_cap_paise": CUSTOM_MAX_SPEND_CAP_PAISE + 1},
    ],
)
def test_custom_scenario_rejects_out_of_bounds_values(overrides):
    with pytest.raises(PolicyScenarioError):
        custom_scenario(_valid_custom(**overrides))


@pytest.mark.parametrize(
    "value",
    [1.5, "2", None, True, [2], {"value": 2}],
)
def test_custom_scenario_rejects_malformed_types(value):
    """bool is an int subclass and must not slip through as a number."""
    with pytest.raises(PolicyScenarioError):
        custom_scenario(_valid_custom(max_interventions_per_customer_24h=value))


def test_custom_scenario_rejects_unknown_parameters():
    parameters = _valid_custom()
    parameters["max_retries"] = 9
    with pytest.raises(PolicyScenarioError, match="unknown policy parameters"):
        custom_scenario(parameters)


def test_custom_scenario_rejects_missing_parameters():
    parameters = _valid_custom()
    del parameters["event_cooldown_minutes"]
    with pytest.raises(PolicyScenarioError, match="missing required parameters"):
        custom_scenario(parameters)


def test_custom_scenario_rejects_a_non_mapping():
    with pytest.raises(PolicyScenarioError):
        custom_scenario([1, 2, 3])


# ---------------------------------------------------------------------------
# Immutable protections cannot be expressed at all
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "key",
    [
        "fraud_protection",
        "terminal_protection",
        "duplicate_protection",
        "fraud_protection_enabled",
        "terminal_failure",
        "duplicate_intervention",
    ],
)
def test_custom_scenario_refuses_to_configure_an_immutable_protection(key):
    parameters = _valid_custom()
    parameters[key] = False
    with pytest.raises(PolicyScenarioError, match="cannot be configured"):
        custom_scenario(parameters)


def test_no_scenario_can_express_a_protection_toggle():
    """The three locked stops are absent from the configurable surface."""
    for protection in IMMUTABLE_PROTECTIONS:
        assert protection not in CONFIGURABLE_PARAMETERS
    for scenario in built_in_scenarios():
        assert set(scenario.parameters) == set(CONFIGURABLE_PARAMETERS)


def test_policy_config_has_no_field_for_any_immutable_protection():
    """The engine's configuration type simply has no such knob to set."""
    fields = set(PolicyConfig.__dataclass_fields__)
    assert not (fields & set(IMMUTABLE_PROTECTIONS))
    assert "fraud" not in " ".join(fields)


# ---------------------------------------------------------------------------
# Resolution at the API boundary
# ---------------------------------------------------------------------------


def test_resolve_scenario_selects_a_built_in():
    assert resolve_scenario({"scenario_id": SCENARIO_AGGRESSIVE}).parameters == (
        aggressive_scenario().parameters
    )


def test_resolve_scenario_builds_a_custom_definition():
    scenario = resolve_scenario(
        {
            "scenario_id": SCENARIO_CUSTOM,
            "name": "Operator trial",
            "parameters": _valid_custom(max_interventions_per_customer_24h=5),
        }
    )
    assert scenario.name == "Operator trial"
    assert scenario.parameters["max_interventions_per_customer_24h"] == 5


def test_resolve_scenario_rejects_parameters_on_a_built_in():
    """Otherwise 'current' could be quietly redefined and still called current."""
    with pytest.raises(PolicyScenarioError, match="fixed parameters"):
        resolve_scenario(
            {"scenario_id": SCENARIO_CURRENT, "parameters": _valid_custom()}
        )


def test_resolve_scenario_rejects_a_custom_definition_without_parameters():
    with pytest.raises(PolicyScenarioError, match="requires parameters"):
        resolve_scenario({"scenario_id": SCENARIO_CUSTOM})


@pytest.mark.parametrize("definition", [None, [], "current", {"scenario_id": ""}])
def test_resolve_scenario_rejects_malformed_definitions(definition):
    with pytest.raises(PolicyScenarioError):
        resolve_scenario(definition)


def test_get_scenario_rejects_an_unknown_id():
    with pytest.raises(PolicyScenarioError, match="unknown scenario"):
        get_scenario("permissive")


# ---------------------------------------------------------------------------
# Catalog
# ---------------------------------------------------------------------------


def test_catalog_publishes_real_bounds_and_defaults():
    catalog = scenario_catalog()

    assert catalog["reference_scenario_id"] == SCENARIO_CURRENT
    assert [s["scenario_id"] for s in catalog["scenarios"]] == list(
        BUILT_IN_SCENARIO_IDS
    )
    assert catalog["custom"]["defaults"] == current_scenario().parameters
    assert set(catalog["custom"]["bounds"]) == set(CONFIGURABLE_PARAMETERS)
    assert catalog["immutable_protections"] == list(IMMUTABLE_PROTECTIONS)


def test_catalog_maps_every_configurable_rule_to_a_real_parameter():
    catalog = scenario_catalog()
    for rule, parameter in catalog["configurable_rules"].items():
        assert parameter in CONFIGURABLE_PARAMETERS, rule


def test_catalog_never_offers_a_bound_for_a_locked_protection():
    bounds = scenario_catalog()["custom"]["bounds"]
    assert not (set(bounds) & set(IMMUTABLE_PROTECTIONS))


# ---------------------------------------------------------------------------
# Constructing a scenario changes nothing outside itself
# ---------------------------------------------------------------------------


def test_building_scenarios_does_not_mutate_the_active_policy(monkeypatch):
    """Every scenario is a fresh value; none writes back to configuration."""
    from app import config as config_module

    before = config_module.build_policy_config()
    conservative_scenario()
    aggressive_scenario()
    custom_scenario(_valid_custom(max_interventions_per_customer_24h=7))
    after = config_module.build_policy_config()

    assert before == after


def test_scenario_model_rejects_a_non_policy_config():
    with pytest.raises(PolicyScenarioError):
        PolicyScenario(
            scenario_id="x", name="x", policy_config={"max": 1}
        )

"""Phase 19 hardening: what "Current Policy" actually means.

The Policy Lab makes a claim on the operator's behalf — *this is the policy
RecoveryOS is configured to use right now* — and a what-if answer built on the
wrong baseline is worse than no answer, because it is confidently wrong about a
system nobody is running.

These tests pin three things that are easy to conflate and must not be:

* the ACTIVE RUNTIME policy, resolved by ``config.build_policy_config()``;
* the CANONICAL BENCHMARK policy, pinned by
  ``benchmark_config.frozen_policy_config`` so Phase 17 reproduces anywhere;
* the REPLAY POLICY BOUNDS, a fixed admissible range for operator input.

They coincide on an unconfigured install, which is exactly why a test suite has
to prove they are resolved separately.
"""

from __future__ import annotations

import ast
import dataclasses
import inspect
from pathlib import Path

import pytest

from app import config as config_module
from app.benchmark_config import Phase17BenchmarkConfig, frozen_policy_config
from app.policy_scenario import (
    CUSTOM_BOUNDS,
    SCENARIO_DERIVATION_FACTOR,
    SOURCE_ACTIVE_RUNTIME,
    SOURCE_DERIVED_FROM_ACTIVE_RUNTIME,
    SOURCE_OPERATOR_DEFINED,
    PolicyScenarioError,
    active_policy_snapshot,
    aggressive_scenario,
    conservative_scenario,
    current_scenario,
    custom_form_defaults,
    custom_scenario,
    scenario_catalog,
)

# A configuration that is deliberately NOTHING like the shipped defaults
# (2 / 30 / 5_000_000), so a test cannot pass by coincidence.
CONFIGURED_LIMIT = 6
CONFIGURED_COOLDOWN = 45
CONFIGURED_SPEND_CAP = 7_777_777


@pytest.fixture
def configured_runtime(monkeypatch):
    """Configure the runtime policy through the supported mechanism only.

    Sets the same ``POLICY_*`` environment variables ``tests/test_config.py``
    already uses, rather than reaching past ``build_policy_config`` — the point
    is to prove the lab reads the real configuration path.
    """
    monkeypatch.setenv(
        "POLICY_MAX_INTERVENTIONS_PER_CUSTOMER_24H", str(CONFIGURED_LIMIT)
    )
    monkeypatch.setenv("POLICY_EVENT_COOLDOWN_MINUTES", str(CONFIGURED_COOLDOWN))
    monkeypatch.setenv("POLICY_DAILY_SPEND_CAP_PAISE", str(CONFIGURED_SPEND_CAP))


# ---------------------------------------------------------------------------
# Current reflects the ACTIVE RUNTIME policy
# ---------------------------------------------------------------------------


def test_current_scenario_reflects_the_configured_runtime_policy(
    configured_runtime,
):
    """HARDENING TEST 1."""
    assert current_scenario().parameters == {
        "max_interventions_per_customer_24h": CONFIGURED_LIMIT,
        "event_cooldown_minutes": CONFIGURED_COOLDOWN,
        "daily_spend_cap_paise": CONFIGURED_SPEND_CAP,
    }


def test_current_scenario_is_the_same_object_the_execution_path_gates_on(
    configured_runtime,
):
    """Not merely equal numbers — the identical PolicyConfig value."""
    assert current_scenario().policy_config == config_module.build_policy_config()


def test_the_active_snapshot_is_frozen_and_freshly_read(configured_runtime):
    """A snapshot cannot be edited, so replay cannot edit the live policy."""
    snapshot = active_policy_snapshot()

    with pytest.raises(dataclasses.FrozenInstanceError):
        snapshot.max_interventions_per_customer_24h = 99

    assert snapshot == active_policy_snapshot()


def test_current_scenario_is_labelled_as_the_active_runtime_policy():
    assert current_scenario().source == SOURCE_ACTIVE_RUNTIME
    assert current_scenario().to_dict()["source"] == SOURCE_ACTIVE_RUNTIME


def test_current_scenario_tracks_configuration_changes(monkeypatch):
    """Re-reading after a change reports the change, with no caching."""
    monkeypatch.setenv("POLICY_EVENT_COOLDOWN_MINUTES", "11")
    assert current_scenario().parameters["event_cooldown_minutes"] == 11

    monkeypatch.setenv("POLICY_EVENT_COOLDOWN_MINUTES", "12")
    assert current_scenario().parameters["event_cooldown_minutes"] == 12


def test_a_broken_runtime_policy_is_an_explicit_scenario_error(monkeypatch):
    """Fail-closed and legible, rather than a 500 from deep in the lab."""
    monkeypatch.setenv("POLICY_EVENT_COOLDOWN_MINUTES", "not-a-number")

    with pytest.raises(PolicyScenarioError) as excinfo:
        current_scenario()

    assert "active runtime policy configuration is invalid" in str(excinfo.value)


def test_the_lab_does_not_parse_policy_environment_variables_itself():
    """One configuration system. The lab READS it, it does not reimplement it.

    Checked structurally: no ``POLICY_*`` variable name and no ``os.environ``
    access may appear in the scenario module, so the only way it can learn the
    active policy is through ``config.build_policy_config()``.
    """
    source = Path(inspect.getsourcefile(current_scenario)).read_text()
    tree = ast.parse(source)

    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute) and node.attr in {"environ", "getenv"}:
            raise AssertionError("the scenario module reads the environment directly")
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            assert not node.value.startswith("POLICY_"), (
                f"policy env var {node.value!r} is parsed in the scenario module; "
                "resolve it through config.build_policy_config() instead"
            )


# ---------------------------------------------------------------------------
# Conservative and Aggressive derive from the ACTIVE policy
# ---------------------------------------------------------------------------


def test_conservative_derives_from_the_configured_runtime_policy(
    configured_runtime,
):
    """HARDENING TEST 3."""
    factor = SCENARIO_DERIVATION_FACTOR

    assert conservative_scenario().parameters == {
        "max_interventions_per_customer_24h": CONFIGURED_LIMIT // factor,
        "event_cooldown_minutes": CONFIGURED_COOLDOWN * factor,
        "daily_spend_cap_paise": CONFIGURED_SPEND_CAP // factor,
    }


def test_aggressive_derives_from_the_configured_runtime_policy(configured_runtime):
    """HARDENING TEST 4."""
    factor = SCENARIO_DERIVATION_FACTOR

    assert aggressive_scenario().parameters == {
        "max_interventions_per_customer_24h": CONFIGURED_LIMIT * factor,
        "event_cooldown_minutes": CONFIGURED_COOLDOWN // factor,
        "daily_spend_cap_paise": CONFIGURED_SPEND_CAP * factor,
    }


def test_the_derived_scenarios_say_what_they_derive_from():
    for scenario in (conservative_scenario(), aggressive_scenario()):
        assert scenario.source == SOURCE_DERIVED_FROM_ACTIVE_RUNTIME
        assert scenario.derived_from == "current"


def test_a_custom_scenario_is_labelled_operator_defined():
    assert custom_scenario(current_scenario().parameters).source == (
        SOURCE_OPERATOR_DEFINED
    )


def test_deriving_from_a_configured_policy_exposes_no_new_knob(
    configured_runtime,
):
    """Permissiveness moves; the locked stops are still not parameters at all.

    Reading the live policy must not widen the configuration surface — the
    three configurable controls stay the only three, whatever the environment
    says, so no scenario gains a way to name a protection.
    """
    from app.policy_scenario import CONFIGURABLE_PARAMETERS, IMMUTABLE_PROTECTIONS

    for scenario in (
        current_scenario(),
        conservative_scenario(),
        aggressive_scenario(),
    ):
        assert set(scenario.parameters) == set(CONFIGURABLE_PARAMETERS)
        assert not set(scenario.parameters) & set(IMMUTABLE_PROTECTIONS)
        assert scenario.to_dict()["immutable_protections"] == list(
            IMMUTABLE_PROTECTIONS
        )


# ---------------------------------------------------------------------------
# The CANONICAL BENCHMARK policy is a separate, frozen concept
# ---------------------------------------------------------------------------


def test_the_benchmark_policy_ignores_runtime_configuration(configured_runtime):
    """HARDENING TEST 7. The canonical benchmark must reproduce anywhere."""
    benchmark = frozen_policy_config()

    assert benchmark.max_interventions_per_customer_24h == (
        config_module.DEFAULT_POLICY_MAX_INTERVENTIONS_PER_CUSTOMER_24H
    )
    assert benchmark.event_cooldown_minutes == (
        config_module.DEFAULT_POLICY_EVENT_COOLDOWN_MINUTES
    )
    assert benchmark.daily_spend_cap_paise == (
        config_module.DEFAULT_POLICY_DAILY_SPEND_CAP_PAISE
    )


def test_the_canonical_benchmark_fingerprint_is_configuration_independent(
    configured_runtime,
):
    """HARDENING TEST 8, via the existing identity mechanism.

    Uses the benchmark's own fingerprint rather than a hardcoded revenue
    figure, so this stays a statement about reproducibility rather than a
    frozen number that would have to be edited to keep passing.
    """
    unconfigured = _fingerprint_without_policy_env()

    assert Phase17BenchmarkConfig().fingerprint() == unconfigured


def _fingerprint_without_policy_env() -> str:
    """The canonical fingerprint with every POLICY_* override removed."""
    import os

    saved = {
        name: os.environ.pop(name)
        for name in (
            "POLICY_MAX_INTERVENTIONS_PER_CUSTOMER_24H",
            "POLICY_EVENT_COOLDOWN_MINUTES",
            "POLICY_DAILY_SPEND_CAP_PAISE",
        )
        if name in os.environ
    }
    try:
        return Phase17BenchmarkConfig().fingerprint()
    finally:
        os.environ.update(saved)


def test_current_and_the_benchmark_policy_are_allowed_to_diverge(
    configured_runtime,
):
    """The whole point of the separation, stated as a test."""
    assert current_scenario().policy_config != frozen_policy_config()


def test_reading_the_current_scenario_does_not_touch_the_benchmark(
    configured_runtime,
):
    before = Phase17BenchmarkConfig().fingerprint()

    current_scenario()
    conservative_scenario()
    aggressive_scenario()
    scenario_catalog()

    assert Phase17BenchmarkConfig().fingerprint() == before


# ---------------------------------------------------------------------------
# Replay policy validation bounds are centralized and deterministic
# ---------------------------------------------------------------------------


def test_the_bounds_are_deterministic_and_configuration_independent(
    configured_runtime,
):
    """HARDENING TEST 5. The admissible policy space is a stated range.

    If the bounds moved with the environment, the lab's promise about what it
    will accept would depend on how the server was launched.
    """
    import os

    configured = {name: dict(b) for name, b in CUSTOM_BOUNDS.items()}
    for name in (
        "POLICY_MAX_INTERVENTIONS_PER_CUSTOMER_24H",
        "POLICY_EVENT_COOLDOWN_MINUTES",
        "POLICY_DAILY_SPEND_CAP_PAISE",
    ):
        os.environ.pop(name, None)

    assert {name: dict(b) for name, b in CUSTOM_BOUNDS.items()} == configured


def test_every_configurable_parameter_has_exactly_one_bound_definition():
    from app.policy_scenario import CONFIGURABLE_PARAMETERS

    assert set(CUSTOM_BOUNDS) == set(CONFIGURABLE_PARAMETERS)
    for name, bounds in CUSTOM_BOUNDS.items():
        assert set(bounds) == {"minimum", "maximum"}
        assert isinstance(bounds["minimum"], int)
        assert isinstance(bounds["maximum"], int)
        assert bounds["minimum"] <= bounds["maximum"]


def test_the_bounds_are_defined_only_in_the_scenario_module():
    """No second copy of the numbers in the API or the replay engine.

    A bound restated elsewhere is a bound that can drift out of agreement with
    the validator, which would let the UI advertise a policy the server
    refuses — or worse, imply a refusal the server does not actually make.
    """
    backend = Path(__file__).resolve().parents[1] / "app"
    owner = backend / "policy_scenario.py"

    for path in backend.rglob("*.py"):
        if path == owner:
            continue
        source = path.read_text()
        assert "CUSTOM_MIN_" not in source, f"{path.name} restates a replay bound"
        assert "CUSTOM_MAX_" not in source, f"{path.name} restates a replay bound"


def test_the_api_serves_the_bounds_so_the_client_never_owns_them():
    catalog = scenario_catalog()

    assert catalog["custom"]["bounds"] == {
        name: dict(bounds) for name, bounds in CUSTOM_BOUNDS.items()
    }


def test_the_custom_form_defaults_are_always_submittable(configured_runtime):
    """A prefill the server would reject is a broken form.

    The active policy can sit outside the replay bounds, so the starting values
    are clamped for presentation. Validation itself is untouched.
    """
    defaults = custom_form_defaults()

    for name, value in defaults.items():
        assert CUSTOM_BOUNDS[name]["minimum"] <= value
        assert value <= CUSTOM_BOUNDS[name]["maximum"]

    custom_scenario(defaults)


def test_clamping_the_form_does_not_soften_validation(monkeypatch):
    """An out-of-bounds policy is still refused when actually submitted."""
    beyond = CUSTOM_BOUNDS["max_interventions_per_customer_24h"]["maximum"] + 1
    monkeypatch.setenv("POLICY_MAX_INTERVENTIONS_PER_CUSTOMER_24H", str(beyond))

    # The Current scenario reports the truth, unclamped.
    assert current_scenario().parameters["max_interventions_per_customer_24h"] == (
        beyond
    )
    # The form starts from something the server will accept.
    assert custom_form_defaults()["max_interventions_per_customer_24h"] < beyond
    # And submitting the real value is still rejected.
    with pytest.raises(PolicyScenarioError):
        custom_scenario(
            {
                "max_interventions_per_customer_24h": beyond,
                "event_cooldown_minutes": 30,
                "daily_spend_cap_paise": 5_000_000,
            }
        )

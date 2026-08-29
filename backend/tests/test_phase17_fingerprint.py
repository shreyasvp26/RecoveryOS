"""Phase 17 configuration identity.

A benchmark that claims a frozen world must be able to prove which world it
ran. These tests assert that the hidden model and the event generator are both
cryptographically identified, that a change to either propagates into the
top-level configuration fingerprint, and that no volatile value can leak in.

Nothing here mutates a frozen coefficient permanently: sensitivity is measured
against locally rebuilt payloads and monkeypatched module state, restored by
pytest after each test.
"""

from __future__ import annotations

import json
import subprocess
import sys

import pytest

from app import generator as generator_module
from app import hidden_world as world_module
from app.benchmark_config import Phase17BenchmarkConfig
from app.generator import (
    EVENT_GENERATOR_METHODOLOGY_VERSION,
    event_generator_fingerprint,
)
from app.hidden_world import (
    HIDDEN_WORLD_METHODOLOGY_VERSION,
    hidden_world_fingerprint,
)

HEX_DIGEST_LENGTH = 32


# ---------------------------------------------------------------------------
# Test A — stability
# ---------------------------------------------------------------------------


def test_every_fingerprint_is_a_stable_hex_digest() -> None:
    for fingerprint in (
        hidden_world_fingerprint(),
        event_generator_fingerprint(),
        Phase17BenchmarkConfig().fingerprint(),
    ):
        assert isinstance(fingerprint, str)
        assert len(fingerprint) == HEX_DIGEST_LENGTH
        int(fingerprint, 16)


def test_repeated_calls_in_one_process_agree() -> None:
    assert hidden_world_fingerprint() == hidden_world_fingerprint()
    assert event_generator_fingerprint() == event_generator_fingerprint()
    assert Phase17BenchmarkConfig().fingerprint() == (
        Phase17BenchmarkConfig().fingerprint()
    )


def test_separate_interpreters_agree_despite_hash_randomization() -> None:
    """Test A and Test E.

    Two fresh interpreters with deliberately different ``PYTHONHASHSEED``
    values must produce identical fingerprints. This is the property Python's
    built-in ``hash`` cannot provide, and it also rules out dictionary
    iteration order as an input.
    """
    program = (
        "from app.benchmark_config import Phase17BenchmarkConfig;"
        "from app.generator import event_generator_fingerprint;"
        "from app.hidden_world import hidden_world_fingerprint;"
        "print(hidden_world_fingerprint(), event_generator_fingerprint(),"
        " Phase17BenchmarkConfig().fingerprint())"
    )

    def run(hash_seed: str) -> str:
        completed = subprocess.run(
            [sys.executable, "-c", program],
            capture_output=True,
            text=True,
            check=True,
            env={"PYTHONHASHSEED": hash_seed, "PATH": "", "PYTHONPATH": ""},
            cwd=str(generator_module.__file__).rsplit("/app/", 1)[0],
        )
        return completed.stdout.strip()

    assert run("0") == run("12345")
    assert run("0").split() == [
        hidden_world_fingerprint(),
        event_generator_fingerprint(),
        Phase17BenchmarkConfig().fingerprint(),
    ]


# ---------------------------------------------------------------------------
# Test B — hidden-world sensitivity
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "attribute, replacement",
    [
        ("BASE_TRUE_BPS", {"no_action": 501}),
        ("SUBSCRIPTION_TRUE_BPS", {"no_action": 301}),
        ("HIGH_VALUE_TRUE_BPS", {"payment_link": 301}),
        ("WORLD_RELIABLE_BPS", 501),
        ("WORLD_UNRELIABLE_MIN_FAILURES", 5),
        ("HIGH_VALUE_THRESHOLD_PAISE", 1_000_001),
        ("HIDDEN_WORLD_METHODOLOGY_VERSION", "phase17-signal-bearing-world-v2"),
        ("RANDOMIZATION_VERSION", "phase17-blake2b-uniform-v2"),
    ],
)
def test_changing_any_hidden_world_parameter_changes_its_fingerprint(
    monkeypatch: pytest.MonkeyPatch, attribute: str, replacement: object
) -> None:
    """Test B."""
    baseline = hidden_world_fingerprint()
    monkeypatch.setattr(world_module, attribute, replacement)
    assert hidden_world_fingerprint() != baseline


def test_changing_a_nested_intervention_coefficient_changes_the_fingerprint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A single cell of a nested table is enough. Test B."""
    baseline = hidden_world_fingerprint()
    table = {
        key: dict(value)
        for key, value in world_module.FAILURE_REASON_TRUE_BPS.items()
    }
    table["bank_timeout"]["retry_delayed"] += 1
    monkeypatch.setattr(world_module, "FAILURE_REASON_TRUE_BPS", table)
    assert hidden_world_fingerprint() != baseline


def test_the_production_coefficients_survive_the_sensitivity_tests() -> None:
    """The frozen tables are restored, and are still read-only."""
    assert world_module.FAILURE_REASON_TRUE_BPS["bank_timeout"]["retry_delayed"] == 2200
    assert world_module.BASE_TRUE_BPS["no_action"] == 500
    with pytest.raises(TypeError):
        world_module.BASE_TRUE_BPS["no_action"] = 9999  # type: ignore[index]


# ---------------------------------------------------------------------------
# Test C — event-generator sensitivity
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "attribute, replacement",
    [
        ("_PAYMENT_METHODS", ("upi", "card", "netbanking")),
        ("_FAILURE_REASONS", ("bank_timeout",)),
        ("_BANKS", ("HDFC",)),
        ("_RISK_FLAGS", ("normal",)),
        ("_MIN_AMOUNT_PAISE", 501),
        ("_MAX_AMOUNT_PAISE", 20001),
        ("_MAX_PRIOR_SUCCESSES", 41),
        ("_MAX_PRIOR_FAILURES", 7),
        ("_SUBSCRIPTION_RATE", 0.36),
        ("_EVENTS_PER_CUSTOMER", 4),
        ("_CURRENCY", "USD"),
        ("EVENT_GENERATOR_METHODOLOGY_VERSION", "phase4-seeded-uniform-v2"),
    ],
)
def test_changing_any_generation_parameter_changes_its_fingerprint(
    monkeypatch: pytest.MonkeyPatch, attribute: str, replacement: object
) -> None:
    """Test C."""
    baseline = event_generator_fingerprint()
    monkeypatch.setattr(generator_module, attribute, replacement)
    assert event_generator_fingerprint() != baseline


def test_shifting_the_timestamp_window_changes_the_fingerprint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Test C."""
    from datetime import datetime, timezone

    baseline = event_generator_fingerprint()
    monkeypatch.setattr(
        generator_module, "_WINDOW_END", datetime(2026, 8, 28, tzinfo=timezone.utc)
    )
    assert event_generator_fingerprint() != baseline


def test_the_fingerprint_describes_the_numbers_the_generator_actually_uses() -> None:
    """The named parameters are the ones in the draw, not a parallel copy."""
    events = generator_module.generate_events(seed=42, count=300)
    assert max(e.customer_history.prior_successful_payments for e in events) <= (
        generator_module._MAX_PRIOR_SUCCESSES
    )
    assert max(e.customer_history.prior_failed_payments for e in events) <= (
        generator_module._MAX_PRIOR_FAILURES
    )
    assert min(e.amount_paise for e in events) >= (
        generator_module._MIN_AMOUNT_PAISE * 100
    )
    assert max(e.amount_paise for e in events) <= (
        generator_module._MAX_AMOUNT_PAISE * 100
    )
    pool_size = (
        300 + generator_module._EVENTS_PER_CUSTOMER - 1
    ) // generator_module._EVENTS_PER_CUSTOMER
    # Distinct customers cannot exceed the pool, and some are never drawn.
    assert 0 < len({e.customer_id for e in events}) <= pool_size


def test_the_generator_fingerprint_excludes_seed_and_count() -> None:
    """Seed and count are per-run configuration, recorded separately."""
    before = event_generator_fingerprint()
    generator_module.generate_events(seed=1337, count=17)
    assert event_generator_fingerprint() == before
    assert Phase17BenchmarkConfig(event_count=17, event_seed=1337).to_dict()[
        "event_generator_fingerprint"
    ] == before


# ---------------------------------------------------------------------------
# Test D — propagation into the top-level configuration identity
# ---------------------------------------------------------------------------


def test_the_config_records_both_methodology_fingerprints() -> None:
    payload = Phase17BenchmarkConfig().to_dict()
    assert payload["hidden_world_fingerprint"] == hidden_world_fingerprint()
    assert payload["event_generator_fingerprint"] == event_generator_fingerprint()
    assert payload["hidden_world_methodology_version"] == (
        HIDDEN_WORLD_METHODOLOGY_VERSION
    )
    assert payload["event_generator_methodology_version"] == (
        EVENT_GENERATOR_METHODOLOGY_VERSION
    )


def test_a_hidden_world_change_reaches_the_config_fingerprint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Test D — the gap this hardening closed."""
    baseline = Phase17BenchmarkConfig().fingerprint()
    table = dict(world_module.BASE_TRUE_BPS)
    table["payment_link"] += 1
    monkeypatch.setattr(world_module, "BASE_TRUE_BPS", table)
    assert Phase17BenchmarkConfig().fingerprint() != baseline


def test_an_event_generator_change_reaches_the_config_fingerprint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Test D."""
    baseline = Phase17BenchmarkConfig().fingerprint()
    monkeypatch.setattr(generator_module, "_MAX_AMOUNT_PAISE", 30000)
    assert Phase17BenchmarkConfig().fingerprint() != baseline


def test_the_run_id_carries_the_strengthened_fingerprint() -> None:
    config = Phase17BenchmarkConfig()
    assert config.fingerprint() in config.run_id()


# ---------------------------------------------------------------------------
# Test E — no volatile inputs
# ---------------------------------------------------------------------------


def test_no_fingerprint_payload_contains_a_volatile_value() -> None:
    """No wall clock, no uuid, no pid, no filesystem path.

    Checked against the serialized payloads rather than the digests, because a
    digest hides everything and would make this assertion vacuous.
    """
    payload = json.dumps(Phase17BenchmarkConfig().to_dict())
    forbidden = ("/Users", "/home", "/tmp", "uuid", "pid", "hostname")
    for term in forbidden:
        assert term not in payload

    for module, names in (
        (world_module, ("hidden_world_fingerprint",)),
        (generator_module, ("event_generator_fingerprint",)),
    ):
        source = __import__("inspect").getsource(getattr(module, names[0]))
        for banned in ("time.", "datetime.now", "uuid", "os.getpid", "__file__", "hash("):
            assert banned not in source


def test_the_only_time_in_the_config_is_the_frozen_evaluation_instant() -> None:
    config = Phase17BenchmarkConfig()
    assert config.to_dict()["evaluation_time"] == config.evaluated_at
    assert Phase17BenchmarkConfig().fingerprint() == config.fingerprint()

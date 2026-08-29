"""Phase 19: replay metrics, scenario comparison and event-level decision deltas."""

from __future__ import annotations

import pytest

from app.benchmark_config import Phase17BenchmarkConfig
from app.policy import (
    RULE_COOLDOWN,
    RULE_CUSTOMER_LIMIT,
    RULE_DUPLICATE,
    RULE_FRAUD,
    RULE_SPEND_CAP,
    RULE_TERMINAL,
)
from app.policy_scenario import (
    IMMUTABLE_PROTECTIONS,
    aggressive_scenario,
    built_in_scenarios,
    conservative_scenario,
    current_scenario,
    custom_scenario,
)
from app.replay import (
    REPLAY_MODE_SIMULATED,
    build_replay_contexts,
    replay_scenario,
    replay_scenarios,
)
from app.replay_metrics import (
    ALL_POLICY_RULES,
    DELTA_NEWLY_BLOCKED,
    blocks_by_rule,
    compare_replays,
    decision_deltas,
    interventions_by_type,
    recoverable_revenue_paise,
    replay_metrics,
    rule_activity,
    simulated_recovered_revenue_paise,
    unrecovered_revenue_paise,
    verify_comparison_fairness,
)
from app.generator import generate_events

SMALL = 60


def small_config(**overrides) -> Phase17BenchmarkConfig:
    defaults = {"event_count": SMALL}
    defaults.update(overrides)
    return Phase17BenchmarkConfig(**defaults)


@pytest.fixture(scope="module")
def canonical_results():
    """One canonical comparison, reused across the read-only metric tests."""
    return replay_scenarios(built_in_scenarios())


# ---------------------------------------------------------------------------
# Financial
# ---------------------------------------------------------------------------


def test_recovered_revenue_is_the_sum_of_the_realized_outcomes(canonical_results):
    for result in canonical_results:
        expected = sum(r.recovered_amount_paise for r in result.records)
        assert simulated_recovered_revenue_paise(result.records) == expected


def test_recoverable_and_unrecovered_revenue_reconcile(canonical_results):
    for result in canonical_results:
        records = result.records
        assert (
            simulated_recovered_revenue_paise(records)
            + unrecovered_revenue_paise(records)
            == recoverable_revenue_paise(records)
        )


def test_every_money_figure_is_an_integer_paise_value(canonical_results):
    metrics = replay_metrics(canonical_results[0]).to_dict()
    for key, value in metrics["financial"].items():
        if key.endswith("_paise"):
            assert isinstance(value, int) and not isinstance(value, bool), key
    assert isinstance(
        metrics["intervention"]["intervention_spend_paise"], int
    )


def test_rates_are_between_zero_and_one(canonical_results):
    for result in canonical_results:
        metrics = replay_metrics(result)
        for rate in (metrics.recovery_rate, metrics.revenue_recovery_rate):
            assert rate is None or 0.0 <= rate <= 1.0


def test_recovery_rate_is_none_when_nothing_was_processed():
    from app.replay_metrics import recovery_rate

    assert recovery_rate([]) is None


# ---------------------------------------------------------------------------
# Intervention
# ---------------------------------------------------------------------------


def test_intervention_counts_agree_with_the_records(canonical_results):
    for result in canonical_results:
        metrics = replay_metrics(result)
        performed = [r for r in result.records if r.attempted]
        assert metrics.total_interventions == len(performed)
        assert sum(metrics.interventions_by_type.values()) == len(performed)


def test_no_action_is_never_counted_as_an_intervention(canonical_results):
    for result in canonical_results:
        assert "no_action" not in interventions_by_type(result.records)


def test_efficiency_is_none_without_a_denominator():
    from app.replay_metrics import intervention_efficiency_paise

    assert intervention_efficiency_paise([]) is None


def test_interventions_per_customer_is_none_without_a_denominator():
    from app.replay_metrics import interventions_per_customer

    assert interventions_per_customer([]) is None


def test_every_event_is_accounted_for_exactly_once(canonical_results):
    for result in canonical_results:
        metrics = replay_metrics(result)
        assert metrics.processed + metrics.failures == metrics.event_count
        assert (
            metrics.total_interventions
            + metrics.no_action_events
            + metrics.failures
            == metrics.event_count
        )


# ---------------------------------------------------------------------------
# Safety
# ---------------------------------------------------------------------------


def test_blocks_by_rule_covers_every_rule_the_engine_can_produce(
    canonical_results,
):
    for result in canonical_results:
        counts = blocks_by_rule(result.records)
        assert set(counts) == set(ALL_POLICY_RULES)
        assert set(ALL_POLICY_RULES) == {
            RULE_FRAUD,
            RULE_TERMINAL,
            RULE_DUPLICATE,
            RULE_CUSTOMER_LIMIT,
            RULE_COOLDOWN,
            RULE_SPEND_CAP,
        }


def test_block_counts_sum_to_the_total(canonical_results):
    for result in canonical_results:
        counts = blocks_by_rule(result.records)
        total = sum(r.blocked_count for r in result.records)
        assert sum(counts.values()) == total


def test_fraud_and_terminal_blocks_are_reported_under_every_scenario(
    canonical_results,
):
    for result in canonical_results:
        counts = blocks_by_rule(result.records)
        assert counts[RULE_FRAUD] > 0
        assert counts[RULE_TERMINAL] > 0


def test_no_scenario_ever_intervenes_on_fraud_or_terminal(canonical_results):
    for result in canonical_results:
        metrics = replay_metrics(result)
        assert metrics.fraud_interventions == 0
        assert metrics.terminal_interventions == 0
        assert metrics.unauthorized_attempts == 0


def test_rule_activity_marks_the_immutable_protections(canonical_results):
    activity = rule_activity(canonical_results[0].records)

    for protection in IMMUTABLE_PROTECTIONS:
        assert activity[protection]["immutable"] is True
        assert activity[protection]["configured_by"] is None


def test_rule_activity_names_the_parameter_behind_each_configurable_rule(
    canonical_results,
):
    activity = rule_activity(canonical_results[0].records)

    assert (
        activity[RULE_CUSTOMER_LIMIT]["configured_by"]
        == "max_interventions_per_customer_24h"
    )
    assert activity[RULE_COOLDOWN]["configured_by"] == "event_cooldown_minutes"
    assert activity[RULE_SPEND_CAP]["configured_by"] == "daily_spend_cap_paise"


def test_rule_activity_reports_load_bearing_from_data_not_from_prose(
    canonical_results,
):
    """A knob that could not have moved this result says so plainly."""
    activity = rule_activity(canonical_results[0].records)

    for rule, entry in activity.items():
        assert entry["load_bearing"] == (entry["blocked"] > 0), rule


# ---------------------------------------------------------------------------
# Failure accounting
# ---------------------------------------------------------------------------


def test_failures_are_reported_by_category_and_never_as_recovery():
    class BrokenClassifier:
        def generate(self, prompt: str) -> str:
            raise RuntimeError("classifier unavailable")

    contexts = build_replay_contexts(
        generate_events(seed=42, count=5), BrokenClassifier()
    )
    result = replay_scenario(
        current_scenario(), config=small_config(event_count=5), contexts=contexts
    )
    metrics = replay_metrics(result)

    assert metrics.failures == 5
    assert metrics.failures_by_category["classification_failure"] == 5
    assert metrics.simulated_recovered_revenue_paise == 0
    assert metrics.recovered_events == 0
    assert metrics.processed == 0


def test_a_partial_failure_leaves_the_successful_events_intact():
    """Some events fail, the rest still produce honest results."""
    from app.benchmark import DeterministicClassifier

    class FlakyClassifier(DeterministicClassifier):
        def __init__(self) -> None:
            self.seen = 0

        def generate(self, prompt: str) -> str:
            self.seen += 1
            if self.seen % 5 == 0:
                raise RuntimeError("intermittent classifier failure")
            return super().generate(prompt)

    contexts = build_replay_contexts(
        generate_events(seed=42, count=20), FlakyClassifier()
    )
    result = replay_scenario(
        current_scenario(), config=small_config(event_count=20), contexts=contexts
    )
    metrics = replay_metrics(result)

    assert metrics.failures == 4
    assert metrics.processed == 16
    assert metrics.failures + metrics.processed == 20


def test_the_canonical_comparison_has_zero_failures(canonical_results):
    for result in canonical_results:
        assert replay_metrics(result).failures == 0


# ---------------------------------------------------------------------------
# Decision deltas
# ---------------------------------------------------------------------------


def test_deltas_are_keyed_by_event_id(canonical_results):
    current, conservative, _aggressive = canonical_results
    deltas = decision_deltas(current, conservative)

    assert deltas
    reference = current.by_event()
    for delta in deltas:
        assert delta.event_id in reference
        assert delta.amount_paise == reference[delta.event_id].amount_paise


def test_deltas_report_only_genuine_differences(canonical_results):
    current, conservative, _aggressive = canonical_results
    deltas = decision_deltas(current, conservative)

    reference = current.by_event()
    candidate = conservative.by_event()
    changed_ids = {d.event_id for d in deltas}
    for event_id in reference:
        identical = (
            reference[event_id].selected_intervention
            == candidate[event_id].selected_intervention
            and reference[event_id].allowed_candidates
            == candidate[event_id].allowed_candidates
            and reference[event_id].attempted == candidate[event_id].attempted
            and (reference[event_id].failure is None)
            == (candidate[event_id].failure is None)
        )
        assert identical == (event_id not in changed_ids), event_id


def test_a_scenario_compared_with_itself_produces_no_deltas():
    result = replay_scenario(current_scenario(), config=small_config())
    assert decision_deltas(result, result) == ()


def test_a_newly_blocked_delta_names_the_rule_that_blocked_it(
    canonical_results,
):
    """The demo delta: same event, only the policy changed."""
    current, conservative, _aggressive = canonical_results
    deltas = [
        d
        for d in decision_deltas(current, conservative)
        if d.delta_type == DELTA_NEWLY_BLOCKED
    ]

    assert deltas
    for delta in deltas:
        assert delta.reference_selected != "no_action"
        assert delta.candidate_selected == "no_action"
        assert delta.candidate_denial_reason == RULE_CUSTOMER_LIMIT
        assert delta.reference_denial_reason is None


def test_a_denial_reason_is_only_reported_when_policy_authorized_nothing(
    canonical_results,
):
    """A no_action with options available is economics, not a policy stop."""
    from app.replay_metrics import _blocking_reason

    for result in canonical_results:
        for record in result.records:
            if record.allowed_candidates:
                assert _blocking_reason(record) is None


def test_deltas_are_returned_in_canonical_event_order(canonical_results):
    current, conservative, _aggressive = canonical_results
    deltas = decision_deltas(current, conservative)

    assert [d.event_id for d in deltas] == sorted(d.event_id for d in deltas)


def test_comparing_different_event_sets_is_refused():
    a = replay_scenario(current_scenario(), config=small_config(event_count=10))
    b = replay_scenario(current_scenario(), config=small_config(event_count=12))

    with pytest.raises(ValueError, match="different event sets"):
        decision_deltas(a, b)


def test_delta_serialization_carries_both_sides(canonical_results):
    current, conservative, _aggressive = canonical_results
    delta = decision_deltas(current, conservative)[0].to_dict()

    assert set(delta["reference"]) == set(delta["candidate"])
    assert "simulated_recovered_amount_paise" in delta["reference"]


# ---------------------------------------------------------------------------
# Comparison
# ---------------------------------------------------------------------------


def test_comparison_marks_exactly_one_reference(canonical_results):
    comparison = compare_replays(canonical_results, "current")
    references = [s for s in comparison["scenarios"] if s["is_reference"]]

    assert len(references) == 1
    assert references[0]["scenario"]["scenario_id"] == "current"


def test_the_reference_has_zero_incremental_against_itself(canonical_results):
    comparison = compare_replays(canonical_results, "current")
    reference = next(s for s in comparison["scenarios"] if s["is_reference"])

    assert reference["vs_reference"]["incremental_recovered_revenue_paise"] == 0
    assert reference["decision_deltas"] == []


def test_incremental_recovery_is_computed_in_integer_paise(canonical_results):
    comparison = compare_replays(canonical_results, "current")

    for entry in comparison["scenarios"]:
        value = entry["vs_reference"]["incremental_recovered_revenue_paise"]
        assert isinstance(value, int) and not isinstance(value, bool)


def test_incremental_recovery_matches_the_underlying_metrics(canonical_results):
    comparison = compare_replays(canonical_results, "current")
    reference_revenue = next(
        s["metrics"]["financial"]["simulated_recovered_revenue_paise"]
        for s in comparison["scenarios"]
        if s["is_reference"]
    )

    for entry in comparison["scenarios"]:
        revenue = entry["metrics"]["financial"][
            "simulated_recovered_revenue_paise"
        ]
        assert (
            entry["vs_reference"]["incremental_recovered_revenue_paise"]
            == revenue - reference_revenue
        )


def test_comparison_publishes_every_fairness_check_as_passing(canonical_results):
    comparison = compare_replays(canonical_results, "current")

    assert comparison["fairness"]
    assert all(comparison["fairness"].values())


def test_fairness_verification_detects_a_different_event_set():
    a = replay_scenario(current_scenario(), config=small_config(event_count=10))
    b = replay_scenario(
        conservative_scenario(), config=small_config(event_count=12)
    )

    checks = verify_comparison_fairness((a, b))
    assert checks["same_event_set"] is False


def test_comparison_refuses_an_unfair_pairing():
    a = replay_scenario(current_scenario(), config=small_config(event_count=10))
    b = replay_scenario(
        conservative_scenario(), config=small_config(event_count=12)
    )

    with pytest.raises(ValueError, match="cannot be compared causally"):
        compare_replays((a, b), "current")


def test_comparison_requires_the_reference_to_be_present(canonical_results):
    with pytest.raises(ValueError, match="is not among the replayed scenarios"):
        compare_replays(canonical_results, "nonexistent")


def test_comparison_rejects_duplicate_scenario_ids():
    result = replay_scenario(current_scenario(), config=small_config())

    with pytest.raises(ValueError, match="unique"):
        compare_replays((result, result), "current")


def test_comparison_requires_at_least_one_result():
    with pytest.raises(ValueError):
        compare_replays((), "current")


def test_comparison_is_labelled_simulated_throughout(canonical_results):
    comparison = compare_replays(canonical_results, "current")

    assert comparison["replay_mode"] == REPLAY_MODE_SIMULATED
    assert comparison["result_type"] == "simulated_policy_replay"
    assert "not production revenue forecasts" in comparison["disclaimer"]
    for entry in comparison["scenarios"]:
        assert entry["metrics"]["replay_mode"] == REPLAY_MODE_SIMULATED
        assert entry["identity"]["replay_mode"] == REPLAY_MODE_SIMULATED


def test_comparison_never_uses_the_words_actual_recovered_revenue(
    canonical_results,
):
    """Replay revenue is never presented as production revenue."""
    import json

    payload = json.dumps(compare_replays(canonical_results, "current")).lower()

    assert "actual_recovered_revenue" not in payload
    assert "actual recovered revenue" not in payload
    assert "simulated_recovered_revenue_paise" in payload


def test_comparison_exposes_no_hidden_ground_truth(canonical_results):
    import json

    payload = json.dumps(compare_replays(canonical_results, "current")).lower()

    for leak in (
        "true_probability",
        "true_ev",
        "draw_bps",
        "oracle",
        "hidden_world",
        "recovery_probability_bps",
    ):
        assert leak not in payload, leak


def test_comparison_includes_the_policy_identity_of_every_scenario(
    canonical_results,
):
    comparison = compare_replays(canonical_results, "current")
    fingerprints = {
        s["identity"]["policy_fingerprint"] for s in comparison["scenarios"]
    }

    assert len(fingerprints) == len(comparison["scenarios"])


def test_comparison_is_deterministic(canonical_results):
    first = compare_replays(canonical_results, "current")
    second = compare_replays(canonical_results, "current")

    assert first == second


def test_a_custom_scenario_participates_in_a_comparison():
    scenarios = (
        current_scenario(),
        custom_scenario(
            {
                "max_interventions_per_customer_24h": 1,
                "event_cooldown_minutes": 30,
                "daily_spend_cap_paise": 5_000_000,
            },
            name="Tighter limit",
        ),
    )
    comparison = compare_replays(
        replay_scenarios(scenarios, config=small_config()), "current"
    )

    assert [s["scenario"]["scenario_id"] for s in comparison["scenarios"]] == [
        "current",
        "custom",
    ]
    assert all(comparison["fairness"].values())


def test_aggressive_never_weakens_a_safety_stop(canonical_results):
    """More permissive means wider thresholds, never fewer protections."""
    current, _conservative, aggressive = canonical_results
    current_blocks = blocks_by_rule(current.records)
    aggressive_blocks = blocks_by_rule(aggressive.records)

    for protection in IMMUTABLE_PROTECTIONS:
        assert aggressive_blocks[protection] == current_blocks[protection]

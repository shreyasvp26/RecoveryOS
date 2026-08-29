"""Phase 17 benchmark report: fairness verification, summary, and verdict.

Turns a completed run into (a) a machine-readable summary, (b) a human-readable
table, and (c) an explicit verdict that is allowed to say V2 lost.

THE VERDICT RULE IS FROZEN
--------------------------
Declared before any Phase 17 result was observed, and stated here so it cannot
be quietly reinterpreted afterwards:

* The criterion is TOTAL TRUE ECONOMIC VALUE, not realized revenue. Realized
  revenue is a sum of 500 Bernoulli draws and moves by tens of thousands of
  paise on luck alone; true EV is what the decision actually controlled.
* The comparison is V2 total true EV minus V1 total true EV.
* Materiality is 1% of the value that was actually at stake in the decisions,
  i.e. 1% of (Oracle total true EV - No Action total true EV). A difference
  smaller than that is reported as a tie, not spun as a win.

FAIRNESS IS VERIFIED, NOT ASSERTED
----------------------------------
The fairness section re-executes the run with the arms reversed, with the
events reversed, and again unchanged, and compares the canonical output. These
are computed checks whose failure is visible in the report, not prose claims.
"""

from __future__ import annotations

import json
from typing import Any, Mapping

from .benchmark_config import Phase17BenchmarkConfig
from .benchmark_phase17 import (
    CANONICAL_STRATEGY_ORDER,
    STRATEGY_LABELS,
    STRATEGY_NO_ACTION,
    STRATEGY_ORACLE,
    STRATEGY_V1,
    STRATEGY_V2,
    Phase17BenchmarkReport,
    run_phase17_benchmark,
)
from .benchmark_phase17_metrics import (
    all_strategy_metrics,
    selection_disagreements,
)

VERDICT_V2_WON = "V2 WON"
VERDICT_V2_LOST = "V2 LOST"
VERDICT_NOT_YET = "NOT YET BETTER"

# 1% of the decision value at stake. Frozen before results; see module docstring.
VERDICT_MATERIALITY_BPS = 100


def rupees(paise: int | None) -> str:
    """Format integer paise as rupees for human-readable output only."""
    if paise is None:
        return "n/a"
    sign = "-" if paise < 0 else ""
    value = abs(paise)
    return f"{sign}Rs {value // 100:,}.{value % 100:02d}"


def _pct(value: float | None) -> str:
    return "n/a" if value is None else f"{100.0 * value:.1f}%"


def canonical_records(report: Phase17BenchmarkReport) -> dict[str, Any]:
    """The order-independent canonical view of a run, for equality checks.

    Records are keyed by strategy and then by event id, so two runs that
    executed their arms or their events in different orders serialize
    identically if and only if they genuinely produced the same results.
    """
    return {
        strategy: {
            record.event_id: record.to_dict()
            for record in report.for_strategy(strategy)
        }
        for strategy in CANONICAL_STRATEGY_ORDER
    }


def canonical_json(report: Phase17BenchmarkReport) -> str:
    """A byte-stable serialization of a run's canonical records."""
    return json.dumps(canonical_records(report), sort_keys=True, separators=(",", ":"))


def verify_fairness(report: Phase17BenchmarkReport) -> dict[str, bool]:
    """Re-run the benchmark under adversarial permutations and compare.

    Each check re-executes the identical frozen configuration with one thing
    deliberately disturbed. Passing means the disturbance changed nothing.
    """
    baseline = canonical_json(report)
    config = report.config

    reversed_arms = run_phase17_benchmark(
        config, order=tuple(reversed(CANONICAL_STRATEGY_ORDER))
    )
    reversed_events = run_phase17_benchmark(
        config, events=tuple(reversed(report.events))
    )
    replay = run_phase17_benchmark(config)

    return {
        "strategy_order_invariant": canonical_json(reversed_arms) == baseline,
        "event_order_invariant": canonical_json(reversed_events) == baseline,
        "deterministic_replay": canonical_json(replay) == baseline,
        "same_event_set_for_every_arm": all(
            tuple(record.event_id for record in report.for_strategy(strategy))
            == tuple(event.event_id for event in report.events)
            for strategy in CANONICAL_STRATEGY_ORDER
        ),
        "same_policy_boundary_for_every_arm": all(
            {
                record.event_id: record.allowed_candidates
                for record in report.for_strategy(strategy)
            }
            == {
                record.event_id: record.allowed_candidates
                for record in report.for_strategy(CANONICAL_STRATEGY_ORDER[0])
            }
            for strategy in CANONICAL_STRATEGY_ORDER
        ),
        "same_hidden_world_for_every_arm": _same_hidden_world(report),
    }


def _same_hidden_world(report: Phase17BenchmarkReport) -> bool:
    """Two arms that chose the same action on an event must see the same truth."""
    truth: dict[tuple[str, str], tuple[int, bool]] = {}
    for strategy in CANONICAL_STRATEGY_ORDER:
        for record in report.for_strategy(strategy):
            if record.true_probability_bps is None:
                continue
            key = (record.event_id, record.selected_intervention)
            observed = (record.true_probability_bps, record.recovered)
            if truth.setdefault(key, observed) != observed:
                return False
    return True


def _verdict(metrics: Mapping[str, Any]) -> dict[str, Any]:
    """Apply the frozen verdict rule to a run's aggregate metrics."""
    v1_ev = metrics[STRATEGY_V1].total_true_ev_paise
    v2_ev = metrics[STRATEGY_V2].total_true_ev_paise
    oracle_ev = metrics[STRATEGY_ORACLE].total_true_ev_paise
    baseline_ev = metrics[STRATEGY_NO_ACTION].total_true_ev_paise

    delta = v2_ev - v1_ev
    at_stake = oracle_ev - baseline_ev
    materiality = abs(at_stake) * VERDICT_MATERIALITY_BPS // 10_000

    if delta > materiality:
        verdict = VERDICT_V2_WON
    elif delta < -materiality:
        verdict = VERDICT_V2_LOST
    else:
        verdict = VERDICT_NOT_YET

    return {
        "verdict": verdict,
        "criterion": "total true economic value (V2 - V1)",
        "v2_minus_v1_true_ev_paise": delta,
        "decision_value_at_stake_paise": at_stake,
        "materiality_threshold_paise": materiality,
        "materiality_rule": (
            f"{VERDICT_MATERIALITY_BPS} bps of (Oracle - No Action) true EV"
        ),
        "v2_minus_v1_recovered_revenue_paise": (
            metrics[STRATEGY_V2].recovered_revenue_paise
            - metrics[STRATEGY_V1].recovered_revenue_paise
        ),
    }


def summarize_report(
    report: Phase17BenchmarkReport, *, verify: bool = True
) -> dict[str, Any]:
    """Assemble the complete machine-readable Phase 17 summary."""
    metrics = all_strategy_metrics(report)
    disagreements = selection_disagreements(report)
    return {
        "run_id": report.run_id,
        "config": report.config.to_dict(),
        "config_fingerprint": report.config.fingerprint(),
        "executed_order": list(report.executed_order),
        "strategies": {
            strategy: metrics[strategy].to_dict()
            for strategy in CANONICAL_STRATEGY_ORDER
        },
        "v1_v2_disagreements": len(disagreements),
        "v1_v2_disagreement_sample": [
            {"event_id": event_id, "v1": v1, "v2": v2}
            for event_id, v1, v2 in disagreements[:10]
        ],
        "fairness": verify_fairness(report) if verify else None,
        "safety": {
            "recoveryos_fraud_intervention_rate": {
                strategy: metrics[strategy].fraud_intervention_rate
                for strategy in (STRATEGY_V1, STRATEGY_V2)
            },
            "recoveryos_unauthorized_attempts": {
                strategy: metrics[strategy].unauthorized_attempts
                for strategy in (STRATEGY_V1, STRATEGY_V2)
            },
            "total_exceptions": {
                strategy: metrics[strategy].exceptions
                for strategy in CANONICAL_STRATEGY_ORDER
            },
        },
        "result": _verdict(metrics),
    }


def format_report(summary: Mapping[str, Any]) -> str:
    """Render the human-readable Phase 17 report."""
    config = summary["config"]
    strategies = summary["strategies"]
    result = summary["result"]
    fairness = summary["fairness"] or {}

    lines = [
        "PHASE 17 BENCHMARK",
        "==================",
        f"Methodology:      {config['methodology']}",
        f"Events:           {config['event_count']}",
        f"Event seed:       {config['event_seed']}",
        f"Outcome seed:     {config['outcome_seed']}",
        f"Randomization:    {config['randomization_version']}",
        f"Config:           {summary['config_fingerprint']}",
        f"Evaluation:       {config['evaluation_mode']}"
        "  (synthetic; not production Razorpay recovery)",
        "",
        f"{'Strategy':<16}{'Revenue':>16}{'vs No Action':>16}"
        f"{'Attempts':>10}{'True EV':>16}{'Regret':>16}{'Optimal':>9}",
        "-" * 99,
    ]
    for strategy in CANONICAL_STRATEGY_ORDER:
        metric = strategies[strategy]
        lines.append(
            f"{STRATEGY_LABELS[strategy]:<16}"
            f"{rupees(metric['recovered_revenue_paise']):>16}"
            f"{rupees(metric['incremental_vs_no_action_paise']):>16}"
            f"{metric['interventions_attempted']:>10}"
            f"{rupees(metric['total_true_ev_paise']):>16}"
            f"{rupees(metric['total_regret_paise']):>16}"
            f"{_pct(metric['optimal_selection_rate']):>9}"
        )

    v1 = strategies[STRATEGY_V1]
    v2 = strategies[STRATEGY_V2]
    lines += [
        "",
        f"V2 vs V1 (revenue):        {rupees(v2['incremental_vs_v1_paise'])}",
        f"V2 vs No Action (revenue): {rupees(v2['incremental_vs_no_action_paise'])}",
        f"V2 vs V1 (true EV):        "
        f"{rupees(result['v2_minus_v1_true_ev_paise'])}",
        f"V2 optimal-selection:      {_pct(v2['optimal_selection_rate'])}"
        f"  (V1 {_pct(v1['optimal_selection_rate'])})",
        f"V2 total regret:           {rupees(v2['total_regret_paise'])}"
        f"  (V1 {rupees(v1['total_regret_paise'])})",
        f"V2 average regret:         "
        f"{rupees(int(v2['average_regret_paise'] or 0))}",
        f"V2 median regret:          "
        f"{rupees(int(v2['median_regret_paise'] or 0))}",
        f"V2 oracle capture (gross): {_pct(v2['oracle_value_capture'])}",
        f"V2 oracle capture (incr.): "
        f"{_pct(v2['incremental_oracle_value_capture'])}"
        f"  (V1 {_pct(v1['incremental_oracle_value_capture'])})",
        f"V1/V2 selection disagreements: {summary['v1_v2_disagreements']}",
        "",
        "Safety",
        f"  fraud intervention rate  V1={_pct(v1['fraud_intervention_rate'])} "
        f"V2={_pct(v2['fraud_intervention_rate'])}",
        f"  unauthorized executions  V1={v1['unauthorized_attempts']} "
        f"V2={v2['unauthorized_attempts']}",
        f"  false-intervention rate  V1={_pct(v1['false_intervention_rate'])} "
        f"V2={_pct(v2['false_intervention_rate'])}",
        f"  negative-EV rate         V1={_pct(v1['negative_ev_intervention_rate'])} "
        f"V2={_pct(v2['negative_ev_intervention_rate'])}",
        f"  exceptions               "
        + " ".join(
            f"{STRATEGY_LABELS[s]}={strategies[s]['exceptions']}"
            for s in CANONICAL_STRATEGY_ORDER
        ),
        "",
        "Fairness",
    ]
    for check, passed in sorted(fairness.items()):
        lines.append(f"  {check:<34} {'PASS' if passed else 'FAIL'}")
    lines += [
        "",
        f"Result: {result['verdict']}",
        f"  criterion  {result['criterion']}",
        f"  difference {rupees(result['v2_minus_v1_true_ev_paise'])}",
        f"  materiality {rupees(result['materiality_threshold_paise'])}"
        f" ({result['materiality_rule']})",
    ]
    return "\n".join(lines)


def run_and_summarize(
    config: Phase17BenchmarkConfig | None = None, *, verify: bool = True
) -> dict[str, Any]:
    """Convenience: run the canonical benchmark and summarize it."""
    return summarize_report(run_phase17_benchmark(config), verify=verify)

"""Phase 17 evaluation metrics — pure functions over per-event records.

Every metric is a pure function of the benchmark records. No metric mutates
state, runs a strategy, or influences a decision. Every denominator is stated
explicitly, and a metric that cannot be computed honestly returns ``None``
rather than a plausible-looking zero.

MONEY
-----
Integer paise everywhere. The only floats are ratios and rates, which are
presentation values and never feed money arithmetic.

REGRET IS A PRIMARY METRIC
--------------------------
Optimal-selection rate alone is a bad summary: an arm that picks a ₹790 action
when ₹800 was available looks identical to one that picks a ₹5 action. Regret
in paise is reported alongside it and is the figure to read first.
"""

from __future__ import annotations

from dataclasses import dataclass
from statistics import median
from typing import Any, Mapping, Sequence

from .benchmark_phase17 import (
    EXCEPTION_CATEGORIES,
    POLICY_BOUNDED_STRATEGIES,
    Phase17BenchmarkReport,
    StrategyEventRecord,
    BenchmarkIntegrityError,
)
from .models import PaymentEvent
from .selector import NO_ACTION


def _scored(records: Sequence[StrategyEventRecord]) -> list[StrategyEventRecord]:
    """Records that produced a real outcome (an exception produced none)."""
    return [record for record in records if record.exception is None]


def _performed(records: Sequence[StrategyEventRecord]) -> list[StrategyEventRecord]:
    """Records on which the arm really performed an intervention.

    Includes an attempt that succeeded and was then followed by a failure in
    outcome realization. The action was taken and its cost was incurred; the
    benchmark must not lose it from the intervention count merely because the
    world subsequently failed to report whether it worked.
    """
    return [record for record in records if record.attempted]


def _attempts(records: Sequence[StrategyEventRecord]) -> list[StrategyEventRecord]:
    """Performed interventions that also produced a scoreable outcome.

    The denominator for the value-based rates (false-intervention, negative-EV),
    which are undefined on a record carrying no true EV. It is deliberately
    narrower than :func:`_performed`.
    """
    return [record for record in _scored(records) if record.attempted]


# ---------------------------------------------------------------------------
# A. revenue, C/D. counts, E. efficiency
# ---------------------------------------------------------------------------


def recovered_revenue_paise(records: Sequence[StrategyEventRecord]) -> int:
    """Metric A — simulated recovered revenue, ``sum(recovered_amount_paise)``."""
    return sum(record.recovered_amount_paise for record in records)


def recovered_events(records: Sequence[StrategyEventRecord]) -> int:
    """Number of events on which money was simulated as recovered."""
    return sum(1 for record in records if record.recovered)


def interventions_attempted(records: Sequence[StrategyEventRecord]) -> int:
    """Metric D — number of attempted interventions.

    Counts every intervention actually performed, including one whose outcome
    realization later failed. Recovery is credited separately and never to a
    failed record, so the count and the revenue stay independently honest.
    """
    return len(_performed(records))


def scoreable_interventions(records: Sequence[StrategyEventRecord]) -> int:
    """Performed interventions carrying a true EV, the value-rate denominator."""
    return len(_attempts(records))


def recovery_efficiency_paise(
    records: Sequence[StrategyEventRecord],
) -> float | None:
    """Metric E — ``recovered_paise / interventions_attempted``.

    Returns None (never 0.0) when the arm attempted nothing: the No Action
    control has no efficiency, it has no denominator.
    """
    attempts = interventions_attempted(records)
    if attempts == 0:
        return None
    return recovered_revenue_paise(records) / attempts


def incremental_paise(
    subject: Sequence[StrategyEventRecord],
    baseline: Sequence[StrategyEventRecord],
) -> int:
    """Metrics B and C — recovered revenue difference in paise."""
    return recovered_revenue_paise(subject) - recovered_revenue_paise(baseline)


def incremental_pct(
    subject: Sequence[StrategyEventRecord],
    baseline: Sequence[StrategyEventRecord],
) -> float | None:
    """Incremental recovery as a percentage of the baseline.

    Returns None when the baseline recovered nothing, because a percentage
    increase over zero is not a meaningful number.
    """
    base = recovered_revenue_paise(baseline)
    if base <= 0:
        return None
    return 100.0 * incremental_paise(subject, baseline) / base


# ---------------------------------------------------------------------------
# F. false interventions, G. negative-EV interventions
# ---------------------------------------------------------------------------


def false_interventions(records: Sequence[StrategyEventRecord]) -> int:
    """Attempts whose true EV is below the event's own no-action true EV.

    The frozen rule (``benchmark_config.FALSE_INTERVENTION_RULE``): an attempt
    is false when the world says it destroyed value relative to the available
    alternative of doing nothing. Breaking even exactly is not a mistake, hence
    the strict comparison.
    """
    count = 0
    for record in _attempts(records):
        if record.true_ev_paise is None or record.no_action_true_ev_paise is None:
            continue
        if record.true_ev_paise < record.no_action_true_ev_paise:
            count += 1
    return count


def false_intervention_rate(
    records: Sequence[StrategyEventRecord],
) -> float | None:
    """Metric F — false interventions divided by scoreable interventions.

    The denominator excludes an attempt whose outcome realization failed: such
    a record carries no true EV, so it can be neither counted as false nor
    honestly assumed sound. It remains visible in the exception count and in
    the intervention count.
    """
    attempts = scoreable_interventions(records)
    if attempts == 0:
        return None
    return false_interventions(records) / attempts


def negative_ev_interventions(records: Sequence[StrategyEventRecord]) -> int:
    """Attempts whose true EV is strictly negative (value burned outright)."""
    return sum(
        1
        for record in _attempts(records)
        if record.true_ev_paise is not None and record.true_ev_paise < 0
    )


def negative_ev_intervention_rate(
    records: Sequence[StrategyEventRecord],
) -> float | None:
    """Metric G — negative-true-EV attempts divided by scoreable attempts.

    Same denominator as metric F, and for the same reason.
    """
    attempts = scoreable_interventions(records)
    if attempts == 0:
        return None
    return negative_ev_interventions(records) / attempts


# ---------------------------------------------------------------------------
# H. optimality, I. regret, J. value capture
# ---------------------------------------------------------------------------


def _decidable(records: Sequence[StrategyEventRecord]) -> list[StrategyEventRecord]:
    """Events with at least one policy-allowed intervention and no exception.

    The denominator for optimality: on an event where policy authorized
    nothing, every arm is forced to ``no_action`` and "did it choose well?" has
    no content.
    """
    return [
        record
        for record in _scored(records)
        if record.allowed_candidates
        and record.true_ev_paise is not None
        and record.oracle_true_ev_paise is not None
    ]


def optimal_selection_rate(
    records: Sequence[StrategyEventRecord],
) -> float | None:
    """Metric H — share of decidable events whose choice was value-optimal.

    Optimality is measured by VALUE, not by name: a choice counts as optimal
    when its true EV equals the Oracle's true EV. That is the correct handling
    of ties — two actions the world values identically are equally optimal, and
    penalizing an arm for picking the other one would measure agreement with
    the Oracle's tie-break rule rather than decision quality.
    ``oracle_choice_match_rate`` reports the stricter name-identity version.
    """
    decidable = _decidable(records)
    if not decidable:
        return None
    optimal = sum(
        1
        for record in decidable
        if record.true_ev_paise == record.oracle_true_ev_paise
    )
    return optimal / len(decidable)


def oracle_choice_match_rate(
    records: Sequence[StrategyEventRecord],
) -> float | None:
    """Share of decidable events where the arm named the Oracle's exact choice."""
    decidable = _decidable(records)
    if not decidable:
        return None
    matched = sum(
        1
        for record in decidable
        if record.selected_intervention == record.oracle_intervention
    )
    return matched / len(decidable)


def regret_values_paise(records: Sequence[StrategyEventRecord]) -> list[int]:
    """Per-event economic regret, ``oracle_true_EV - strategy_true_EV``.

    Only defined for arms that decide inside the policy boundary; the caller is
    responsible for not asking for an unbounded arm's regret.
    """
    values: list[int] = []
    for record in _scored(records):
        regret = record.regret_paise
        if regret is None:
            continue
        if regret < 0:
            raise BenchmarkIntegrityError(
                f"negative regret on {record.event_id!r} for "
                f"{record.strategy!r}: a policy-bounded strategy cannot exceed "
                f"the policy-bounded Oracle, so the harness is wrong "
                f"(oracle={record.oracle_true_ev_paise}, "
                f"strategy={record.true_ev_paise})"
            )
        values.append(regret)
    return values


def total_true_ev_paise(records: Sequence[StrategyEventRecord]) -> int:
    """The arm's total true economic value across the shared event set."""
    return sum(
        record.true_ev_paise
        for record in _scored(records)
        if record.true_ev_paise is not None
    )


def oracle_value_capture(
    records: Sequence[StrategyEventRecord],
    oracle_records: Sequence[StrategyEventRecord],
) -> float | None:
    """Metric J (gross) — arm total true EV over Oracle total true EV.

    GROSS: the numerator and denominator both include the passive no-action
    value that every arm gets for free, so this figure is generous by
    construction and is always high. ``incremental_oracle_value_capture`` is
    the honest one to quote. Returns None unless the denominator is strictly
    positive; a zero or negative denominator makes the ratio meaningless.
    """
    denominator = total_true_ev_paise(oracle_records)
    if denominator <= 0:
        return None
    return total_true_ev_paise(records) / denominator


def incremental_oracle_value_capture(
    records: Sequence[StrategyEventRecord],
    oracle_records: Sequence[StrategyEventRecord],
    no_action_records: Sequence[StrategyEventRecord],
) -> float | None:
    """Metric J (incremental) — share of the Oracle's ADDED value captured.

        (strategy_EV - no_action_EV) / (oracle_EV - no_action_EV)

    This measures what the decision engine actually contributed, stripping out
    the passive baseline both it and the Oracle inherit. Returns None when the
    Oracle adds nothing over doing nothing (a world in which there was no
    decision to get right). The value can be negative, which is a real and
    important outcome: an arm that acts destructively captures less than none
    of the available upside.
    """
    baseline = total_true_ev_paise(no_action_records)
    denominator = total_true_ev_paise(oracle_records) - baseline
    if denominator <= 0:
        return None
    return (total_true_ev_paise(records) - baseline) / denominator


# ---------------------------------------------------------------------------
# K/L/M. safety and exceptions
# ---------------------------------------------------------------------------


def fraud_intervention_rate(
    records: Sequence[StrategyEventRecord],
    events: Sequence[PaymentEvent],
) -> float | None:
    """Metric K — interventions on fraud events over fraud events.

    Measured from the records, never asserted. Returns None when the shared set
    contains no fraud events.
    """
    fraud_ids = {
        event.event_id for event in events if event.risk_flag == "fraud_suspect"
    }
    if not fraud_ids:
        return None
    attempted = sum(
        1 for record in _performed(records) if record.event_id in fraud_ids
    )
    return attempted / len(fraud_ids)


def unauthorized_attempts(records: Sequence[StrategyEventRecord]) -> int:
    """Metric L — attempts performed without an authoritative policy ALLOW.

    RecoveryOS's requirement is 0. Naive Retry has no policy gate by design, so
    a non-zero count there is the measurement of exactly that, not a defect.
    """
    return sum(1 for record in _performed(records) if not record.authorized)


def exception_counts(
    records: Sequence[StrategyEventRecord],
) -> dict[str, int]:
    """Metric M — exceptions by category; every category is always present."""
    counts = {category: 0 for category in EXCEPTION_CATEGORIES}
    for record in records:
        if record.exception_category is None:
            continue
        counts[record.exception_category] = (
            counts.get(record.exception_category, 0) + 1
        )
    return counts


def intervention_mix(records: Sequence[StrategyEventRecord]) -> dict[str, int]:
    """How many times each intervention was selected (``no_action`` included).

    Counts a record that either produced an outcome or genuinely performed an
    intervention, so an action taken before a later failure still appears in
    the mix. A failure that never reached a decision contributes nothing.
    """
    counted = [
        record
        for record in records
        if record.exception is None or record.attempted
    ]
    mix: dict[str, int] = {}
    for record in counted:
        mix[record.selected_intervention] = (
            mix.get(record.selected_intervention, 0) + 1
        )
    return dict(sorted(mix.items()))


# ---------------------------------------------------------------------------
# Aggregate
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class StrategyMetrics:
    """Every Phase 17 metric for one arm, with denominators made explicit."""

    strategy: str
    policy_bounded: bool
    event_count: int
    processed: int
    exceptions: int
    exceptions_by_category: Mapping[str, int]
    recovered_events: int
    recovered_revenue_paise: int
    interventions_attempted: int
    scoreable_interventions: int
    intervention_mix: Mapping[str, int]
    recovery_efficiency_paise: float | None
    incremental_vs_no_action_paise: int
    incremental_vs_no_action_pct: float | None
    incremental_vs_v1_paise: int
    total_true_ev_paise: int
    false_interventions: int
    false_intervention_rate: float | None
    negative_ev_interventions: int
    negative_ev_intervention_rate: float | None
    decidable_events: int
    optimal_selection_rate: float | None
    oracle_choice_match_rate: float | None
    total_regret_paise: int | None
    average_regret_paise: float | None
    median_regret_paise: float | None
    oracle_value_capture: float | None
    incremental_oracle_value_capture: float | None
    fraud_intervention_rate: float | None
    unauthorized_attempts: int

    def to_dict(self) -> dict[str, Any]:
        """Serialize every metric, preserving explicit ``None`` denominators."""
        return {
            "strategy": self.strategy,
            "policy_bounded": self.policy_bounded,
            "event_count": self.event_count,
            "processed": self.processed,
            "exceptions": self.exceptions,
            "exceptions_by_category": dict(sorted(self.exceptions_by_category.items())),
            "recovered_events": self.recovered_events,
            "recovered_revenue_paise": self.recovered_revenue_paise,
            "interventions_attempted": self.interventions_attempted,
            "scoreable_interventions": self.scoreable_interventions,
            "intervention_mix": dict(sorted(self.intervention_mix.items())),
            "recovery_efficiency_paise": self.recovery_efficiency_paise,
            "incremental_vs_no_action_paise": self.incremental_vs_no_action_paise,
            "incremental_vs_no_action_pct": self.incremental_vs_no_action_pct,
            "incremental_vs_v1_paise": self.incremental_vs_v1_paise,
            "total_true_ev_paise": self.total_true_ev_paise,
            "false_interventions": self.false_interventions,
            "false_intervention_rate": self.false_intervention_rate,
            "negative_ev_interventions": self.negative_ev_interventions,
            "negative_ev_intervention_rate": self.negative_ev_intervention_rate,
            "decidable_events": self.decidable_events,
            "optimal_selection_rate": self.optimal_selection_rate,
            "oracle_choice_match_rate": self.oracle_choice_match_rate,
            "total_regret_paise": self.total_regret_paise,
            "average_regret_paise": self.average_regret_paise,
            "median_regret_paise": self.median_regret_paise,
            "oracle_value_capture": self.oracle_value_capture,
            "incremental_oracle_value_capture": (
                self.incremental_oracle_value_capture
            ),
            "fraud_intervention_rate": self.fraud_intervention_rate,
            "unauthorized_attempts": self.unauthorized_attempts,
        }


def strategy_metrics(
    report: Phase17BenchmarkReport, strategy: str
) -> StrategyMetrics:
    """Compute every metric for one arm of a completed run."""
    from .benchmark_phase17 import STRATEGY_NO_ACTION, STRATEGY_ORACLE, STRATEGY_V1

    records = report.for_strategy(strategy)
    no_action = report.for_strategy(STRATEGY_NO_ACTION)
    oracle = report.for_strategy(STRATEGY_ORACLE)
    v1 = report.for_strategy(STRATEGY_V1)
    policy_bounded = strategy in POLICY_BOUNDED_STRATEGIES

    exceptions_by_category = exception_counts(records)
    exceptions = sum(exceptions_by_category.values())

    if policy_bounded:
        regrets = regret_values_paise(records)
        total_regret: int | None = sum(regrets)
        average_regret: float | None = (
            sum(regrets) / len(regrets) if regrets else None
        )
        median_regret: float | None = float(median(regrets)) if regrets else None
    else:
        # Naive Retry acts outside the policy boundary, so the policy-bounded
        # Oracle is not an upper bound for it. Reporting a number here would be
        # a category error; the honest answer is that it is undefined.
        total_regret = None
        average_regret = None
        median_regret = None

    return StrategyMetrics(
        strategy=strategy,
        policy_bounded=policy_bounded,
        event_count=len(records),
        processed=len(_scored(records)),
        exceptions=exceptions,
        exceptions_by_category=exceptions_by_category,
        recovered_events=recovered_events(records),
        recovered_revenue_paise=recovered_revenue_paise(records),
        interventions_attempted=interventions_attempted(records),
        scoreable_interventions=scoreable_interventions(records),
        intervention_mix=intervention_mix(records),
        recovery_efficiency_paise=recovery_efficiency_paise(records),
        incremental_vs_no_action_paise=incremental_paise(records, no_action),
        incremental_vs_no_action_pct=incremental_pct(records, no_action),
        incremental_vs_v1_paise=incremental_paise(records, v1),
        total_true_ev_paise=total_true_ev_paise(records),
        false_interventions=false_interventions(records),
        false_intervention_rate=false_intervention_rate(records),
        negative_ev_interventions=negative_ev_interventions(records),
        negative_ev_intervention_rate=negative_ev_intervention_rate(records),
        decidable_events=len(_decidable(records)),
        optimal_selection_rate=optimal_selection_rate(records),
        oracle_choice_match_rate=oracle_choice_match_rate(records),
        total_regret_paise=total_regret,
        average_regret_paise=average_regret,
        median_regret_paise=median_regret,
        oracle_value_capture=oracle_value_capture(records, oracle),
        incremental_oracle_value_capture=incremental_oracle_value_capture(
            records, oracle, no_action
        ),
        fraud_intervention_rate=fraud_intervention_rate(records, report.events),
        unauthorized_attempts=unauthorized_attempts(records),
    )


def all_strategy_metrics(
    report: Phase17BenchmarkReport,
) -> dict[str, StrategyMetrics]:
    """Compute metrics for every arm, in canonical order."""
    from .benchmark_phase17 import CANONICAL_STRATEGY_ORDER

    return {
        strategy: strategy_metrics(report, strategy)
        for strategy in CANONICAL_STRATEGY_ORDER
    }


def selection_disagreements(
    report: Phase17BenchmarkReport,
) -> tuple[tuple[str, str, str], ...]:
    """Events where V1 and V2 chose differently: ``(event_id, v1, v2)``.

    The benchmark is only informative if the two decision engines can actually
    diverge on the natural event distribution; this reports where they did.
    """
    from .benchmark_phase17 import STRATEGY_V1, STRATEGY_V2

    v1 = {record.event_id: record for record in report.for_strategy(STRATEGY_V1)}
    v2 = {record.event_id: record for record in report.for_strategy(STRATEGY_V2)}
    return tuple(
        (event_id, v1[event_id].selected_intervention, v2[event_id].selected_intervention)
        for event_id in sorted(v1)
        if event_id in v2
        and v1[event_id].selected_intervention != v2[event_id].selected_intervention
    )


def no_action_events(report: Phase17BenchmarkReport, strategy: str) -> int:
    """Events on which the arm deliberately did nothing (not an exception)."""
    return sum(
        1
        for record in _scored(report.for_strategy(strategy))
        if record.selected_intervention == NO_ACTION
    )

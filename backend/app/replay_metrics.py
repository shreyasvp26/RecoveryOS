"""Phase 19 replay metrics, scenario comparison and event-level decision deltas.

Pure functions over the per-event replay records. No metric mutates state, runs
a decision, or influences one.

MONEY
-----
Integer paise everywhere, matching the locked ``PaymentEvent`` contract. The
only floats are rates and ratios, which are presentation values and never feed
money arithmetic.

HONEST DENOMINATORS
-------------------
A metric that cannot be computed honestly returns ``None`` rather than a
plausible-looking zero. An arm that attempted nothing has no efficiency; it has
no denominator.

FAILURES ARE NEVER RECOVERED INTO SUCCESS
-----------------------------------------
Every failure stays visible and stays categorized, and a failed event
contributes no recovery. "Nothing was recovered" and "the evaluation failed"
are different facts and are reported as different numbers.

WHICH RULES ACTUALLY BOUND
--------------------------
``rule_activity`` reports, per policy rule, how many candidate interventions it
denied on this workload. This is the honest counterpart to offering three
configurable knobs: it lets the lab show from DATA which of them were load
bearing on the events replayed, instead of implying that every knob must have
moved the result.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Sequence

from .policy import (
    RULE_COOLDOWN,
    RULE_CUSTOMER_LIMIT,
    RULE_DUPLICATE,
    RULE_FRAUD,
    RULE_SPEND_CAP,
    RULE_TERMINAL,
)
from .policy_scenario import CONFIGURABLE_RULES, IMMUTABLE_PROTECTIONS
from .replay import (
    FAILURE_CATEGORIES,
    REPLAY_MODE_SIMULATED,
    ReplayEventRecord,
    ReplayResult,
)
from .selector import NO_ACTION

# Every denial reason the engine can produce, in its deterministic evaluation
# order. Sourced from policy.py so the lab cannot drift from the engine.
ALL_POLICY_RULES: tuple[str, ...] = (
    RULE_FRAUD,
    RULE_TERMINAL,
    RULE_DUPLICATE,
    RULE_CUSTOMER_LIMIT,
    RULE_COOLDOWN,
    RULE_SPEND_CAP,
)


def _scored(records: Sequence[ReplayEventRecord]) -> list[ReplayEventRecord]:
    """Records that produced a real outcome (a failure produced none)."""
    return [record for record in records if record.failure is None]


def _performed(records: Sequence[ReplayEventRecord]) -> list[ReplayEventRecord]:
    """Records on which an intervention was genuinely performed.

    Includes an attempt that ran and was then followed by a failure in outcome
    realization: the action was taken, so the intervention count must not lose
    it merely because the world subsequently failed to report the result.
    """
    return [record for record in records if record.attempted]


# ---------------------------------------------------------------------------
# Financial
# ---------------------------------------------------------------------------


def simulated_recovered_revenue_paise(
    records: Sequence[ReplayEventRecord],
) -> int:
    """Simulated recovered revenue. NOT a production revenue figure."""
    return sum(record.recovered_amount_paise for record in records)


def recoverable_revenue_paise(records: Sequence[ReplayEventRecord]) -> int:
    """Total value of the failed payments in the replayed workload."""
    return sum(record.amount_paise for record in records)


def unrecovered_revenue_paise(records: Sequence[ReplayEventRecord]) -> int:
    """Workload value that the scenario did not recover, in simulation."""
    return recoverable_revenue_paise(records) - simulated_recovered_revenue_paise(
        records
    )


def recovered_events(records: Sequence[ReplayEventRecord]) -> int:
    """Events on which money was simulated as recovered."""
    return sum(1 for record in records if record.recovered)


def recovery_rate(records: Sequence[ReplayEventRecord]) -> float | None:
    """Recovered events over events that produced an outcome at all.

    The denominator excludes failed evaluations: an event whose replay failed
    was neither recovered nor confirmed unrecovered, so counting it as a miss
    would convert a failure into a result.
    """
    scored = _scored(records)
    if not scored:
        return None
    return sum(1 for record in scored if record.recovered) / len(scored)


def revenue_recovery_rate(records: Sequence[ReplayEventRecord]) -> float | None:
    """Recovered value over recoverable value, by money rather than by count."""
    recoverable = recoverable_revenue_paise(records)
    if recoverable <= 0:
        return None
    return simulated_recovered_revenue_paise(records) / recoverable


# ---------------------------------------------------------------------------
# Intervention
# ---------------------------------------------------------------------------


def total_interventions(records: Sequence[ReplayEventRecord]) -> int:
    """Interventions actually performed, in simulation."""
    return len(_performed(records))


def interventions_by_type(
    records: Sequence[ReplayEventRecord],
) -> dict[str, int]:
    """How many times each intervention was performed, in canonical order."""
    mix: dict[str, int] = {}
    for record in _performed(records):
        mix[record.selected_intervention] = (
            mix.get(record.selected_intervention, 0) + 1
        )
    return dict(sorted(mix.items()))


def customers_touched(records: Sequence[ReplayEventRecord]) -> int:
    """Distinct customers that received at least one intervention."""
    return len({record.customer_id for record in _performed(records)})


def interventions_per_customer(
    records: Sequence[ReplayEventRecord],
) -> float | None:
    """Interventions divided by the customers that received one.

    Returns None when nothing was attempted: a scenario that intervened on
    nobody has no per-customer intensity, it has no denominator.
    """
    customers = customers_touched(records)
    if customers == 0:
        return None
    return total_interventions(records) / customers


def intervention_efficiency_paise(
    records: Sequence[ReplayEventRecord],
) -> float | None:
    """Simulated recovered paise per intervention performed."""
    attempts = total_interventions(records)
    if attempts == 0:
        return None
    return simulated_recovered_revenue_paise(records) / attempts


def no_action_events(records: Sequence[ReplayEventRecord]) -> int:
    """Events on which the pipeline deliberately did nothing (not a failure)."""
    return sum(
        1
        for record in _scored(records)
        if record.selected_intervention == NO_ACTION
    )


def intervention_spend_paise(records: Sequence[ReplayEventRecord]) -> int:
    """Total configured cost of the interventions performed, in paise."""
    return sum(record.intervention_cost_paise for record in _performed(records))


# ---------------------------------------------------------------------------
# Safety
# ---------------------------------------------------------------------------


def blocks_by_rule(records: Sequence[ReplayEventRecord]) -> dict[str, int]:
    """Denied candidate interventions per policy rule; every rule is present.

    Counts CANDIDATES denied, not events: one event can have several
    candidates denied by the same rule, and the safety question is how many
    proposed actions the gate stopped.
    """
    counts = {rule: 0 for rule in ALL_POLICY_RULES}
    for record in records:
        for reason in record.denials.values():
            counts[reason] = counts.get(reason, 0) + 1
    return counts


def total_blocked_interventions(records: Sequence[ReplayEventRecord]) -> int:
    """Every candidate intervention the policy gate denied."""
    return sum(record.blocked_count for record in records)


def fully_blocked_events(records: Sequence[ReplayEventRecord]) -> int:
    """Events where policy authorized nothing at all."""
    return sum(
        1
        for record in _scored(records)
        if record.candidates_considered and not record.allowed_candidates
    )


def unauthorized_attempts(records: Sequence[ReplayEventRecord]) -> int:
    """Interventions performed without an authoritative policy ALLOW.

    Replay's requirement is 0, and the record type refuses to represent a
    non-zero value, so this is a measurement of a structural guarantee rather
    than a hope.
    """
    return sum(1 for record in _performed(records) if not record.authorized)


def fraud_interventions(records: Sequence[ReplayEventRecord]) -> int:
    """Interventions performed on an event the classifier flagged as fraud.

    Must be 0 under every scenario. Measured from the records rather than
    asserted from the policy configuration.
    """
    return sum(
        1
        for record in _performed(records)
        if record.root_cause_category == "fraud_suspect"
    )


def terminal_interventions(records: Sequence[ReplayEventRecord]) -> int:
    """Interventions performed on a terminal event. Must be 0 always."""
    return sum(
        1
        for record in _performed(records)
        if record.root_cause_category == "terminal"
    )


def rule_activity(records: Sequence[ReplayEventRecord]) -> dict[str, Any]:
    """Per-rule denial counts, annotated with what configures each rule.

    ``load_bearing`` says whether the rule denied anything on THIS workload.
    A configurable rule that denied nothing is reported plainly as such: the
    lab offers the knob because the engine has it, and reports honestly when
    turning it could not have changed this result.
    """
    counts = blocks_by_rule(records)
    return {
        rule: {
            "blocked": counts[rule],
            "immutable": rule in IMMUTABLE_PROTECTIONS,
            "configured_by": CONFIGURABLE_RULES.get(rule),
            "load_bearing": counts[rule] > 0,
        }
        for rule in ALL_POLICY_RULES
    }


# ---------------------------------------------------------------------------
# Operational
# ---------------------------------------------------------------------------


def failures_by_category(records: Sequence[ReplayEventRecord]) -> dict[str, int]:
    """Failures per category; every category is always present."""
    counts = {category: 0 for category in FAILURE_CATEGORIES}
    for record in records:
        if record.failure_category is None:
            continue
        counts[record.failure_category] = counts.get(record.failure_category, 0) + 1
    return counts


def total_failures(records: Sequence[ReplayEventRecord]) -> int:
    """Events whose replay failed and therefore produced no result."""
    return sum(1 for record in records if record.failure is not None)


# ---------------------------------------------------------------------------
# Aggregate
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ReplayMetrics:
    """Every Phase 19 metric for one scenario, with denominators explicit."""

    scenario_id: str
    scenario_name: str
    replay_mode: str
    event_count: int
    processed: int
    failures: int
    failures_by_category: Mapping[str, int]

    simulated_recovered_revenue_paise: int
    recoverable_revenue_paise: int
    unrecovered_revenue_paise: int
    recovered_events: int
    recovery_rate: float | None
    revenue_recovery_rate: float | None

    total_interventions: int
    interventions_by_type: Mapping[str, int]
    customers_touched: int
    interventions_per_customer: float | None
    intervention_efficiency_paise: float | None
    intervention_spend_paise: int
    no_action_events: int

    total_blocked_interventions: int
    blocks_by_rule: Mapping[str, int]
    fully_blocked_events: int
    rule_activity: Mapping[str, Any]
    unauthorized_attempts: int
    fraud_interventions: int
    terminal_interventions: int

    def to_dict(self) -> dict[str, Any]:
        """Serialize every metric, preserving explicit ``None`` denominators."""
        return {
            "scenario_id": self.scenario_id,
            "scenario_name": self.scenario_name,
            "replay_mode": self.replay_mode,
            "event_count": self.event_count,
            "processed": self.processed,
            "failures": self.failures,
            "failures_by_category": dict(sorted(self.failures_by_category.items())),
            "financial": {
                "simulated_recovered_revenue_paise": (
                    self.simulated_recovered_revenue_paise
                ),
                "recoverable_revenue_paise": self.recoverable_revenue_paise,
                "unrecovered_revenue_paise": self.unrecovered_revenue_paise,
                "recovered_events": self.recovered_events,
                "recovery_rate": self.recovery_rate,
                "revenue_recovery_rate": self.revenue_recovery_rate,
            },
            "intervention": {
                "total_interventions": self.total_interventions,
                "interventions_by_type": dict(
                    sorted(self.interventions_by_type.items())
                ),
                "customers_touched": self.customers_touched,
                "interventions_per_customer": self.interventions_per_customer,
                "intervention_efficiency_paise": self.intervention_efficiency_paise,
                "intervention_spend_paise": self.intervention_spend_paise,
                "no_action_events": self.no_action_events,
            },
            "safety": {
                "total_blocked_interventions": self.total_blocked_interventions,
                "blocks_by_rule": dict(sorted(self.blocks_by_rule.items())),
                "fully_blocked_events": self.fully_blocked_events,
                "rule_activity": self.rule_activity,
                "unauthorized_attempts": self.unauthorized_attempts,
                "fraud_interventions": self.fraud_interventions,
                "terminal_interventions": self.terminal_interventions,
            },
        }


def replay_metrics(result: ReplayResult) -> ReplayMetrics:
    """Compute every metric for one completed replay."""
    records = result.records
    categories = failures_by_category(records)
    return ReplayMetrics(
        scenario_id=result.scenario.scenario_id,
        scenario_name=result.scenario.name,
        replay_mode=result.replay_mode,
        event_count=len(records),
        processed=len(_scored(records)),
        failures=sum(categories.values()),
        failures_by_category=categories,
        simulated_recovered_revenue_paise=simulated_recovered_revenue_paise(records),
        recoverable_revenue_paise=recoverable_revenue_paise(records),
        unrecovered_revenue_paise=unrecovered_revenue_paise(records),
        recovered_events=recovered_events(records),
        recovery_rate=recovery_rate(records),
        revenue_recovery_rate=revenue_recovery_rate(records),
        total_interventions=total_interventions(records),
        interventions_by_type=interventions_by_type(records),
        customers_touched=customers_touched(records),
        interventions_per_customer=interventions_per_customer(records),
        intervention_efficiency_paise=intervention_efficiency_paise(records),
        intervention_spend_paise=intervention_spend_paise(records),
        no_action_events=no_action_events(records),
        total_blocked_interventions=total_blocked_interventions(records),
        blocks_by_rule=blocks_by_rule(records),
        fully_blocked_events=fully_blocked_events(records),
        rule_activity=rule_activity(records),
        unauthorized_attempts=unauthorized_attempts(records),
        fraud_interventions=fraud_interventions(records),
        terminal_interventions=terminal_interventions(records),
    )


# ---------------------------------------------------------------------------
# Event-level decision deltas
# ---------------------------------------------------------------------------

DELTA_SELECTION_CHANGED = "selection_changed"
DELTA_NEWLY_BLOCKED = "newly_blocked"
DELTA_NEWLY_ALLOWED = "newly_allowed"
DELTA_AUTHORIZATION_CHANGED = "authorization_changed"
DELTA_FAILURE_CHANGED = "failure_changed"


@dataclass(frozen=True)
class DecisionDelta:
    """One event where two scenarios decided differently.

    Keyed by event id, so a comparison is a dictionary lookup rather than a
    positional pairing that could silently misalign two runs.
    """

    event_id: str
    customer_id: str
    amount_paise: int
    root_cause_category: str | None
    delta_type: str
    reference_selected: str
    candidate_selected: str
    reference_denial_reason: str | None
    candidate_denial_reason: str | None
    reference_allowed: tuple[str, ...]
    candidate_allowed: tuple[str, ...]
    reference_recovered_amount_paise: int
    candidate_recovered_amount_paise: int

    def to_dict(self) -> dict[str, Any]:
        """Serialize the delta for the Policy Lab."""
        return {
            "event_id": self.event_id,
            "customer_id": self.customer_id,
            "amount_paise": self.amount_paise,
            "root_cause_category": self.root_cause_category,
            "delta_type": self.delta_type,
            "reference": {
                "selected_intervention": self.reference_selected,
                "denial_reason": self.reference_denial_reason,
                "allowed_candidates": list(self.reference_allowed),
                "simulated_recovered_amount_paise": (
                    self.reference_recovered_amount_paise
                ),
            },
            "candidate": {
                "selected_intervention": self.candidate_selected,
                "denial_reason": self.candidate_denial_reason,
                "allowed_candidates": list(self.candidate_allowed),
                "simulated_recovered_amount_paise": (
                    self.candidate_recovered_amount_paise
                ),
            },
        }


def _blocking_reason(record: ReplayEventRecord) -> str | None:
    """Why nothing was done, when nothing was done and policy is the cause.

    Reports a denial reason only for an event where the gate authorized
    NOTHING. On an event with an allowed candidate, a ``no_action`` result is
    the optimizer's economic judgement, not a policy stop, and labelling it
    with an unrelated per-candidate denial would misattribute the decision.
    """
    if record.allowed_candidates or not record.denials:
        return None
    for rule in ALL_POLICY_RULES:
        if rule in record.denials.values():
            return rule
    return None


def decision_deltas(
    reference: ReplayResult, candidate: ReplayResult
) -> tuple[DecisionDelta, ...]:
    """Events where ``candidate`` decided differently from ``reference``.

    Compared by event id in canonical event order. Both replays must cover the
    same event set — otherwise the comparison is not causal and is refused
    rather than computed over the intersection.
    """
    reference_records = reference.by_event()
    candidate_records = candidate.by_event()
    if set(reference_records) != set(candidate_records):
        raise ValueError(
            "scenarios were replayed over different event sets and cannot be "
            "compared"
        )

    deltas: list[DecisionDelta] = []
    for event_id in sorted(reference_records):
        before = reference_records[event_id]
        after = candidate_records[event_id]
        delta_type = _delta_type(before, after)
        if delta_type is None:
            continue
        deltas.append(
            DecisionDelta(
                event_id=event_id,
                customer_id=before.customer_id,
                amount_paise=before.amount_paise,
                root_cause_category=before.root_cause_category,
                delta_type=delta_type,
                reference_selected=before.selected_intervention,
                candidate_selected=after.selected_intervention,
                reference_denial_reason=_blocking_reason(before),
                candidate_denial_reason=_blocking_reason(after),
                reference_allowed=before.allowed_candidates,
                candidate_allowed=after.allowed_candidates,
                reference_recovered_amount_paise=before.recovered_amount_paise,
                candidate_recovered_amount_paise=after.recovered_amount_paise,
            )
        )
    return tuple(deltas)


def _delta_type(
    before: ReplayEventRecord, after: ReplayEventRecord
) -> str | None:
    """Classify how two scenarios differed on one event, or None if identical.

    Ordered most-specific first so a single event gets one honest label.
    """
    if (before.failure is None) != (after.failure is None):
        return DELTA_FAILURE_CHANGED
    if before.attempted and not after.attempted:
        return DELTA_NEWLY_BLOCKED
    if after.attempted and not before.attempted:
        return DELTA_NEWLY_ALLOWED
    if before.selected_intervention != after.selected_intervention:
        return DELTA_SELECTION_CHANGED
    if before.allowed_candidates != after.allowed_candidates:
        return DELTA_AUTHORIZATION_CHANGED
    return None


# ---------------------------------------------------------------------------
# Comparison
# ---------------------------------------------------------------------------


def _incremental(
    subject: ReplayMetrics, reference: ReplayMetrics
) -> dict[str, Any]:
    """Differences against the reference scenario, in integer paise."""
    incremental_paise = (
        subject.simulated_recovered_revenue_paise
        - reference.simulated_recovered_revenue_paise
    )
    base = reference.simulated_recovered_revenue_paise
    return {
        "incremental_recovered_revenue_paise": incremental_paise,
        # A percentage increase over a zero baseline is not a meaningful
        # number, so it is None rather than a division that happens to work.
        "incremental_recovered_revenue_pct": (
            None if base <= 0 else 100.0 * incremental_paise / base
        ),
        "incremental_interventions": (
            subject.total_interventions - reference.total_interventions
        ),
        "incremental_blocked_interventions": (
            subject.total_blocked_interventions
            - reference.total_blocked_interventions
        ),
    }


def compare_replays(
    results: Sequence[ReplayResult], reference_scenario_id: str
) -> dict[str, Any]:
    """Compare scenarios against a reference and report what policy changed.

    Verifies, rather than asserts, that the comparison is causal: every
    scenario must have replayed the same event set under the same world
    identity, and their policy fingerprints must actually differ where their
    scenario ids do. A comparison that cannot be shown fair is refused.
    """
    results = tuple(results)
    if not results:
        raise ValueError("at least one replay result is required")

    by_id = {result.scenario.scenario_id: result for result in results}
    if len(by_id) != len(results):
        raise ValueError("scenario ids must be unique within one comparison")
    reference = by_id.get(reference_scenario_id)
    if reference is None:
        raise ValueError(
            f"reference scenario {reference_scenario_id!r} is not among the "
            f"replayed scenarios {sorted(by_id)}"
        )

    fairness = verify_comparison_fairness(results)
    if not all(fairness.values()):
        failed = sorted(check for check, ok in fairness.items() if not ok)
        raise ValueError(
            f"scenarios cannot be compared causally; failed checks: {failed}"
        )

    reference_metrics = replay_metrics(reference)
    scenarios: list[dict[str, Any]] = []
    for result in results:
        metrics = replay_metrics(result)
        scenarios.append(
            {
                "scenario": result.scenario.to_dict(),
                "identity": result.identity(),
                "metrics": metrics.to_dict(),
                "is_reference": result.scenario.scenario_id
                == reference_scenario_id,
                "vs_reference": _incremental(metrics, reference_metrics),
                "decision_deltas": [
                    delta.to_dict()
                    for delta in decision_deltas(reference, result)
                ],
            }
        )

    return {
        "replay_mode": REPLAY_MODE_SIMULATED,
        "result_type": "simulated_policy_replay",
        "disclaimer": (
            "Policy replay results are controlled simulated evaluations and "
            "are not production revenue forecasts. No Razorpay execution and "
            "no customer-facing action occurs during replay."
        ),
        "reference_scenario_id": reference_scenario_id,
        "event_count": reference.event_count,
        "fairness": fairness,
        "scenarios": scenarios,
    }


def verify_comparison_fairness(
    results: Sequence[ReplayResult],
) -> dict[str, bool]:
    """Check that the only thing differing between scenarios is policy.

    These are computed checks whose failure is visible in the response, not
    prose claims. Each compares every scenario against the first.
    """
    results = tuple(results)
    first = results[0]
    first_events = tuple(record.event_id for record in first.records)
    first_identity = first.identity()

    def world_identity(result: ReplayResult) -> tuple[Any, ...]:
        identity = result.identity()
        return tuple(
            identity[key]
            for key in (
                "benchmark_methodology",
                "event_count",
                "event_seed",
                "outcome_seed",
                "replication",
                "randomization_version",
                "classification_source",
                "replay_methodology",
            )
        )

    return {
        "same_event_set": all(
            tuple(record.event_id for record in result.records) == first_events
            for result in results
        ),
        "same_world_identity": all(
            world_identity(result) == world_identity(first) for result in results
        ),
        "same_classification_source": all(
            result.identity()["classification_source"]
            == first_identity["classification_source"]
            for result in results
        ),
        "all_simulated": all(
            result.replay_mode == REPLAY_MODE_SIMULATED
            and all(
                record.replay_mode == REPLAY_MODE_SIMULATED
                for record in result.records
            )
            for result in results
        ),
        "no_unauthorized_attempt": all(
            unauthorized_attempts(result.records) == 0 for result in results
        ),
        "immutable_protections_held": all(
            fraud_interventions(result.records) == 0
            and terminal_interventions(result.records) == 0
            for result in results
        ),
    }

"""Phase 17 signal-bearing benchmark — five arms over one frozen world.

THE QUESTION THIS ANSWERS
-------------------------
Given the same events, the same policy configuration, the same hidden world,
the same execution assumptions and deterministic evaluation, does the V2
economic optimizer make better decisions than V1 fixed priority and than the
naive baselines? The harness is built so that the answer can honestly be "no".

    FROZEN EVENTS -> HIDDEN WORLD (hidden_world.py)
                          |
                   hidden from the SUT
                          |
      +--------+----------+----------+---------+
      |        |          |          |         |
   No Action  Naive   RecoveryOS  RecoveryOS  Oracle
              Retry       V1          V2    (evaluation
                           \\         /        only)
                            policy gate
                                 |
                        simulated execution
                                 |
                        outcome realization
                                 |
                          evaluation layer

WHAT MAKES THE COMPARISON FAIR
------------------------------
Everything an arm cannot control is computed ONCE per event, before any arm
runs, and handed to every arm identically: the event, its classification, the
authoritative policy decisions, the allowed candidate set, and the Oracle's
answer. An arm therefore cannot see a different world, a different policy, or a
different cost model than its rivals, and the only thing that varies between
arms is the choice they make.

Combined with the hidden world's strategy-independence and the common
randomness contract, this makes the run invariant to the order the arms run in
and to the order the events appear in — both asserted as hard tests.

CROSS-EVENT POLICY STATE (an explicit, documented decision)
-----------------------------------------------------------
The Phase 9 harness ran RecoveryOS through the database-backed
``execute_event``, so each intervention it performed became history for the
next event and the per-customer 24h limit, cooldown and duplicate rules fired
according to the order events happened to be processed in. That makes results
depend on event order, which Phase 17 must not do.

Phase 17 therefore evaluates each event as an INDEPENDENT decision problem: the
policy engine is the real one and remains authoritative, but it is given an
empty ``PolicyHistory`` per event. The consequence is honest and must be read
as a limitation: within a Phase 17 run the fraud and terminal rules are load
bearing, while the duplicate, per-customer-limit, cooldown and spend-cap rules
never fire. See docs/BENCHMARK.md. The Phase 9 benchmark keeps its sequential
behaviour, unchanged, for historical comparability.

EVERY NUMBER HERE IS SIMULATED
------------------------------
No batch execution touches Razorpay. Recovery is realized by the synthetic
hidden world. These figures are controlled evaluation results and are never a
claim about production Razorpay recovery.
"""

from __future__ import annotations

import argparse
import json
from dataclasses import dataclass, field
from typing import Any, Mapping, Sequence

from .benchmark import DeterministicClassifier
from .benchmark_config import (
    METHODOLOGY_PHASE17,
    Phase17BenchmarkConfig,
)
from .benchmark_simulation import (
    SimulatedExecution,
    SimulatedExecutionError,
    SimulatedExecutor,
)
from .classification import ClassificationResult
from .classifier import ClassifierAdapter, classify_event
from .economics import EXECUTABLE_INTERVENTIONS
from .execution_service import (
    SELECTION_V1_FIXED_PRIORITY,
    SELECTION_V2_ECONOMIC,
    select_for_strategy,
)
from .generator import generate_events
from .hidden_world import HiddenWorld
from .models import PaymentEvent
from .policy import (
    PolicyDecision,
    PolicyEngine,
    PolicyHistory,
    PolicyInput,
)
from .selector import INTERVENTION_PRIORITY, NO_ACTION

# ---------------------------------------------------------------------------
# Arms, in canonical report order
# ---------------------------------------------------------------------------

STRATEGY_NO_ACTION = "no_action"
STRATEGY_NAIVE_RETRY = "naive_retry"
STRATEGY_V1 = "recoveryos_v1"
STRATEGY_V2 = "recoveryos_v2"
STRATEGY_ORACLE = "oracle"

CANONICAL_STRATEGY_ORDER: tuple[str, ...] = (
    STRATEGY_NO_ACTION,
    STRATEGY_NAIVE_RETRY,
    STRATEGY_V1,
    STRATEGY_V2,
    STRATEGY_ORACLE,
)

STRATEGY_LABELS: Mapping[str, str] = {
    STRATEGY_NO_ACTION: "No Action",
    STRATEGY_NAIVE_RETRY: "Naive Retry",
    STRATEGY_V1: "RecoveryOS V1",
    STRATEGY_V2: "RecoveryOS V2",
    STRATEGY_ORACLE: "Oracle",
}

# Arms that make their decision strictly inside the policy authorization
# boundary. Naive Retry is deliberately excluded: it has no policy gate, so the
# policy-bounded Oracle is NOT an upper bound for it and regret against that
# Oracle would be a meaningless (and possibly negative) number.
POLICY_BOUNDED_STRATEGIES: frozenset[str] = frozenset(
    {STRATEGY_NO_ACTION, STRATEGY_V1, STRATEGY_V2, STRATEGY_ORACLE}
)

# The arms that are RecoveryOS itself, and are therefore held to the safety
# requirements (zero fraud interventions, zero unauthorized executions).
RECOVERYOS_STRATEGIES: frozenset[str] = frozenset({STRATEGY_V1, STRATEGY_V2})

NAIVE_RETRY_INTERVENTION = "retry_immediate"

SOURCE_CONTROL = "control"
SOURCE_NAIVE_FIXED = "naive_fixed_retry"
SOURCE_V1_PRIORITY = "v1_fixed_priority"
SOURCE_V2_ECONOMIC = "v2_economic"
SOURCE_ORACLE = "oracle_true_ev"

# Exception categories. Every failure stays visible and stays classified; a
# failure is NEVER folded into an ordinary "did not recover" outcome.
EXCEPTION_CLASSIFICATION = "classification_failure"
EXCEPTION_POLICY = "policy_failure"
EXCEPTION_SELECTION = "selection_failure"
EXCEPTION_SIMULATION = "simulation_failure"
EXCEPTION_MALFORMED_RESULT = "malformed_strategy_result"
EXCEPTION_CONFIGURATION = "benchmark_configuration_failure"

EXCEPTION_CATEGORIES: tuple[str, ...] = (
    EXCEPTION_CLASSIFICATION,
    EXCEPTION_POLICY,
    EXCEPTION_SELECTION,
    EXCEPTION_SIMULATION,
    EXCEPTION_MALFORMED_RESULT,
    EXCEPTION_CONFIGURATION,
)

_PRIORITY_INDEX: Mapping[str, int] = {
    intervention: index
    for index, intervention in enumerate(INTERVENTION_PRIORITY)
}
_UNRANKED = len(INTERVENTION_PRIORITY)


class Phase17BenchmarkError(Exception):
    """The Phase 17 benchmark cannot proceed honestly."""


class BenchmarkIntegrityError(Phase17BenchmarkError):
    """An invariant the benchmark's honesty depends on was violated.

    Raised rather than repaired. A strategy that appears to beat the Oracle, or
    a regret that comes out negative, means the harness is wrong; clamping the
    number would hide a methodology bug behind a plausible result.
    """


# ---------------------------------------------------------------------------
# Per-event shared context: identical for every arm, computed once
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class OracleEvaluation:
    """The best achievable true expected value inside the policy boundary.

    EVALUATION ONLY. The Oracle reads hidden ground truth, which no RecoveryOS
    component may do, so it is an upper bound to measure against and never a
    strategy that could ship.

    Option set: every policy-ALLOWED executable intervention, plus
    ``no_action`` — which is always available, is never denied because it is
    never proposed, and is the honest floor for "the best thing to do here may
    be nothing".

    Tie-break (total and deterministic): highest true EV, then ``no_action``
    ahead of any action at an exact tie (spending money to achieve the same
    modelled value is not better), then V1 priority order, then name.
    """

    event_id: str
    option_true_ev_paise: Mapping[str, int]
    selected_intervention: str
    true_ev_paise: int
    no_action_true_ev_paise: int

    def to_dict(self) -> dict[str, Any]:
        """Serialize the Oracle's answer for benchmark artifacts."""
        return {
            "event_id": self.event_id,
            "option_true_ev_paise": dict(sorted(self.option_true_ev_paise.items())),
            "selected_intervention": self.selected_intervention,
            "true_ev_paise": self.true_ev_paise,
            "no_action_true_ev_paise": self.no_action_true_ev_paise,
        }


def _oracle_rank_key(item: tuple[str, int]) -> tuple[int, int, int, str]:
    intervention, true_ev = item
    return (
        -true_ev,
        0 if intervention == NO_ACTION else 1,
        _PRIORITY_INDEX.get(intervention, _UNRANKED),
        intervention,
    )


def evaluate_oracle(
    event: PaymentEvent,
    allowed: Sequence[str],
    world: HiddenWorld,
) -> OracleEvaluation:
    """Compute the policy-bounded best action under hidden ground truth."""
    options: dict[str, int] = {NO_ACTION: world.true_ev_paise(event, NO_ACTION)}
    for intervention in allowed:
        if intervention == NO_ACTION:
            continue
        if intervention not in EXECUTABLE_INTERVENTIONS:
            raise BenchmarkIntegrityError(
                f"policy allowed a non-executable intervention {intervention!r}"
            )
        options[intervention] = world.true_ev_paise(event, intervention)
    best = min(options.items(), key=_oracle_rank_key)
    return OracleEvaluation(
        event_id=event.event_id,
        option_true_ev_paise=options,
        selected_intervention=best[0],
        true_ev_paise=best[1],
        no_action_true_ev_paise=options[NO_ACTION],
    )


@dataclass(frozen=True)
class EventWorldContext:
    """Everything every arm shares for one event, computed exactly once.

    Building this before any arm runs is what makes fairness structural rather
    than a convention: two arms cannot disagree about the classification, the
    policy decisions, the allowed set, or the Oracle, because there is only one
    of each and none of them is recomputed per arm.

    ``exception`` is set when the shared work itself failed (a classification
    or policy failure). Every arm then records that same exception, so a broken
    event penalizes no arm more than another.
    """

    event: PaymentEvent
    classification: ClassificationResult | None
    decisions: Mapping[str, PolicyDecision]
    allowed: tuple[str, ...]
    oracle: OracleEvaluation | None
    exception: str | None = None
    exception_category: str | None = None

    @property
    def has_allowed_candidate(self) -> bool:
        """True when policy authorized at least one executable intervention."""
        return bool(self.allowed)


def build_event_context(
    event: PaymentEvent,
    classifier: ClassifierAdapter,
    config: Phase17BenchmarkConfig,
    world: HiddenWorld,
) -> EventWorldContext:
    """Classify, authorize and oracle-evaluate one event, once, for all arms."""
    try:
        classification = classify_event(event, classifier)
    except Exception as exc:
        return EventWorldContext(
            event=event,
            classification=None,
            decisions={},
            allowed=(),
            oracle=None,
            exception=str(exc),
            exception_category=EXCEPTION_CLASSIFICATION,
        )

    # Each event is an independent decision problem; see the module docstring.
    history = PolicyHistory(
        customer_intervention_count_24h=0,
        most_recent_event_intervention_time=None,
        has_successful_intervention=False,
        existing_daily_spend_paise=0,
    )
    decisions: dict[str, PolicyDecision] = {}
    try:
        for candidate in classification.candidate_interventions:
            if candidate == NO_ACTION:
                continue
            decisions[candidate] = PolicyEngine().evaluate(
                PolicyInput(
                    event=event,
                    classification=classification,
                    proposed_intervention=candidate,
                    history=history,
                    evaluation_time=config.evaluation_time,
                ),
                config.policy_config,
            )
    except Exception as exc:
        return EventWorldContext(
            event=event,
            classification=classification,
            decisions={},
            allowed=(),
            oracle=None,
            exception=str(exc),
            exception_category=EXCEPTION_POLICY,
        )

    allowed = tuple(
        candidate
        for candidate in sorted(decisions)
        if decisions[candidate].allowed
    )
    return EventWorldContext(
        event=event,
        classification=classification,
        decisions=decisions,
        allowed=allowed,
        oracle=evaluate_oracle(event, allowed, world),
    )


# ---------------------------------------------------------------------------
# The benchmark-only per-event record
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class StrategyEventRecord:
    """One arm's full trace for one event. BENCHMARK-ONLY.

    Carries hidden ground truth (``true_probability_bps``, ``true_ev_paise``)
    because the evaluation layer needs it to compute regret. Nothing of this
    type is ever returned by a production API, persisted into the operational
    tables, or handed to a decision module; the production decision path stays
    blind by construction.
    """

    event_id: str
    strategy: str
    root_cause_category: str | None
    candidates_considered: tuple[str, ...]
    allowed_candidates: tuple[str, ...]
    selected_intervention: str
    selection_source: str
    attempted: bool
    authorized: bool
    execution: SimulatedExecution | None
    recovered: bool
    recovered_amount_paise: int
    true_probability_bps: int | None
    true_ev_paise: int | None
    oracle_intervention: str | None
    oracle_true_ev_paise: int | None
    no_action_true_ev_paise: int | None
    exception: str | None = None
    exception_category: str | None = None

    def __post_init__(self) -> None:
        if self.strategy not in CANONICAL_STRATEGY_ORDER:
            raise Phase17BenchmarkError(
                f"strategy must be one of {CANONICAL_STRATEGY_ORDER}, "
                f"got {self.strategy!r}"
            )
        if (self.exception is None) != (self.exception_category is None):
            raise Phase17BenchmarkError(
                "an exception must always carry a category, and a category "
                "must always carry an exception"
            )
        if self.exception is not None:
            if self.recovered or self.recovered_amount_paise:
                raise Phase17BenchmarkError(
                    "a failed event never reports recovery; an exception is "
                    "not an ordinary non-recovery outcome"
                )
        if self.attempted and self.selected_intervention == NO_ACTION:
            raise Phase17BenchmarkError(
                f"{NO_ACTION!r} is never an attempted intervention"
            )
        if self.recovered and self.recovered_amount_paise <= 0:
            raise Phase17BenchmarkError(
                "a recovered event must carry a positive recovered amount"
            )
        if not self.recovered and self.recovered_amount_paise != 0:
            raise Phase17BenchmarkError(
                "a non-recovered event must carry a zero recovered amount"
            )

    @property
    def execution_mode(self) -> str | None:
        """The execution mode of the attempt, always SIMULATED when present."""
        return None if self.execution is None else self.execution.execution_mode

    @property
    def regret_paise(self) -> int | None:
        """Oracle true EV minus this arm's true EV, or None when undefined."""
        if self.oracle_true_ev_paise is None or self.true_ev_paise is None:
            return None
        return self.oracle_true_ev_paise - self.true_ev_paise

    def to_dict(self) -> dict[str, Any]:
        """Serialize the full trace for benchmark artifacts and tests."""
        return {
            "event_id": self.event_id,
            "strategy": self.strategy,
            "root_cause_category": self.root_cause_category,
            "candidates_considered": list(self.candidates_considered),
            "allowed_candidates": list(self.allowed_candidates),
            "selected_intervention": self.selected_intervention,
            "selection_source": self.selection_source,
            "attempted": self.attempted,
            "authorized": self.authorized,
            "execution": None if self.execution is None else self.execution.to_dict(),
            "recovered": self.recovered,
            "recovered_amount_paise": self.recovered_amount_paise,
            "true_probability_bps": self.true_probability_bps,
            "true_ev_paise": self.true_ev_paise,
            "oracle_intervention": self.oracle_intervention,
            "oracle_true_ev_paise": self.oracle_true_ev_paise,
            "no_action_true_ev_paise": self.no_action_true_ev_paise,
            "regret_paise": self.regret_paise,
            "exception": self.exception,
            "exception_category": self.exception_category,
        }


def _exception_record(
    context: EventWorldContext,
    strategy: str,
    detail: str,
    category: str,
    *,
    selected: str = NO_ACTION,
    source: str = SOURCE_CONTROL,
    attempted: bool = False,
    authorized: bool = False,
    execution: SimulatedExecution | None = None,
) -> StrategyEventRecord:
    """Build a visible, categorized failure record that claims nothing.

    A failure claims no recovery, but it must not erase what already happened.
    The caller passes whatever state the pipeline had genuinely reached, so a
    failure during outcome realization still records the intervention that was
    selected and the simulated execution that really ran. Defaults describe a
    failure before any decision was reached.
    """
    return StrategyEventRecord(
        event_id=context.event.event_id,
        strategy=strategy,
        root_cause_category=(
            None
            if context.classification is None
            else context.classification.root_cause_category
        ),
        candidates_considered=(
            ()
            if context.classification is None
            else tuple(context.classification.candidate_interventions)
        ),
        allowed_candidates=context.allowed,
        selected_intervention=selected,
        selection_source=source,
        attempted=attempted,
        authorized=authorized,
        execution=execution,
        recovered=False,
        recovered_amount_paise=0,
        true_probability_bps=None,
        true_ev_paise=None,
        oracle_intervention=(
            None if context.oracle is None else context.oracle.selected_intervention
        ),
        oracle_true_ev_paise=(
            None if context.oracle is None else context.oracle.true_ev_paise
        ),
        no_action_true_ev_paise=(
            None if context.oracle is None else context.oracle.no_action_true_ev_paise
        ),
        exception=detail,
        exception_category=category,
    )


def _finalize(
    context: EventWorldContext,
    strategy: str,
    selected: str,
    source: str,
    world: HiddenWorld,
    executor: SimulatedExecutor,
    *,
    require_authorization: bool = True,
) -> StrategyEventRecord:
    """Execute (in simulation) and realize the outcome for one arm decision.

    The order is load bearing and mirrors reality: the action is decided, then
    performed, and only then does the world decide whether money came back.
    Execution success never implies recovery.

    State accumulates as the pipeline advances, and a failure at any stage
    reports the state actually reached rather than resetting to the beginning.
    An execution that genuinely ran stays on the record even if the world then
    fails to tell us the outcome, because the benchmark's accounting must not
    lose an intervention it really performed.
    """
    assert context.classification is not None and context.oracle is not None
    event = context.event
    execution: SimulatedExecution | None = None
    attempted = selected != NO_ACTION
    authorized = False

    def failure(detail: str, category: str) -> StrategyEventRecord:
        return _exception_record(
            context,
            strategy,
            detail,
            category,
            selected=selected,
            source=source,
            attempted=attempted and execution is not None,
            authorized=authorized,
            execution=execution,
        )

    if attempted:
        try:
            execution = executor.execute(
                event,
                selected,
                context.decisions.get(selected),
                require_authorization=require_authorization,
            )
        except SimulatedExecutionError as exc:
            return failure(str(exc), EXCEPTION_SIMULATION)
        authorized = execution.authorized

    try:
        outcome = world.realize(event, selected)
        true_ev_paise = world.true_ev_paise(event, selected)
    except Exception as exc:
        return failure(str(exc), EXCEPTION_SIMULATION)

    return StrategyEventRecord(
        event_id=event.event_id,
        strategy=strategy,
        root_cause_category=context.classification.root_cause_category,
        candidates_considered=tuple(context.classification.candidate_interventions),
        allowed_candidates=context.allowed,
        selected_intervention=selected,
        selection_source=source,
        attempted=attempted,
        authorized=authorized,
        execution=execution,
        recovered=outcome.recovered,
        recovered_amount_paise=outcome.recovered_amount_paise,
        true_probability_bps=outcome.true_probability_bps,
        true_ev_paise=true_ev_paise,
        oracle_intervention=context.oracle.selected_intervention,
        oracle_true_ev_paise=context.oracle.true_ev_paise,
        no_action_true_ev_paise=context.oracle.no_action_true_ev_paise,
    )


# ---------------------------------------------------------------------------
# The five arms
# ---------------------------------------------------------------------------


def arm_no_action(
    context: EventWorldContext, world: HiddenWorld, executor: SimulatedExecutor
) -> StrategyEventRecord:
    """ARM A — the control. Nothing is ever attempted on any event."""
    return _finalize(
        context, STRATEGY_NO_ACTION, NO_ACTION, SOURCE_CONTROL, world, executor
    )


def arm_naive_retry(
    context: EventWorldContext, world: HiddenWorld, executor: SimulatedExecutor
) -> StrategyEventRecord:
    """ARM B — ``retry_immediate`` on every non-fraud event.

    No AI, no policy gate, no economics, no ground truth. Eligibility is the
    Phase 9 rule, unchanged: skip ``fraud_suspect``, act on everything else —
    including the events policy would have denied as terminal. It executes with
    authorization explicitly not required, and its attempts are recorded as
    unauthorized, because giving this arm the policy gate would quietly turn
    the naive baseline into a second RecoveryOS.
    """
    if context.event.risk_flag == "fraud_suspect":
        return _finalize(
            context, STRATEGY_NAIVE_RETRY, NO_ACTION, SOURCE_CONTROL, world, executor
        )
    return _finalize(
        context,
        STRATEGY_NAIVE_RETRY,
        NAIVE_RETRY_INTERVENTION,
        SOURCE_NAIVE_FIXED,
        world,
        executor,
        require_authorization=False,
    )


def _recoveryos_arm(
    context: EventWorldContext,
    strategy: str,
    selection_strategy: str,
    source: str,
    world: HiddenWorld,
    executor: SimulatedExecutor,
) -> StrategyEventRecord:
    """Run a real RecoveryOS decision path over the shared policy decisions."""
    assert context.classification is not None
    try:
        selected, _ = select_for_strategy(
            context.event,
            context.classification,
            context.decisions,
            selection_strategy,
        )
    except Exception as exc:
        return _exception_record(context, strategy, str(exc), EXCEPTION_SELECTION)
    return _finalize(context, strategy, selected, source, world, executor)


def arm_recoveryos_v1(
    context: EventWorldContext, world: HiddenWorld, executor: SimulatedExecutor
) -> StrategyEventRecord:
    """ARM C — the frozen V1 fixed-priority selector, unmodified."""
    return _recoveryos_arm(
        context,
        STRATEGY_V1,
        SELECTION_V1_FIXED_PRIORITY,
        SOURCE_V1_PRIORITY,
        world,
        executor,
    )


def arm_recoveryos_v2(
    context: EventWorldContext, world: HiddenWorld, executor: SimulatedExecutor
) -> StrategyEventRecord:
    """ARM D — the Phase 16 economic optimizer and estimator, unmodified.

    No hidden probability is injected: V2 ranks candidates using its own
    estimator, which may be wrong, and the benchmark exists to find out.
    """
    return _recoveryos_arm(
        context,
        STRATEGY_V2,
        SELECTION_V2_ECONOMIC,
        SOURCE_V2_ECONOMIC,
        world,
        executor,
    )


def arm_oracle(
    context: EventWorldContext, world: HiddenWorld, executor: SimulatedExecutor
) -> StrategyEventRecord:
    """ARM E — the evaluation-only upper bound.

    Sees hidden truth, respects the identical policy boundary, and picks the
    highest true-EV allowed option. Not a RecoveryOS strategy and not shippable.
    """
    assert context.oracle is not None
    return _finalize(
        context,
        STRATEGY_ORACLE,
        context.oracle.selected_intervention,
        SOURCE_ORACLE,
        world,
        executor,
    )


ARMS: Mapping[str, Any] = {
    STRATEGY_NO_ACTION: arm_no_action,
    STRATEGY_NAIVE_RETRY: arm_naive_retry,
    STRATEGY_V1: arm_recoveryos_v1,
    STRATEGY_V2: arm_recoveryos_v2,
    STRATEGY_ORACLE: arm_oracle,
}


# ---------------------------------------------------------------------------
# Run
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Phase17BenchmarkReport:
    """The complete output of one Phase 17 run.

    ``records`` is the raw per-event evidence every metric is derived from, so
    a reader can re-derive any published figure by hand rather than trusting an
    aggregate.
    """

    run_id: str
    config: Phase17BenchmarkConfig
    events: tuple[PaymentEvent, ...]
    contexts: tuple[EventWorldContext, ...]
    records: Mapping[str, tuple[StrategyEventRecord, ...]]
    executed_order: tuple[str, ...] = field(default=CANONICAL_STRATEGY_ORDER)

    def __post_init__(self) -> None:
        if set(self.records) != set(CANONICAL_STRATEGY_ORDER):
            raise Phase17BenchmarkError(
                f"records must cover exactly {CANONICAL_STRATEGY_ORDER}"
            )
        for strategy, records in self.records.items():
            if len(records) != len(self.events):
                raise Phase17BenchmarkError(
                    "every arm must evaluate the identical shared event set"
                )
            if any(record.strategy != strategy for record in records):
                raise Phase17BenchmarkError(
                    f"records[{strategy!r}] contains foreign records"
                )

    def for_strategy(self, strategy: str) -> tuple[StrategyEventRecord, ...]:
        """Return one arm's per-event records in canonical event order."""
        records = self.records.get(strategy)
        if records is None:
            raise Phase17BenchmarkError(f"no records for strategy {strategy!r}")
        return records


def _validate_order(order: Sequence[str]) -> tuple[str, ...]:
    unknown = set(order) - set(CANONICAL_STRATEGY_ORDER)
    if unknown:
        raise Phase17BenchmarkError(f"unknown strategies in order: {sorted(unknown)}")
    if len(order) != len(set(order)):
        raise Phase17BenchmarkError("order must not name a strategy more than once")
    if set(order) != set(CANONICAL_STRATEGY_ORDER):
        raise Phase17BenchmarkError(
            "every arm must run: a partial run cannot be compared fairly"
        )
    return tuple(order)


def run_phase17_benchmark(
    config: Phase17BenchmarkConfig | None = None,
    *,
    classifier: ClassifierAdapter | None = None,
    order: Sequence[str] = CANONICAL_STRATEGY_ORDER,
    events: Sequence[PaymentEvent] | None = None,
) -> Phase17BenchmarkReport:
    """Run all five arms over one frozen world and return the full evidence.

    ``order`` exists so that strategy-order invariance can be TESTED, not so
    that it can be tuned: the returned records are always keyed by strategy and
    always in canonical event order, whatever order the arms executed in.
    """
    if config is None:
        config = Phase17BenchmarkConfig()
    if not isinstance(config, Phase17BenchmarkConfig):
        raise Phase17BenchmarkError("config must be a Phase17BenchmarkConfig")
    if config.methodology != METHODOLOGY_PHASE17:
        raise Phase17BenchmarkError(
            f"this harness only produces {METHODOLOGY_PHASE17!r} results, "
            f"got {config.methodology!r}"
        )
    executed_order = _validate_order(order)

    if classifier is None:
        classifier = DeterministicClassifier()
    if events is None:
        events = generate_events(seed=config.event_seed, count=config.event_count)
    events = tuple(events)
    if len(events) != config.event_count:
        raise Phase17BenchmarkError(
            "the supplied event set does not match the frozen event_count"
        )

    world = HiddenWorld(
        outcome_seed=config.outcome_seed,
        model=config.economic_model,
        replication=config.replication,
    )
    executor = SimulatedExecutor()

    # One shared context per event, built before any arm runs.
    contexts = tuple(
        build_event_context(event, classifier, config, world) for event in events
    )

    records: dict[str, tuple[StrategyEventRecord, ...]] = {}
    for strategy in executed_order:
        arm = ARMS[strategy]
        per_event: list[StrategyEventRecord] = []
        for context in contexts:
            if context.exception is not None:
                per_event.append(
                    _exception_record(
                        context,
                        strategy,
                        context.exception,
                        context.exception_category or EXCEPTION_CONFIGURATION,
                    )
                )
                continue
            try:
                record = arm(context, world, executor)
            except Exception as exc:
                record = _exception_record(
                    context, strategy, str(exc), EXCEPTION_MALFORMED_RESULT
                )
            if not isinstance(record, StrategyEventRecord):
                record = _exception_record(
                    context,
                    strategy,
                    f"arm returned {type(record).__name__}",
                    EXCEPTION_MALFORMED_RESULT,
                )
            per_event.append(record)
        records[strategy] = tuple(per_event)

    # Canonical key order, so the report serializes identically whatever order
    # the arms happened to run in.
    ordered = {strategy: records[strategy] for strategy in CANONICAL_STRATEGY_ORDER}
    return Phase17BenchmarkReport(
        run_id=config.run_id(),
        config=config,
        events=events,
        contexts=contexts,
        records=ordered,
        executed_order=executed_order,
    )


def _main(argv: Sequence[str] | None = None) -> None:
    from .benchmark_phase17_report import format_report, summarize_report

    parser = argparse.ArgumentParser(
        description=(
            "Run the Phase 17 signal-bearing RecoveryOS benchmark: No Action, "
            "Naive Retry, RecoveryOS V1, RecoveryOS V2 and the evaluation-only "
            "Oracle over one frozen synthetic world. All results are SIMULATED."
        )
    )
    parser.add_argument("--seed", type=int, default=None, help="event seed")
    parser.add_argument("--count", type=int, default=None, help="event count")
    parser.add_argument(
        "--outcome-seed", type=int, default=None, help="outcome realization seed"
    )
    parser.add_argument(
        "--json", action="store_true", help="print the machine-readable summary only"
    )
    args = parser.parse_args(argv)

    defaults = Phase17BenchmarkConfig()
    config = Phase17BenchmarkConfig(
        event_seed=defaults.event_seed if args.seed is None else args.seed,
        event_count=defaults.event_count if args.count is None else args.count,
        outcome_seed=(
            (defaults.outcome_seed if args.seed is None else args.seed)
            if args.outcome_seed is None
            else args.outcome_seed
        ),
    )
    summary = summarize_report(run_phase17_benchmark(config))
    if args.json:
        print(json.dumps(summary, indent=2, sort_keys=True))
        return
    print(format_report(summary))
    print()
    print(json.dumps(summary, indent=2, sort_keys=True))


if __name__ == "__main__":
    _main()

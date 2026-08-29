"""Phase 19 deterministic policy replay — the What-If Decision Lab engine.

THE QUESTION THIS ANSWERS
-------------------------
What would RecoveryOS have done, on the exact same workload, if the control
policy had been different?

    same events
      + same classifications
      + same candidate recommendations
      + same policy ENGINE
      + same Phase 18 economic optimizer
      + same hidden world and seed
      + DIFFERENT policy CONFIGURATION
    = a causal reading of what the policy changed

THIS IS NOT A SIMULATOR
-----------------------
Nothing here re-implements a decision. Classification comes from the same
``classify_event`` the benchmark uses, authorization from the same
``PolicyEngine``, selection from the same ``select_for_strategy`` the
production execution service calls, execution from the benchmark's
``SimulatedExecutor``, and outcomes from the same ``HiddenWorld``. This module
is wiring and accounting; every judgement is made by an existing component.

WHY REPLAY KEEPS ITS OWN POLICY HISTORY
---------------------------------------
Phase 17 evaluates each event as an INDEPENDENT decision problem and hands the
engine an empty ``PolicyHistory``, which is correct for comparing decision
ENGINES: it removes event order as a confound. But it also means the limit,
cooldown, duplicate and spend-cap rules never fire, and those are precisely the
rules a policy scenario configures. Replaying scenarios through that harness
would produce byte-identical results for every scenario — a lab that cannot
show a difference.

Phase 19 therefore accumulates policy history ACROSS events, exactly as the
production ``execute_event`` does, but in memory:

* history is derived with the same four facts and the same rolling-24h
  semantics as ``db.get_policy_history``, computed over attempts this replay
  itself performed;
* each event is evaluated at ITS OWN timestamp, so the 24h window and the
  cooldown mean what they say instead of collapsing onto one frozen instant;
* events are processed in a canonical order fixed by the data
  (``timestamp``, then ``event_id``), so the accumulation is a pure function of
  the event SET and not of the order it was handed to us.

Nothing is written to the database. Replay cannot touch the production
``intervention_attempts`` table, so it cannot alter what the real policy engine
would decide about the next real payment.

GROUND TRUTH STAYS OUT OF THE DECISION
--------------------------------------
The hidden world is consulted ONCE per event, after the decision has been made
and the simulated execution has run. No hidden probability reaches the
classifier, the policy engine, the optimizer or the executor, and no hidden
probability is recorded on a replay record at all — so there is nothing for an
API to leak. Whether an attempt "succeeded" for history purposes is read from
the simulated EXECUTION, never from whether money came back.

EVERY NUMBER IS SIMULATED
-------------------------
Replay never calls Razorpay, never creates a Payment Link, and never performs a
customer-facing action. Recovery is realized by the synthetic hidden world.
These are controlled evaluation results, not production revenue forecasts.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from typing import Any, Mapping, Sequence

from .benchmark import DeterministicClassifier
from .benchmark_config import METHODOLOGY_PHASE17, Phase17BenchmarkConfig
from .benchmark_simulation import (
    SIMULATED,
    SimulatedExecution,
    SimulatedExecutionError,
    SimulatedExecutor,
)
from .classification import ClassificationResult
from .classifier import ClassifierAdapter, classify_event
from .execution_service import SELECTION_V2_ECONOMIC, select_for_strategy
from .generator import generate_events
from .hidden_world import HiddenWorld
from .models import PaymentEvent
from .policy import (
    InterventionAttempt,
    PolicyDecision,
    PolicyEngine,
    PolicyHistory,
    PolicyInput,
    parse_aware_datetime,
)
from .policy_scenario import PolicyScenario, PolicyScenarioError
from .selector import NO_ACTION

# Bumping this identifies a deliberate change to how replay drives the
# pipeline (ordering, history accumulation, evaluation time). It is recorded on
# every result so a comparison cannot silently span two methodologies.
REPLAY_METHODOLOGY_VERSION = "phase19-policy-replay-v1"

# Replay results are simulated evaluations. Stamped on every record and every
# result so the label travels with the number.
REPLAY_MODE_SIMULATED = "SIMULATED"

# Failure categories, mirroring the Phase 17 vocabulary so that failure
# accounting reads the same across the two harnesses. A failure is NEVER folded
# into an ordinary "did not recover" outcome.
FAILURE_CLASSIFICATION = "classification_failure"
FAILURE_POLICY = "policy_failure"
FAILURE_SELECTION = "selection_failure"
FAILURE_SIMULATION = "simulation_failure"
FAILURE_REPLAY = "replay_failure"

FAILURE_CATEGORIES: tuple[str, ...] = (
    FAILURE_CLASSIFICATION,
    FAILURE_POLICY,
    FAILURE_SELECTION,
    FAILURE_SIMULATION,
    FAILURE_REPLAY,
)


class ReplayError(Exception):
    """Replay cannot proceed honestly."""


class ReplayIntegrityError(ReplayError):
    """An invariant replay's honesty depends on was violated.

    Raised rather than repaired: a replay that executed something policy did
    not authorize, or that reported recovery on a failed event, is a broken
    harness, and producing a plausible number anyway would hide that.
    """


# ---------------------------------------------------------------------------
# Shared, policy-independent context: computed once, reused by every scenario
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ReplayEventContext:
    """Everything about one event that a policy scenario cannot change.

    Building this ONCE and sharing it across scenarios is what makes fairness
    structural rather than a convention: two scenarios cannot disagree about
    the event or its classification, because there is only one of each.

    ``failure`` is set when the shared work itself failed. Every scenario then
    records that same failure, so a broken event penalizes no scenario more
    than another.
    """

    event: PaymentEvent
    evaluation_time: datetime
    classification: ClassificationResult | None
    failure: str | None = None
    failure_category: str | None = None


def canonical_event_order(
    events: Sequence[PaymentEvent],
) -> tuple[PaymentEvent, ...]:
    """Order events by ``(timestamp, event_id)``.

    Replay accumulates policy history, so the processing order is part of the
    result. Fixing that order from the DATA rather than from the caller's list
    means the same event SET always replays identically, and that scenario B
    cannot depend on how scenario A happened to be handed its events.
    """
    return tuple(sorted(events, key=lambda event: (event.timestamp, event.event_id)))


def build_replay_contexts(
    events: Sequence[PaymentEvent],
    classifier: ClassifierAdapter | None = None,
) -> tuple[ReplayEventContext, ...]:
    """Classify every event once, in canonical order, for all scenarios.

    Classification is deliberately NOT redone per scenario. Reusing one
    classification per event is what isolates policy as the single experimental
    variable, and it keeps replay free of LLM nondeterminism and of one network
    call per event per scenario. The default classifier is the same
    project-owned deterministic adapter the Phase 17 benchmark runs on.
    """
    if classifier is None:
        classifier = DeterministicClassifier()

    contexts: list[ReplayEventContext] = []
    for event in canonical_event_order(events):
        try:
            evaluation_time = parse_aware_datetime(event.timestamp)
        except Exception as exc:
            contexts.append(
                ReplayEventContext(
                    event=event,
                    # The event's own timestamp is unusable, so there is no
                    # honest evaluation instant; the epoch placeholder is only
                    # ever attached to a record that already carries a failure.
                    evaluation_time=datetime.fromtimestamp(0, tz=None).astimezone(),
                    classification=None,
                    failure=str(exc),
                    failure_category=FAILURE_POLICY,
                )
            )
            continue
        try:
            classification = classify_event(event, classifier)
        except Exception as exc:
            contexts.append(
                ReplayEventContext(
                    event=event,
                    evaluation_time=evaluation_time,
                    classification=None,
                    failure=str(exc),
                    failure_category=FAILURE_CLASSIFICATION,
                )
            )
            continue
        contexts.append(
            ReplayEventContext(
                event=event,
                evaluation_time=evaluation_time,
                classification=classification,
            )
        )
    return tuple(contexts)


# ---------------------------------------------------------------------------
# In-memory policy history — the same four facts, never the database
# ---------------------------------------------------------------------------


class ReplayInterventionLedger:
    """Accumulates the attempts a replay performed, to derive policy history.

    Mirrors ``db.get_policy_history`` exactly — the same four facts, the same
    rolling-24h arithmetic on timezone-aware datetimes, the same per-event
    scoping for cooldown and duplicate — but over an in-memory list. Replay
    therefore exercises the real cross-event policy behaviour without any
    ability to write to, or read from, production state.
    """

    def __init__(self) -> None:
        self._attempts: list[tuple[InterventionAttempt, datetime]] = []

    def record(self, attempt: InterventionAttempt) -> None:
        """Add one performed attempt to the ledger."""
        if not isinstance(attempt, InterventionAttempt):
            raise ReplayIntegrityError("only an InterventionAttempt can be recorded")
        self._attempts.append((attempt, parse_aware_datetime(attempt.attempted_at)))

    @property
    def attempts(self) -> tuple[InterventionAttempt, ...]:
        """Every attempt performed so far, in the order performed."""
        return tuple(attempt for attempt, _ in self._attempts)

    def history_for(
        self, event: PaymentEvent, evaluation_time: datetime
    ) -> PolicyHistory:
        """Derive the four historical policy facts at ``evaluation_time``."""
        window_start = evaluation_time - timedelta(hours=24)

        customer_count_24h = 0
        existing_daily_spend_paise = 0
        most_recent: datetime | None = None
        has_successful_intervention = False

        for attempt, attempted_at in self._attempts:
            if window_start <= attempted_at <= evaluation_time:
                if attempt.customer_id == event.customer_id:
                    customer_count_24h += 1
                existing_daily_spend_paise += attempt.cost_paise
            if attempt.event_id != event.event_id:
                continue
            if most_recent is None or attempted_at > most_recent:
                most_recent = attempted_at
            if attempt.status == "successful":
                has_successful_intervention = True

        return PolicyHistory(
            customer_intervention_count_24h=customer_count_24h,
            most_recent_event_intervention_time=most_recent,
            has_successful_intervention=has_successful_intervention,
            existing_daily_spend_paise=existing_daily_spend_paise,
        )


# ---------------------------------------------------------------------------
# The per-event replay record
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ReplayEventRecord:
    """One scenario's full decision trace for one event.

    Deliberately carries NO hidden ground truth. Phase 17's per-event record
    holds ``true_probability_bps`` and ``true_ev_paise`` because its evaluation
    layer needs them to compute regret; replay does not compute regret, so it
    does not carry them, and there is consequently nothing for the Policy Lab
    API to leak. ``recovered_amount_paise`` is the realized SIMULATED outcome,
    which is the result being reported, not the probability behind it.
    """

    event_id: str
    customer_id: str
    amount_paise: int
    root_cause_category: str | None
    candidates_considered: tuple[str, ...]
    allowed_candidates: tuple[str, ...]
    denials: Mapping[str, str]
    selected_intervention: str
    selection_reason: str | None
    selected_expected_value_paise: int | None
    attempted: bool
    authorized: bool
    execution_mode: str | None
    recovered: bool
    recovered_amount_paise: int
    intervention_cost_paise: int
    replay_mode: str = REPLAY_MODE_SIMULATED
    failure: str | None = None
    failure_category: str | None = None

    def __post_init__(self) -> None:
        if self.replay_mode != REPLAY_MODE_SIMULATED:
            raise ReplayIntegrityError(
                "a replay record is always SIMULATED; replay must never report "
                "production execution"
            )
        if (self.failure is None) != (self.failure_category is None):
            raise ReplayIntegrityError(
                "a failure must always carry a category, and a category must "
                "always carry a failure"
            )
        if self.failure is not None and (
            self.recovered or self.recovered_amount_paise
        ):
            raise ReplayIntegrityError(
                "a failed event never reports recovery; a failure is not an "
                "ordinary non-recovery outcome"
            )
        if self.attempted and self.selected_intervention == NO_ACTION:
            raise ReplayIntegrityError(
                f"{NO_ACTION!r} is never an attempted intervention"
            )
        if self.attempted and not self.authorized:
            raise ReplayIntegrityError(
                "replay performs an intervention only under an authoritative "
                "policy ALLOW; an unauthorized attempt is a harness defect"
            )
        if self.execution_mode is not None and self.execution_mode != SIMULATED:
            raise ReplayIntegrityError(
                f"execution_mode must be {SIMULATED!r}, got {self.execution_mode!r}"
            )
        if self.recovered and self.recovered_amount_paise <= 0:
            raise ReplayIntegrityError(
                "a recovered event must carry a positive recovered amount"
            )
        if not self.recovered and self.recovered_amount_paise != 0:
            raise ReplayIntegrityError(
                "a non-recovered event must carry a zero recovered amount"
            )

    @property
    def blocked_count(self) -> int:
        """How many candidate interventions policy denied on this event."""
        return len(self.denials)

    def to_dict(self) -> dict[str, Any]:
        """Serialize the trace. Contains no hidden ground truth by design."""
        return {
            "event_id": self.event_id,
            "customer_id": self.customer_id,
            "amount_paise": self.amount_paise,
            "root_cause_category": self.root_cause_category,
            "candidates_considered": list(self.candidates_considered),
            "allowed_candidates": list(self.allowed_candidates),
            "denials": dict(sorted(self.denials.items())),
            "selected_intervention": self.selected_intervention,
            "selection_reason": self.selection_reason,
            "selected_expected_value_paise": self.selected_expected_value_paise,
            "attempted": self.attempted,
            "authorized": self.authorized,
            "execution_mode": self.execution_mode,
            "replay_mode": self.replay_mode,
            "simulated_recovered": self.recovered,
            "simulated_recovered_amount_paise": self.recovered_amount_paise,
            "intervention_cost_paise": self.intervention_cost_paise,
            "failure": self.failure,
            "failure_category": self.failure_category,
        }


def _failure_record(
    context: ReplayEventContext,
    detail: str,
    category: str,
    *,
    candidates: tuple[str, ...] = (),
    allowed: tuple[str, ...] = (),
    denials: Mapping[str, str] | None = None,
    selected: str = NO_ACTION,
    attempted: bool = False,
    authorized: bool = False,
    execution_mode: str | None = None,
    intervention_cost_paise: int = 0,
) -> ReplayEventRecord:
    """Build a visible, categorized failure record that claims nothing.

    A failure claims no recovery, but it must not erase what already happened:
    the caller passes whatever state the pipeline genuinely reached, so a
    failure during outcome realization still records the intervention that was
    selected and the simulated execution that really ran.
    """
    event = context.event
    return ReplayEventRecord(
        event_id=event.event_id,
        customer_id=event.customer_id,
        amount_paise=event.amount_paise,
        root_cause_category=(
            None
            if context.classification is None
            else context.classification.root_cause_category
        ),
        candidates_considered=candidates,
        allowed_candidates=allowed,
        denials=dict(denials or {}),
        selected_intervention=selected,
        selection_reason=None,
        selected_expected_value_paise=None,
        attempted=attempted,
        authorized=authorized,
        execution_mode=execution_mode,
        recovered=False,
        recovered_amount_paise=0,
        intervention_cost_paise=intervention_cost_paise,
        failure=detail,
        failure_category=category,
    )


# ---------------------------------------------------------------------------
# The replay result
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ReplayResult:
    """One scenario replayed over one event set. Every figure is simulated."""

    replay_id: str
    scenario: PolicyScenario
    config: Phase17BenchmarkConfig
    records: tuple[ReplayEventRecord, ...]
    replay_mode: str = REPLAY_MODE_SIMULATED
    methodology: str = REPLAY_METHODOLOGY_VERSION

    def __post_init__(self) -> None:
        if self.replay_mode != REPLAY_MODE_SIMULATED:
            raise ReplayIntegrityError("replay results are always SIMULATED")
        seen = {record.event_id for record in self.records}
        if len(seen) != len(self.records):
            raise ReplayIntegrityError("an event may appear at most once per replay")

    @property
    def event_count(self) -> int:
        """How many events this scenario was replayed over."""
        return len(self.records)

    def by_event(self) -> dict[str, ReplayEventRecord]:
        """Records keyed by event id, for comparison across scenarios."""
        return {record.event_id: record for record in self.records}

    def identity(self) -> dict[str, Any]:
        """Everything a replay result must be attributable to.

        Reuses the Phase 17 configuration fingerprint rather than inventing a
        second benchmark identity system: that digest already covers the event
        set, the hidden-world coefficients, the event generator, the estimator,
        the economic model, the seeds and the policy configuration. The policy
        fingerprint is published alongside it so that "A and B are identical
        except for policy" is a checkable claim about two identities and not a
        promise.
        """
        return {
            "replay_id": self.replay_id,
            "replay_methodology": self.methodology,
            "replay_mode": self.replay_mode,
            "scenario_id": self.scenario.scenario_id,
            "scenario_name": self.scenario.name,
            "policy_fingerprint": self.scenario.fingerprint(),
            "config_fingerprint": self.config.fingerprint(),
            "benchmark_methodology": self.config.methodology,
            "event_count": self.config.event_count,
            "event_seed": self.config.event_seed,
            "outcome_seed": self.config.outcome_seed,
            "replication": self.config.replication,
            "randomization_version": self.config.randomization_version,
            "classification_source": DeterministicClassifier.__name__,
        }


def replay_id_for(
    scenario: PolicyScenario, config: Phase17BenchmarkConfig
) -> str:
    """The canonical, deterministic identifier of one replay.

    Deterministic by construction: two replays that would produce identical
    results share an id, and any change to the policy or to the world changes
    it. No timestamp and no random component participates, because a replay's
    identity is what it evaluated, not when it was asked for.
    """
    return (
        f"recoveryos-replay:{REPLAY_METHODOLOGY_VERSION}:"
        f"scenario={scenario.scenario_id}:policy={scenario.fingerprint()}:"
        f"config={config.fingerprint()}"
    )


# ---------------------------------------------------------------------------
# Run
# ---------------------------------------------------------------------------


def replay_config(
    scenario: PolicyScenario, base: Phase17BenchmarkConfig | None = None
) -> Phase17BenchmarkConfig:
    """Bind a scenario's policy onto the frozen benchmark configuration.

    Uses ``dataclasses.replace`` on the existing frozen config, so everything
    that is not policy — event count, seeds, hidden world, economic model,
    randomization version — is carried over UNCHANGED and by construction
    rather than by being copied out and rebuilt. The returned value is a new
    object; the base configuration is not mutated, and neither is the active
    runtime policy.
    """
    if not isinstance(scenario, PolicyScenario):
        raise ReplayError("scenario must be a PolicyScenario")
    if base is None:
        base = Phase17BenchmarkConfig()
    if not isinstance(base, Phase17BenchmarkConfig):
        raise ReplayError("base must be a Phase17BenchmarkConfig")
    if base.methodology != METHODOLOGY_PHASE17:
        raise ReplayError(
            f"replay evaluates against {METHODOLOGY_PHASE17!r}, got "
            f"{base.methodology!r}"
        )
    return replace(base, policy_config=scenario.policy_config)


def replay_scenario(
    scenario: PolicyScenario,
    *,
    config: Phase17BenchmarkConfig | None = None,
    contexts: Sequence[ReplayEventContext] | None = None,
    events: Sequence[PaymentEvent] | None = None,
    classifier: ClassifierAdapter | None = None,
    executor: SimulatedExecutor | None = None,
) -> ReplayResult:
    """Replay one policy scenario over one event set.

    ``contexts`` is the fairness lever: a caller comparing scenarios builds the
    shared contexts once and passes the SAME object to every scenario, which
    makes identical events and identical classifications structural rather than
    something the caller has to remember to arrange.

    ``executor`` is injectable so tests can prove what replay does and does not
    call. It defaults to the benchmark's ``SimulatedExecutor``, which has no
    Razorpay import, no network import and no credential lookup, so the replay
    path cannot reach a provider even by accident.
    """
    if not isinstance(scenario, PolicyScenario):
        raise ReplayError("scenario must be a PolicyScenario")
    resolved_config = replay_config(scenario, config)

    if contexts is None:
        if events is None:
            events = generate_events(
                seed=resolved_config.event_seed, count=resolved_config.event_count
            )
        contexts = build_replay_contexts(events, classifier)
    contexts = tuple(contexts)

    if executor is None:
        executor = SimulatedExecutor()

    world = HiddenWorld(
        outcome_seed=resolved_config.outcome_seed,
        model=resolved_config.economic_model,
        replication=resolved_config.replication,
    )
    engine = PolicyEngine()
    ledger = ReplayInterventionLedger()

    records = tuple(
        _replay_one_event(
            context, scenario, resolved_config, engine, ledger, executor, world
        )
        for context in contexts
    )
    return ReplayResult(
        replay_id=replay_id_for(scenario, resolved_config),
        scenario=scenario,
        config=resolved_config,
        records=records,
    )


def _replay_one_event(
    context: ReplayEventContext,
    scenario: PolicyScenario,
    config: Phase17BenchmarkConfig,
    engine: PolicyEngine,
    ledger: ReplayInterventionLedger,
    executor: SimulatedExecutor,
    world: HiddenWorld,
) -> ReplayEventRecord:
    """Run the full RecoveryOS decision pipeline for one event, in simulation.

    The order is load bearing and is the locked architecture:

        classification -> policy gate -> optimizer -> execution -> outcome

    The optimizer runs strictly AFTER policy filtering and sees only what
    policy authorized, because ``select_for_strategy`` derives its input
    through ``AllowedCandidates``, which can only be built from authoritative
    ALLOW decisions.
    """
    if context.failure is not None:
        return _failure_record(
            context,
            context.failure,
            context.failure_category or FAILURE_REPLAY,
        )

    event = context.event
    classification = context.classification
    assert classification is not None
    candidates = tuple(classification.candidate_interventions)

    # --- policy gate -------------------------------------------------------
    try:
        history = ledger.history_for(event, context.evaluation_time)
        decisions: dict[str, PolicyDecision] = {}
        for candidate in candidates:
            if candidate == NO_ACTION:
                continue
            decisions[candidate] = engine.evaluate(
                PolicyInput(
                    event=event,
                    classification=classification,
                    proposed_intervention=candidate,
                    history=history,
                    evaluation_time=context.evaluation_time,
                ),
                scenario.policy_config,
            )
    except Exception as exc:
        return _failure_record(
            context, str(exc), FAILURE_POLICY, candidates=candidates
        )

    allowed = tuple(
        candidate for candidate in sorted(decisions) if decisions[candidate].allowed
    )
    denials = {
        candidate: decision.denial_reason
        for candidate, decision in sorted(decisions.items())
        if not decision.allowed and decision.denial_reason is not None
    }

    # --- economic optimizer (Phase 18), strictly after the gate ------------
    try:
        selected, optimizer_decision = select_for_strategy(
            event, classification, decisions, SELECTION_V2_ECONOMIC
        )
    except Exception as exc:
        return _failure_record(
            context,
            str(exc),
            FAILURE_SELECTION,
            candidates=candidates,
            allowed=allowed,
            denials=denials,
        )

    if selected != NO_ACTION and selected not in allowed:
        raise ReplayIntegrityError(
            f"optimizer selected {selected!r} on {event.event_id!r}, which "
            "policy did not authorize"
        )

    selection_reason = (
        None if optimizer_decision is None else optimizer_decision.selection_reason
    )
    selected_ev = _selected_expected_value(optimizer_decision, selected)
    cost_paise = (
        0
        if selected == NO_ACTION
        else scenario.policy_config.intervention_cost(selected)
    )

    # --- simulated execution ----------------------------------------------
    execution: SimulatedExecution | None = None
    attempted = selected != NO_ACTION
    if attempted:
        try:
            execution = executor.execute(
                event,
                selected,
                decisions[selected],
                require_authorization=True,
            )
        except SimulatedExecutionError as exc:
            return _failure_record(
                context,
                str(exc),
                FAILURE_SIMULATION,
                candidates=candidates,
                allowed=allowed,
                denials=denials,
                selected=selected,
                intervention_cost_paise=cost_paise,
            )
        # The attempt becomes history for every LATER event, exactly as a real
        # execution would. Status is read from the EXECUTION, never from
        # whether the hidden world later says money came back: letting ground
        # truth feed the duplicate rule would leak the benchmark's answer into
        # the system under test.
        ledger.record(
            InterventionAttempt(
                event_id=event.event_id,
                intervention=selected,
                customer_id=event.customer_id,
                cost_paise=cost_paise,
                attempted_at=context.evaluation_time.isoformat(),
                status=(
                    "successful" if execution.status == "SUCCESS" else "failed"
                ),
            )
        )

    # --- outcome realization, strictly after the decision ------------------
    try:
        outcome = world.realize(event, selected)
    except Exception as exc:
        return _failure_record(
            context,
            str(exc),
            FAILURE_SIMULATION,
            candidates=candidates,
            allowed=allowed,
            denials=denials,
            selected=selected,
            attempted=attempted and execution is not None,
            authorized=bool(execution and execution.authorized),
            execution_mode=None if execution is None else execution.execution_mode,
            intervention_cost_paise=cost_paise,
        )

    return ReplayEventRecord(
        event_id=event.event_id,
        customer_id=event.customer_id,
        amount_paise=event.amount_paise,
        root_cause_category=classification.root_cause_category,
        candidates_considered=candidates,
        allowed_candidates=allowed,
        denials=denials,
        selected_intervention=selected,
        selection_reason=selection_reason,
        selected_expected_value_paise=selected_ev,
        attempted=attempted,
        authorized=bool(execution and execution.authorized),
        execution_mode=None if execution is None else execution.execution_mode,
        recovered=outcome.recovered,
        recovered_amount_paise=outcome.recovered_amount_paise,
        intervention_cost_paise=cost_paise,
    )


def _selected_expected_value(
    optimizer_decision: Any, selected: str
) -> int | None:
    """The optimizer's own expected value for the option it chose, in paise.

    Already exposed by the Phase 18 economic decision trace, so publishing it
    here leaks nothing new. It is RecoveryOS's ESTIMATE, not ground truth.
    """
    if optimizer_decision is None or selected == NO_ACTION:
        return None
    for evaluation in optimizer_decision.evaluations:
        if evaluation.intervention == selected:
            return evaluation.expected_value_paise
    return None


def replay_scenarios(
    scenarios: Sequence[PolicyScenario],
    *,
    config: Phase17BenchmarkConfig | None = None,
    events: Sequence[PaymentEvent] | None = None,
    classifier: ClassifierAdapter | None = None,
    executor: SimulatedExecutor | None = None,
) -> tuple[ReplayResult, ...]:
    """Replay several scenarios over ONE shared event set and classification.

    This is the entry point a comparison should use. The events are generated
    once and classified once, and the same contexts are handed to every
    scenario, so "same events, same classifications, only policy differs" is
    guaranteed by the call graph instead of being asserted afterwards.
    """
    scenarios = tuple(scenarios)
    if not scenarios:
        raise ReplayError("at least one scenario is required")
    for scenario in scenarios:
        if not isinstance(scenario, PolicyScenario):
            raise PolicyScenarioError("every scenario must be a PolicyScenario")

    base = Phase17BenchmarkConfig() if config is None else config
    if events is None:
        events = generate_events(seed=base.event_seed, count=base.event_count)
    contexts = build_replay_contexts(events, classifier)

    return tuple(
        replay_scenario(
            scenario, config=base, contexts=contexts, executor=executor
        )
        for scenario in scenarios
    )

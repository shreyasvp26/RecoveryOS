"""Honest three-strategy benchmark harness for RecoveryOS (Phase 9).

The benchmark compares three strategies over ONE shared synthetic event set and
ONE shared hidden outcome model, reusing the Phase 8 evaluation layer
(``app/outcome_model.py`` and ``app/outcome.py``):

    No Action   -> the control: nothing is attempted; every event is valued
                   at its modeled natural (``no_action``) baseline.
    Naive Retry -> ``retry_immediate`` on every eligible non-fraud event.
                   Naive Retry has no AI, no policy, and no selector, so it
                   never fabricates an authorization; its retries are modeled
                   directly by the outcome simulator, never through the
                   policy-authorizing executor.
    RecoveryOS  -> the REAL pipeline: advisory classification (classifier ->
                   policy gate -> deterministic selection -> bounded execution)
                   through ``execution_service.execute_event`` against the
                   existing SQLite schema, with simulated recovery decided
                   only after execution was already determined.

Evaluation honesty:

* Ground truth (the hidden outcome model) is consulted ONLY to simulate the
  outcome of an already-decided intervention. It never enters classification,
  policy, selection, or execution.
* Outcomes are deterministic: the draw (seed, event, intervention) always
  produces the identical simulated outcome, independent of strategy order and
  prior simulations, so all three strategies run fairly on the same hidden
  environment.
* Recovery money is simulated. Every revenue figure is labeled simulated and
  is never presented as production Razorpay revenue.
* When a strategy attempts no intervention on an event, the modeled
  ``no_action`` baseline is what materializes for that event (uniform rule;
  see the strategy definitions below).
* Executor execution is recorded precisely: only RecoveryOS can run the
  policy-authorizing executor (``executed_by_executor`` True, with an
  executor status). Baseline strategies select/attempt an intervention but
  are modeled directly by the simulator and never claim executor success.
* A pipeline exception is always visible and is never converted into an
  ordinary recovery outcome; a failure that occurs after an intervention was
  selected and executed preserves the attempted state.
* The classifier used by default is a deterministic, project-owned controlled
  classifier (advisory, decision-time inputs only) so runs are reproducible.
  Any LLM adapter satisfying the classifier Protocol can be injected instead,
  but such a run is model-dependent and explicitly NOT reproducible.
"""

from __future__ import annotations

import argparse
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Mapping, Protocol, Sequence

from .classification import CANDIDATE_INTERVENTIONS, ClassificationResult
from .classifier import ClassifierAdapter, classify_event
from .config import build_policy_config
from .db import (
    connect,
    get_policy_decision,
    init_db,
    insert_classification_result,
    insert_payment_event,
)
from .execution_service import (
    STATUS_EXECUTION_FAILED,
    STATUS_EXECUTION_SUCCESS,
    STATUS_MISSING_CLASSIFICATION,
    STATUS_NOT_FOUND,
    STATUS_NO_ACTION,
    execute_event,
)
from .executor import EXECUTION_STATUSES
from .generator import generate_events
from .models import PaymentEvent
from .outcome import OutcomeSimulator, RecoveryOutcome
from .outcome_model import generate_hidden_outcome_model
from .policy import PolicyConfig
from .selector import NO_ACTION

# Canonical benchmark configuration (Phase 9). The event set size is fixed at
# 500 unless an existing canonical configuration overrides it; none exists in
# the repository, so the benchmark module is the canonical source.
BENCHMARK_EVENT_COUNT = 500
BENCHMARK_DEFAULT_SEED = 42
BENCHMARK_EVALUATION_TIME = datetime(2026, 8, 27, 13, 0, tzinfo=timezone.utc)

# Locked strategy identifiers, in canonical report order.
STRATEGY_NO_ACTION = "no_action"
STRATEGY_NAIVE_RETRY = "naive_retry"
STRATEGY_RECOVERY_OS = "recovery_os"
STRATEGIES: tuple[str, ...] = (
    STRATEGY_NO_ACTION,
    STRATEGY_NAIVE_RETRY,
    STRATEGY_RECOVERY_OS,
)

NAIVE_RETRY_INTERVENTION = "retry_immediate"

# Recovery sources recorded on every event result.
RECOVERY_SOURCE_ATTEMPT = "attempt"
RECOVERY_SOURCE_PASSIVE = "passive"

EVALUATION_MODE = "SIMULATED"


class BenchmarkError(Exception):
    """Base class for all explicit benchmark failures."""


class InvalidBenchmarkConfigurationError(BenchmarkError):
    """Benchmark configuration is malformed or an invariant was violated."""


# Failure reasons the deterministic controlled classifier maps to terminal.
_TERMINAL_REASONS: frozenset[str] = frozenset({"transaction_declined", "payment_failed"})

# Failure reasons the deterministic controlled classifier maps to customer action.
_CUSTOMER_ACTION_REASONS: frozenset[str] = frozenset(
    {
        "insufficient_funds",
        "authentication_failed",
        "declined_by_bank",
        "expired_card",
    }
)


class DeterministicClassifier:
    """Controlled, project-owned advisory classifier reproducible runs default to.

    Produces a valid classification JSON string from decision-time event
    information only (it parses the deterministic prompt payload). It never
    calls a network, never sees hidden ground truth, and is advisory only:
    it merely recommends; the policy gate still authorizes.
    """

    def generate(self, prompt: str) -> str:
        marker = "Event:\n"
        payload_text = prompt.partition(marker)[2]
        try:
            event_data = json.loads(payload_text)
        except (json.JSONDecodeError, TypeError) as exc:
            raise InvalidBenchmarkConfigurationError(
                "deterministic classifier could not read its prompt payload"
            ) from exc
        result = self._classify_from_dict(event_data)
        return json.dumps(result)

    @staticmethod
    def _classify_from_dict(event_data: Mapping[str, Any]) -> dict[str, Any]:
        risk_flag = event_data["risk_flag"]
        failure_reason = event_data["failure_reason"]
        if risk_flag == "fraud_suspect":
            root = "fraud_suspect"
        elif failure_reason in _TERMINAL_REASONS:
            root = "terminal"
        elif failure_reason in _CUSTOMER_ACTION_REASONS:
            root = "customer_action_needed"
        else:
            root = "transient"
        return {
            "event_id": event_data["event_id"],
            "root_cause_category": root,
            "confidence": 0.9,
            "reasoning": (
                f"deterministic controlled classifier: risk_flag={risk_flag}, "
                f"failure_reason={failure_reason} -> {root}"
            ),
            "candidate_interventions": sorted(
                CANDIDATE_INTERVENTIONS - {NO_ACTION}
            ),
        }


@dataclass(frozen=True)
class BenchmarkEventResult:
    """One simulated outcome for one event under one strategy.

    The record is explicit about what happened:
    - attempted: an intervention was selected/attempted on this event;
    - executed_by_executor: the intervention actually ran through the
      RecoveryOS policy-authorizing executor (BoundedExecutor). False when the
      intervention was modeled directly by the outcome simulator without the
      executor (the No Action baseline and Naive Retry), even when attempted;
    - execution_status: the executor's SUCCESS/FAILED (or None when the
      executor did not run);
    - skipped: the strategy bypassed this event by eligibility definition;
    - exception: the pipeline failed on this event and produced no simulated
      outcome (recovered is then False and the recovered amount is 0);
    - recovery_source: "attempt" when the outcome came from an attempted
      intervention, "passive" when the modeled no_action baseline applied.
    """

    event_id: str
    strategy: str
    intervention: str
    attempted: bool
    executed_by_executor: bool
    execution_status: str | None
    skipped: bool
    exception: str | None
    recovered: bool
    recovered_amount_paise: int
    recovery_source: str
    denial_reasons: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.event_id, str) or not self.event_id.strip():
            raise InvalidBenchmarkConfigurationError("event_id must be a non-empty string")
        if self.strategy not in STRATEGIES:
            raise InvalidBenchmarkConfigurationError(
                f"strategy must be one of {STRATEGIES}, got {self.strategy!r}"
            )
        if self.intervention not in CANDIDATE_INTERVENTIONS:
            raise InvalidBenchmarkConfigurationError(
                f"intervention must be one of {sorted(CANDIDATE_INTERVENTIONS)}, "
                f"got {self.intervention!r}"
            )
        if type(self.attempted) is not bool:
            raise InvalidBenchmarkConfigurationError("attempted must be a boolean")
        if type(self.executed_by_executor) is not bool:
            raise InvalidBenchmarkConfigurationError(
                "executed_by_executor must be a boolean"
            )
        if self.execution_status is not None and self.execution_status not in EXECUTION_STATUSES:
            raise InvalidBenchmarkConfigurationError(
                f"execution_status must be None or one of {sorted(EXECUTION_STATUSES)}, "
                f"got {self.execution_status!r}"
            )
        if self.executed_by_executor:
            if not self.attempted:
                raise InvalidBenchmarkConfigurationError(
                    "an executor-run intervention must also be attempted"
                )
            if self.execution_status is None:
                raise InvalidBenchmarkConfigurationError(
                    "an executor-run intervention must carry an execution_status"
                )
        else:
            if self.execution_status is not None:
                raise InvalidBenchmarkConfigurationError(
                    "a non-executor event must not carry an execution_status"
                )
        if type(self.skipped) is not bool:
            raise InvalidBenchmarkConfigurationError("skipped must be a boolean")
        if self.exception is not None and not isinstance(self.exception, str):
            raise InvalidBenchmarkConfigurationError("exception must be a string or None")
        if self.exception is None:
            if type(self.recovered) is not bool:
                raise InvalidBenchmarkConfigurationError("recovered must be a boolean")
            if type(self.recovered_amount_paise) is not int or self.recovered_amount_paise < 0:
                raise InvalidBenchmarkConfigurationError(
                    "recovered_amount_paise must be a non-negative integer"
                )
            if self.recovered and self.recovered_amount_paise <= 0:
                raise InvalidBenchmarkConfigurationError(
                    "a recovered event must carry a positive recovered amount"
                )
            if not self.recovered and self.recovered_amount_paise != 0:
                raise InvalidBenchmarkConfigurationError(
                    "a non-recovered event must carry zero recovered amount"
                )
            object.__setattr__(self, "exception", None)
        else:
            object.__setattr__(self, "recovered", False)
            object.__setattr__(self, "recovered_amount_paise", 0)
        if self.recovery_source not in (RECOVERY_SOURCE_ATTEMPT, RECOVERY_SOURCE_PASSIVE):
            raise InvalidBenchmarkConfigurationError(
                f"recovery_source must be one of "
                f"'{RECOVERY_SOURCE_ATTEMPT}', '{RECOVERY_SOURCE_PASSIVE}', "
                f"got {self.recovery_source!r}"
            )
        if self.attempted and self.recovery_source != RECOVERY_SOURCE_ATTEMPT:
            raise InvalidBenchmarkConfigurationError(
                "an attempted intervention must record an attempt recovery source"
            )
        if not self.attempted and self.recovery_source != RECOVERY_SOURCE_PASSIVE:
            raise InvalidBenchmarkConfigurationError(
                "a non-attempted event must record a passive recovery source"
            )
        object.__setattr__(
            self,
            "denial_reasons",
            tuple(reason for reason in self.denial_reasons if reason),
        )

    def to_dict(self) -> dict[str, Any]:
        """Serialize to a plain dict, preserving the record contract."""
        return {
            "event_id": self.event_id,
            "strategy": self.strategy,
            "intervention": self.intervention,
            "attempted": self.attempted,
            "executed_by_executor": self.executed_by_executor,
            "execution_status": self.execution_status,
            "skipped": self.skipped,
            "exception": self.exception,
            "recovered": self.recovered,
            "recovered_amount_paise": self.recovered_amount_paise,
            "recovery_source": self.recovery_source,
            "denial_reasons": list(self.denial_reasons),
        }

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, BenchmarkEventResult):
            return NotImplemented
        return self.to_dict() == other.to_dict()


@dataclass(frozen=True)
class BenchmarkStrategyResult:
    """The aggregate result for one strategy over the shared event set.

    Accounting invariant: processed + skipped + exceptions == event_count.
    processed counts events that produced a simulated outcome (attempted or
    passive) without an exception. successful_interventions counts only events
    whose intervention ran through the RecoveryOS executor and reported SUCCESS
    (baseline strategies that never call the executor report 0).
    failed_outcomes counts attempted events that simulated non-recovery and
    are NOT exceptions (an exception is never a failed outcome, and a
    post-execution exception is never an ordinary unsuccessful recovery).
    skipped counts events the strategy bypassed by eligibility definition.
    """

    strategy: str
    event_count: int
    interventions_attempted: int
    successful_interventions: int
    recovered_events: int
    recovered_amount_paise: int
    failed_outcomes: int
    skipped_events: int
    exceptions: int
    processed: int

    def __post_init__(self) -> None:
        if self.strategy not in STRATEGIES:
            raise InvalidBenchmarkConfigurationError(
                f"strategy must be one of {STRATEGIES}, got {self.strategy!r}"
            )
        for name in (
            "event_count",
            "interventions_attempted",
            "successful_interventions",
            "recovered_events",
            "recovered_amount_paise",
            "failed_outcomes",
            "skipped_events",
            "exceptions",
            "processed",
        ):
            value = getattr(self, name)
            if type(value) is not int or value < 0:
                raise InvalidBenchmarkConfigurationError(
                    f"{name} must be a non-negative integer"
                )
        if self.event_count < 1:
            raise InvalidBenchmarkConfigurationError("event_count must be at least 1")
        for name in (
            "interventions_attempted",
            "successful_interventions",
            "recovered_events",
            "failed_outcomes",
            "skipped_events",
            "exceptions",
        ):
            if getattr(self, name) > self.event_count:
                raise InvalidBenchmarkConfigurationError(
                    f"{name} cannot exceed event_count"
                )
        if self.successful_interventions > self.interventions_attempted:
            raise InvalidBenchmarkConfigurationError(
                "successful_interventions cannot exceed interventions_attempted"
            )
        if self.failed_outcomes > self.interventions_attempted:
            raise InvalidBenchmarkConfigurationError(
                "failed_outcomes cannot exceed interventions_attempted"
            )
        if self.processed + self.skipped_events + self.exceptions != self.event_count:
            raise InvalidBenchmarkConfigurationError(
                "accounting invariant violated: "
                "processed + skipped + exceptions must equal event_count"
            )

    def to_dict(self) -> dict[str, Any]:
        """Serialize to a plain dict, preserving the result contract."""
        return {
            "strategy": self.strategy,
            "event_count": self.event_count,
            "interventions_attempted": self.interventions_attempted,
            "successful_interventions": self.successful_interventions,
            "recovered_events": self.recovered_events,
            "recovered_amount_paise": self.recovered_amount_paise,
            "failed_outcomes": self.failed_outcomes,
            "skipped_events": self.skipped_events,
            "exceptions": self.exceptions,
            "processed": self.processed,
        }

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, BenchmarkStrategyResult):
            return NotImplemented
        return self.to_dict() == other.to_dict()


@dataclass(frozen=True)
class BenchmarkRunResult:
    """Run-level benchmark summary, in locked canonical strategy order."""

    run_id: str
    seed: int
    event_count: int
    model_seed: int
    evaluation_time: str
    evaluation_mode: str
    strategy_results: tuple[BenchmarkStrategyResult, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.run_id, str) or not self.run_id.strip():
            raise InvalidBenchmarkConfigurationError("run_id must be a non-empty string")
        if type(self.seed) is not int:
            raise InvalidBenchmarkConfigurationError("seed must be an integer")
        if type(self.model_seed) is not int:
            raise InvalidBenchmarkConfigurationError("model_seed must be an integer")
        if type(self.event_count) is not int or self.event_count < 1:
            raise InvalidBenchmarkConfigurationError("event_count must be a positive integer")
        if self.evaluation_mode != EVALUATION_MODE:
            raise InvalidBenchmarkConfigurationError(
                f"benchmark results are simulated; evaluation_mode must be "
                f"{EVALUATION_MODE!r}"
            )
        if (
            not isinstance(self.strategy_results, (list, tuple))
            or tuple(r.strategy for r in self.strategy_results) != STRATEGIES
        ):
            raise InvalidBenchmarkConfigurationError(
                f"strategy_results must cover exactly {STRATEGIES} in order"
            )
        for result in self.strategy_results:
            if result.event_count != self.event_count:
                raise InvalidBenchmarkConfigurationError(
                    "every strategy must evaluate the same shared event set"
                )
        object.__setattr__(
            self, "strategy_results", tuple(self.strategy_results)
        )

    def to_dict(self) -> dict[str, Any]:
        """Serialize to a plain dict, preserving the run contract."""
        return {
            "run_id": self.run_id,
            "seed": self.seed,
            "event_count": self.event_count,
            "model_seed": self.model_seed,
            "evaluation_time": self.evaluation_time,
            "evaluation_mode": self.evaluation_mode,
            "strategy_results": [
                result.to_dict() for result in self.strategy_results
            ],
        }

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, BenchmarkRunResult):
            return NotImplemented
        return self.to_dict() == other.to_dict()


@dataclass(frozen=True)
class BenchmarkReport:
    """Full benchmark output: the run summary plus per-event records.

    The per-event records are the raw foundation behind the metrics (including
    the false-intervention foundation) and the integrity/accounting checks.
    They are kept separate from the compact run summary by design; the summary
    is never a giant JSON blob.
    """

    run: BenchmarkRunResult
    event_results: Mapping[str, tuple[BenchmarkEventResult, ...]]
    events: tuple[PaymentEvent, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.run, BenchmarkRunResult):
            raise InvalidBenchmarkConfigurationError(
                "run must be a BenchmarkRunResult"
            )
        if not isinstance(self.event_results, Mapping):
            raise InvalidBenchmarkConfigurationError(
                "event_results must be a mapping of strategy -> records"
            )
        unknown = set(self.event_results) - set(STRATEGIES)
        if unknown:
            raise InvalidBenchmarkConfigurationError(
                f"event_results contains unknown strategies: {sorted(unknown)}"
            )
        if not self.event_results:
            raise InvalidBenchmarkConfigurationError(
                "event_results must name at least one strategy"
            )
        for strategy in STRATEGIES:
            records = self.event_results.get(strategy)
            if records is None:
                continue
            if not isinstance(records, (list, tuple)):
                raise InvalidBenchmarkConfigurationError(
                    f"event_results[{strategy!r}] must be a sequence"
                )
            if len(records) != self.run.event_count:
                raise InvalidBenchmarkConfigurationError(
                    "every strategy must evaluate the same shared event set"
                )
            if any(
                not isinstance(record, BenchmarkEventResult)
                or record.strategy != strategy
                for record in records
            ):
                raise InvalidBenchmarkConfigurationError(
                    f"event_results[{strategy!r}] records are malformed"
                )
        if not isinstance(self.events, (list, tuple)):
            raise InvalidBenchmarkConfigurationError(
                "events must be a sequence of PaymentEvent"
            )
        events = tuple(self.events)
        if not events:
            raise InvalidBenchmarkConfigurationError("events must not be empty")
        if len(events) != self.run.event_count:
            raise InvalidBenchmarkConfigurationError(
                "events must be the exact shared event set evaluated by the run"
            )
        if any(not isinstance(event, PaymentEvent) for event in events):
            raise InvalidBenchmarkConfigurationError(
                "events must contain only PaymentEvent instances"
            )
        object.__setattr__(self, "events", events)


def _exception_record(
    event: PaymentEvent,
    strategy: str,
    detail: str,
    *,
    attempted: bool = False,
    executed_by_executor: bool = False,
    intervention: str = NO_ACTION,
    execution_status: str | None = None,
) -> BenchmarkEventResult:
    """Build an exception record carrying the pipeline failure (never conflation).

    For a pre-intervention failure (e.g. classification) ``attempted`` stays
    False. For a failure that happens AFTER an intervention was selected and
    executed (e.g. the outcome simulation throws), the known state at failure
    is preserved: the intervention, whether the executor ran, and the executor
    status are carried on the record, and ``recovery_source`` is "attempt".
    The record never claims recovery and never converts the exception into an
    ordinary unsuccessful recovery.
    """
    return BenchmarkEventResult(
        event_id=event.event_id,
        strategy=strategy,
        intervention=intervention,
        attempted=attempted,
        executed_by_executor=executed_by_executor,
        execution_status=execution_status,
        skipped=False,
        exception=detail,
        recovered=False,
        recovered_amount_paise=0,
        recovery_source=(
            RECOVERY_SOURCE_ATTEMPT if attempted else RECOVERY_SOURCE_PASSIVE
        ),
    )


def run_no_action(
    events: Sequence[PaymentEvent], simulator: OutcomeSimulator
) -> tuple[BenchmarkEventResult, ...]:
    """The control: no intervention is ever attempted on any event.

    Every event is valued at its modeled natural baseline. An outcome
    simulation failure surfaces as an explicit exception record.
    """
    results: list[BenchmarkEventResult] = []
    for event in events:
        try:
            outcome: RecoveryOutcome = simulator.simulate(event, NO_ACTION)
        except Exception as exc:
            results.append(_exception_record(event, STRATEGY_NO_ACTION, str(exc)))
            continue
        results.append(
            BenchmarkEventResult(
                event_id=event.event_id,
                strategy=STRATEGY_NO_ACTION,
                intervention=NO_ACTION,
                attempted=False,
                executed_by_executor=False,
                execution_status=None,
                skipped=False,
                exception=None,
                recovered=outcome.recovered,
                recovered_amount_paise=outcome.recovered_amount_paise,
                recovery_source=RECOVERY_SOURCE_PASSIVE,
            )
        )
    return tuple(results)


def run_naive_retry(
    events: Sequence[PaymentEvent], simulator: OutcomeSimulator
) -> tuple[BenchmarkEventResult, ...]:
    """retry_immediate on every eligible non-fraud event; fraud is excluded.

    Eligibility: ``risk_flag != "fraud_suspect"``. Naive Retry has no AI, no
    policy, no selector, and no hidden probabilities. It never consults hidden
    ground truth to decide, and it never fabricates a PolicyDecision, so its
    retries are modeled directly by the simulator (never through the
    policy-authorizing executor). Skipped fraud events receive no retry and
    are valued at the modeled natural baseline (uniform "do nothing" rule).
    """
    results: list[BenchmarkEventResult] = []
    for event in events:
        if event.risk_flag == "fraud_suspect":
            try:
                outcome = simulator.simulate(event, NO_ACTION)
            except Exception as exc:
                results.append(_exception_record(event, STRATEGY_NAIVE_RETRY, str(exc)))
                continue
            results.append(
                BenchmarkEventResult(
                    event_id=event.event_id,
                    strategy=STRATEGY_NAIVE_RETRY,
                    intervention=NO_ACTION,
                    attempted=False,
                    executed_by_executor=False,
                    execution_status=None,
                    skipped=True,
                    exception=None,
                    recovered=outcome.recovered,
                    recovered_amount_paise=outcome.recovered_amount_paise,
                    recovery_source=RECOVERY_SOURCE_PASSIVE,
                )
            )
            continue
        try:
            outcome = simulator.simulate(event, NAIVE_RETRY_INTERVENTION)
        except Exception as exc:
            results.append(_exception_record(event, STRATEGY_NAIVE_RETRY, str(exc)))
            continue
        results.append(
            BenchmarkEventResult(
                event_id=event.event_id,
                strategy=STRATEGY_NAIVE_RETRY,
                intervention=NAIVE_RETRY_INTERVENTION,
                attempted=True,
                executed_by_executor=False,
                execution_status=None,
                skipped=False,
                exception=None,
                recovered=outcome.recovered,
                recovered_amount_paise=outcome.recovered_amount_paise,
                recovery_source=RECOVERY_SOURCE_ATTEMPT,
            )
        )
    return tuple(results)


def run_recoveryos(
    events: Sequence[PaymentEvent],
    simulator: OutcomeSimulator,
    classifier: ClassifierAdapter,
    evaluation_time: datetime,
    policy_config: PolicyConfig,
) -> tuple[BenchmarkEventResult, ...]:
    """Run the REAL RecoveryOS pipeline over the shared event set.

    Events are persisted into an isolated SQLite database through the existing
    schema; each event is classified through the real advisory classifier
    boundary; ``execution_service.execute_event`` derives authoritative policy
    decisions, selects one intervention deterministically, and executes it
    through the policy-authorizing executor (no Razorpay client is ever
    configured, so no real provider call is possible). Recovery is simulated
    ONLY after execution was determined. Policy-denied events are captured
    (with their denial reasons) and valued at the natural baseline, never
    discarded.
    """
    conn = connect(":memory:")
    init_db(conn)
    try:
        for event in events:
            insert_payment_event(conn, event)

        classification_failures: dict[str, str] = {}
        for event in events:
            try:
                classification = classify_event(event, classifier)
            except Exception as exc:
                classification_failures[event.event_id] = str(exc)
                continue
            insert_classification_result(conn, classification)

        evaluated_at = evaluation_time.astimezone(timezone.utc).isoformat()
        results: list[BenchmarkEventResult] = []
        for event in events:
            if event.event_id in classification_failures:
                results.append(
                    _exception_record(
                        event,
                        STRATEGY_RECOVERY_OS,
                        classification_failures[event.event_id],
                    )
                )
                continue
            try:
                service_result = execute_event(
                    conn,
                    event.event_id,
                    evaluation_time,
                    policy_config,
                    razorpay_client=None,
                )
            except Exception as exc:
                results.append(
                    _exception_record(event, STRATEGY_RECOVERY_OS, str(exc))
                )
                continue

            if service_result.status in (
                STATUS_EXECUTION_SUCCESS,
                STATUS_EXECUTION_FAILED,
            ):
                intervention = service_result.selected_intervention
                try:
                    outcome = simulator.simulate(event, intervention)
                except Exception as exc:
                    results.append(
                        _exception_record(
                            event,
                            STRATEGY_RECOVERY_OS,
                            str(exc),
                            attempted=True,
                            executed_by_executor=True,
                            intervention=intervention,
                            execution_status=service_result.outcome.status,
                        )
                    )
                    continue
                results.append(
                    BenchmarkEventResult(
                        event_id=event.event_id,
                        strategy=STRATEGY_RECOVERY_OS,
                        intervention=intervention,
                        attempted=True,
                        executed_by_executor=True,
                        execution_status=service_result.outcome.status,
                        skipped=False,
                        exception=None,
                        recovered=outcome.recovered,
                        recovered_amount_paise=outcome.recovered_amount_paise,
                        recovery_source=RECOVERY_SOURCE_ATTEMPT,
                    )
                )
                continue

            if service_result.status == STATUS_NO_ACTION:
                denied: list[str] = []
                for candidate in sorted(CANDIDATE_INTERVENTIONS - {NO_ACTION}):
                    decision = get_policy_decision(
                        conn, event.event_id, candidate, evaluated_at
                    )
                    if decision is not None and not decision.allowed:
                        denied.append(decision.denial_reason)
                try:
                    outcome = simulator.simulate(event, NO_ACTION)
                except Exception as exc:
                    results.append(
                        _exception_record(event, STRATEGY_RECOVERY_OS, str(exc))
                    )
                    continue
                results.append(
                    BenchmarkEventResult(
                        event_id=event.event_id,
                        strategy=STRATEGY_RECOVERY_OS,
                        intervention=NO_ACTION,
                        attempted=False,
                        executed_by_executor=False,
                        execution_status=None,
                        skipped=False,
                        exception=None,
                        recovered=outcome.recovered,
                        recovered_amount_paise=outcome.recovered_amount_paise,
                        recovery_source=RECOVERY_SOURCE_PASSIVE,
                        denial_reasons=tuple(set(denied)),
                    )
                )
                continue

            if service_result.status == STATUS_MISSING_CLASSIFICATION:
                results.append(
                    _exception_record(
                        event,
                        STRATEGY_RECOVERY_OS,
                        "missing_classification: no valid classification",
                    )
                )
                continue

            results.append(
                _exception_record(
                    event, STRATEGY_RECOVERY_OS, "event_not_found"
                )
            )
        return tuple(results)
    finally:
        conn.close()


def _summarize(
    strategy: str, event_results: Sequence[BenchmarkEventResult]
) -> BenchmarkStrategyResult:
    """Aggregate a strategy's event records into a validated strategy result."""
    event_count = len(event_results)
    exceptions = sum(1 for record in event_results if record.exception is not None)
    skipped_events = sum(1 for record in event_results if record.skipped)
    interventions_attempted = sum(1 for record in event_results if record.attempted)
    successful_interventions = sum(
        1
        for record in event_results
        if record.attempted and record.execution_status == "SUCCESS"
    )
    recovered_events = sum(1 for record in event_results if record.recovered)
    recovered_amount_paise = sum(
        record.recovered_amount_paise for record in event_results
    )
    failed_outcomes = sum(
        1
        for record in event_results
        if record.attempted
        and not record.recovered
        and record.exception is None
    )
    return BenchmarkStrategyResult(
        strategy=strategy,
        event_count=event_count,
        interventions_attempted=interventions_attempted,
        successful_interventions=successful_interventions,
        recovered_events=recovered_events,
        recovered_amount_paise=recovered_amount_paise,
        failed_outcomes=failed_outcomes,
        skipped_events=skipped_events,
        exceptions=exceptions,
        processed=event_count - skipped_events - exceptions,
    )


def _run_strategies(
    events: Sequence[PaymentEvent],
    simulator: OutcomeSimulator,
    classifier: ClassifierAdapter,
    evaluation_time: datetime,
    policy_config: PolicyConfig,
    order: Sequence[str],
) -> dict[str, tuple[BenchmarkEventResult, ...]]:
    """Run the strategies in the supplied order on shared, isolated state."""
    results: dict[str, tuple[BenchmarkEventResult, ...]] = {}
    for strategy in order:
        if strategy == STRATEGY_NO_ACTION:
            results[strategy] = run_no_action(events, simulator)
        elif strategy == STRATEGY_NAIVE_RETRY:
            results[strategy] = run_naive_retry(events, simulator)
        elif strategy == STRATEGY_RECOVERY_OS:
            results[strategy] = run_recoveryos(
                events, simulator, classifier, evaluation_time, policy_config
            )
        else:
            raise InvalidBenchmarkConfigurationError(
                f"unknown strategy {strategy!r}"
            )
    return results


def run_benchmark(
    *,
    seed: int = BENCHMARK_DEFAULT_SEED,
    event_count: int = BENCHMARK_EVENT_COUNT,
    classifier: ClassifierAdapter | None = None,
    evaluation_time: datetime = BENCHMARK_EVALUATION_TIME,
    policy_config: PolicyConfig | None = None,
    order: Sequence[str] = STRATEGIES,
) -> BenchmarkReport:
    """Run the three-strategy benchmark over the shared event set.

    The shared event set and the shared hidden outcome model are both derived
    from the single explicit seed, so the same configuration always produces
    the identical, reproducible run. Recovery is determined exclusively after
    execution was decided; the hidden model never influences a decision.
    """
    if type(seed) is not int:
        raise InvalidBenchmarkConfigurationError("seed must be an integer")
    if type(event_count) is not int or event_count < 1:
        raise InvalidBenchmarkConfigurationError(
            "event_count must be a positive integer"
        )
    if classifier is None:
        classifier = DeterministicClassifier()
    if policy_config is None:
        policy_config = build_policy_config()

    events = generate_events(seed=seed, count=event_count)
    model = generate_hidden_outcome_model(events, seed)
    simulator = OutcomeSimulator(model)

    unknown = set(order) - set(STRATEGIES)
    if unknown:
        raise InvalidBenchmarkConfigurationError(
            f"unknown strategies in order: {sorted(unknown)}"
        )
    if len(order) != len(set(order)):
        raise InvalidBenchmarkConfigurationError(
            "order must not name a strategy more than once"
        )
    if not order:
        raise InvalidBenchmarkConfigurationError("order must name at least one strategy")
    effective_order = tuple(order)

    event_results = _run_strategies(
        events,
        simulator,
        classifier,
        evaluation_time,
        policy_config,
        effective_order,
    )

    strategy_results = tuple(
        _summarize(strategy, event_results[strategy]) for strategy in STRATEGIES
    )
    run = BenchmarkRunResult(
        run_id=(
            f"recoveryos-benchmark:simulated:"
            f"seed={seed}:event_count={event_count}:model_seed={seed}"
        ),
        seed=seed,
        event_count=event_count,
        model_seed=model.seed,
        evaluation_time=evaluation_time.astimezone(timezone.utc).isoformat(),
        evaluation_mode=EVALUATION_MODE,
        strategy_results=strategy_results,
    )
    return BenchmarkReport(
        run=run, event_results=event_results, events=tuple(events)
    )


def _main(argv: Sequence[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Run the honest three-strategy RecoveryOS benchmark over a shared "
            "synthetic event set and shared hidden outcome model."
        )
    )
    parser.add_argument("--seed", type=int, default=BENCHMARK_DEFAULT_SEED)
    parser.add_argument("--count", type=int, default=BENCHMARK_EVENT_COUNT)
    args = parser.parse_args(argv)
    report = run_benchmark(seed=args.seed, event_count=args.count)
    print(json.dumps(report.run.to_dict(), indent=2))


if __name__ == "__main__":
    _main()

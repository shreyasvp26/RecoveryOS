# RecoveryOS Benchmark Methodology

> **Status note:** This document describes the implemented benchmark. Phase 8 delivered the deterministic, hidden, event-specific outcome model (`app/outcome_model.py`) and the deterministic recovery simulation (`app/outcome.py`), both strictly isolated from the decision path. Phase 9 delivers the harness itself: the three strategies (`app/benchmark.py`), the metrics (`app/benchmark_metrics.py`), and the integrity/fairness/reproducibility tests.

## Purpose

The benchmark measures whether RecoveryOS recovers more *simulated* revenue than baseline approaches under identical conditions. It proves value by comparison, not by claiming absolute revenue.

## Baselines

The benchmark compares RecoveryOS against two baselines over the **same 500-event set** (Phase 9 canonical: `BENCHMARK_EVENT_COUNT = 500`, seed 42):

- **No Action** — the control: nothing is attempted. Establishes the natural (zero-intervention) outcome. Every event is valued at its modeled `no_action` baseline.
- **Naive Retry** — `retry_immediate` on every eligible **non-fraud** event (`risk_flag != "fraud_suspect"`); fraud events are skipped. Naive Retry has no AI, no policy, and no selector, so its retries are modeled directly by the outcome simulator and it never fabricates a policy authorization. Skipped fraud events and any event with no retry are valued at the modeled `no_action` baseline (uniform "do nothing" rule).
- **RecoveryOS** — the full real pipeline: advisory classification → deterministic policy gate → deterministic selection → bounded execution, run through the existing frozen modules against an isolated in-memory SQLite database. Recovery is simulated only after execution was already determined.

Because all three run against the **same event set** and the **same hidden outcome model**, differences in outcome are attributable to the strategy, not the data.

## Outcome Model

Outcomes are produced by a **hidden intervention-specific outcome model** (Phase 8 foundation):

- Unit of evaluation: a chosen intervention on a specific event.
- The outcome model is **deterministic**: a per-event `random.Random(f"{seed}:{event_id}")` draws each intervention probability from an explicit integer seed; the same seed and event set always produce the identical model, and a different seed produces a different one.
- It is **event-specific**: every event receives its own probability for every locked intervention (including `no_action`), correlated with the event identity — never a single global probability per intervention.
- It is **hidden**: the model and its probabilities are evaluation-owned. The classifier, policy gate, selector, executor, and Razorpay boundary never receive, see, or act on it.
- Every probability satisfies `0 <= p <= 1`; an invalid or missing value fails explicitly (never clamped, never defaulted).

## Deterministic Seed

A **deterministic seed** is fixed so that benchmark runs are reproducible: the same event set and seed always produce the same outcomes. Phase 8 draws the outcome for any (seed, event, intervention) triple from its own `random.Random(f"{seed}:{event_id}:{intervention}")`, so the result is also **independent of evaluation order, strategy order, and prior simulations**.

## Strategy Fairness

Because each (seed, event, intervention) outcome is a pure function of the triple, the three strategies are evaluated **fairly** on the exact same hidden environment, regardless of the order in which they run. The harness accepts an explicit strategy `order`, and the integrity tests assert order-invariance (e.g. `[no_action, naive_retry, recovery_os]` produces byte-identical outcomes to `[recovery_os, no_action, naive_retry]`).

## Simulated Outcomes

All outcomes and all **recovery amounts are simulated** (Phase 8 foundation):

- Each executed intervention is evaluated by a single Bernoulli draw against its hidden per-event probability; `recovered_amount_paise` is derived from the event's amount when recovered, else zero.
- They are **not** produced by real Razorpay transactions. The RecoveryOS strategy runs against an in-memory SQLite database with no Razorpay client configured, so no real provider call is ever made.
- They are labeled as **simulated** in all results and reporting (`evaluation_mode = "SIMULATED"`).
- **Execution success ≠ recovery success:** an intervention that executed successfully can still simulate a non-recovery (and vice versa); recovery is decided only at the evaluation boundary.

## No Forced Positive Result

The benchmark does **not force positive outcomes**. There is no guarantee that RecoveryOS (or any strategy) succeeds. Honest, representative outcomes are expected, including failures and zero-recovery results where appropriate. On the canonical seed 42 run, RecoveryOS recovers **less** simulated revenue than both baselines; that result is reported honestly and is the intended demonstration of a non-rigged benchmark.

## Data Provenance

The event set is **seeded synthetic data** produced by `app/generator.py`. **No benchmark figure is derived from real customer payment data**, from real Razorpay transactions (Test Mode or otherwise), or from the live closed-loop verification described in the README. The benchmark and the live Razorpay verification are entirely separate exercises and their numbers must never be combined or presented as one result.

## Disclosed Limitation — The Outcome Model Carries No Signal

This is the most important caveat about interpreting any benchmark result, and it is a property of the hidden model by construction.

Recovery probabilities are drawn as **independent uniform values** — `rng.random()` per (event, intervention) pair in `generate_hidden_outcome_model`. They are therefore:

- **uncorrelated with every event feature** — failure reason, payment method, amount, risk flag, and customer history have no bearing on which intervention succeeds; and
- **uncorrelated across interventions** — no intervention is genuinely better suited to any given event.

Every intervention on every event consequently has an expected recovery probability of ≈0.5. Two conclusions follow, and both are stated plainly:

1. **The benchmark cannot reward intelligent targeting.** Since no intervention is truly better for any event, no classifier or selector — however good — can beat a blanket strategy on expected recovery. The only lever that moves simulated recovered revenue is *how many* events a strategy intervenes on.
2. **The canonical seed-42 result reflects exactly this.** No Action recovers 242/500 events, Naive Retry 246/500, and RecoveryOS 241/500 — statistically flat at the ~0.5 the model dictates. Naive Retry's marginal edge comes from attempting more interventions (240) than RecoveryOS (159), not from choosing better ones; RecoveryOS sits slightly lower because the policy gate correctly refuses to act on fraud and terminal events.

**What the benchmark does establish:** harness integrity — fairness over a shared event set and shared hidden model, order-invariant determinism, strict isolation of ground truth from the decision path, honest accounting (`processed + skipped + exceptions == event_count`), and a demonstrably non-rigged comparison that reports RecoveryOS losing.

**What the benchmark cannot establish:** that RecoveryOS's targeting recovers more revenue than a naive blanket retry. Demonstrating that would require a signal-bearing outcome model in which intervention efficacy genuinely depends on event characteristics. Constructing one would mean inventing the very correlations the system claims to exploit, so it has deliberately not been done, and the methodology and calculations above are preserved unchanged rather than tuned to produce a more flattering presentation.

## Metrics

All metrics are pure functions over the collected per-event records (`app/benchmark_metrics.py`):

- **A. Simulated recovered revenue** — total `recovered_amount_paise` recovered by a strategy over the shared event set (integer paise, labeled simulated).
- **B. Recovery rate** — `recovered_events / event_count`. **Denominator defined by the Phase 9 methodology:** every strategy evaluates the same shared event set and each event yields exactly one simulated outcome (an attempted intervention or the modeled `no_action` baseline), so the denominator is the strategy's shared event count.
- **C. Intervention count** — total interventions a strategy attempted (No Action is always 0).
- **D. Recovery efficiency** — `recovered_amount_paise / interventions_attempted`, or `None` when zero interventions were attempted (never a division by zero).
- **E. Incremental over No Action** — RecoveryOS recovered revenue minus No Action recovered revenue (paise).
- **F. RecoveryOS vs Naive Retry** — RecoveryOS recovered revenue minus Naive Retry recovered revenue (paise).
- **G. Fraud intervention rate** — interventions on `fraud_suspect` events divided by fraud events; RecoveryOS's desired value is 0, measured from records and never hardcoded. `None` when there are no fraud events.
- **H. False-intervention rate — METRIC DEFINITION AMBIGUITY** — NOT computed: the repository defines **no canonical false-intervention threshold**, so a threshold would be invented rather than measured. The raw per-event attempted/recovered foundation any such metric would need is carried on every `BenchmarkEventResult`.

## Reporting Rule

Any revenue figure produced by the benchmark **must be labeled as simulated evaluation results**, never presented as production Razorpay revenue. Accounting on every run: `processed + skipped + exceptions == event_count`, and skipped, failed, and exception outcomes are always distinguished.

## Run Reproducibility

The canonical run is `python -m app.benchmark --seed 42 --count 500` (from `backend/`), which computes the run and prints the summary. `python -m app.benchmark_store --seed 42 --count 500` performs the same canonical run and additionally persists the summary to `benchmark_runs` so the dashboard has a comparison to read; both use identical seed, count, and methodology. Any fixed `--seed` and `--count` reproduce the identical run summary and per-event records. The default classifier is the project-owned, deterministic `DeterministicClassifier` (advisory, decision-time inputs only); any adapter satisfying the classifier Protocol may be injected instead, but such a run is model-dependent and explicitly NOT reproducible.

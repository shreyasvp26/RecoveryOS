# RecoveryOS Benchmark Methodology (Intended)

> **Status note:** This document describes the **intended** benchmark methodology. Phase 8 has implemented the **foundation** it depends on — a deterministic, hidden, event-specific outcome model (`app/outcome_model.py`) and a deterministic recovery simulation (`app/outcome.py`), both strictly isolated from the decision path. The benchmark harness itself (strategies, metrics, reporting) is still not implemented.

## Purpose

The benchmark measures whether RecoveryOS recovers more *simulated* revenue than baseline approaches under identical conditions. It proves value by comparison, not by claiming absolute revenue.

## Baselines

The benchmark compares RecoveryOS against two baselines over the **same event set**:

- **No Action** — the control: nothing is attempted. Establishes the natural (zero-intervention) outcome.
- **Naive Retry** — a simple, aggressive retry strategy with no reasoning or policy.
- **RecoveryOS** — the full AI-recommend / policy-decide pipeline.

Because all three run against the **same event set**, differences in outcome are attributable to the strategy, not the data.

## Outcome Model

Outcomes are produced by a **hidden intervention-specific outcome model** (Phase 8 foundation):

- Unit of evaluation: a chosen intervention on a specific event.
- The outcome model is **deterministic**: a per-event `random.Random(f"{seed}:{event_id}")` draws each intervention probability from an explicit integer seed; the same seed and event set always produce the identical model, and a different seed produces a different one.
- It is **event-specific**: every event receives its own probability for every locked intervention (including `no_action`), correlated with the event identity — never a single global probability per intervention.
- It is **hidden**: the model and its probabilities are evaluation-owned. The classifier, policy gate, selector, executor, and Razorpay boundary never receive, see, or act on it.
- Every probability satisfies `0 <= p <= 1`; an invalid or missing value fails explicitly (never clamped, never defaulted).

## Deterministic Seed

A **deterministic seed** is fixed so that benchmark runs are reproducible: the same event set and seed always produce the same outcomes. Phase 8 draws the outcome for any (seed, event, intervention) triple from its own `random.Random(f"{seed}:{event_id}:{intervention}")`, so the result is also **independent of evaluation order, strategy order, and prior simulations**.

## Simulated Outcomes

All outcomes and all **recovery amounts are simulated** (Phase 8 foundation):

- Each executed intervention is evaluated by a single Bernoulli draw against its hidden per-event probability; `recovered_amount_paise` is derived from the event's amount when recovered, else zero.
- They are **not** produced by real Razorpay transactions.
- They are labeled as **simulated** in all results and reporting.
- **Execution success ≠ recovery success:** an intervention that executed successfully can still simulate a non-recovery (and vice versa); recovery is decided only at the evaluation boundary.

## No Forced Positive Result

The benchmark does **not force positive outcomes**. There is no guarantee that RecoveryOS (or any strategy) succeeds. Honest, representative outcomes are expected, including failures and zero-recovery results where appropriate.

## Reporting Rule

Any revenue figure produced by the benchmark **must be labeled as simulated evaluation results**, never presented as production Razorpay revenue.

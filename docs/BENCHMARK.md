# RecoveryOS Benchmark Methodology (Intended)

> **Status note:** This document describes the **intended** benchmark methodology. No benchmark logic, data, or hidden ground truth exist in Phase 1.

## Purpose

The benchmark measures whether RecoveryOS recovers more *simulated* revenue than baseline approaches under identical conditions. It proves value by comparison, not by claiming absolute revenue.

## Baselines

The benchmark compares RecoveryOS against two baselines over the **same event set**:

- **No Action** — the control: nothing is attempted. Establishes the natural (zero-intervention) outcome.
- **Naive Retry** — a simple, aggressive retry strategy with no reasoning or policy.
- **RecoveryOS** — the full AI-recommend / policy-decide pipeline.

Because all three run against the **same event set**, differences in outcome are attributable to the strategy, not the data.

## Outcome Model

Outcomes are produced by a **hidden intervention-specific outcome model**:

- Unit of evaluation: a chosen intervention on a specific event.
- The outcome model is **deterministic**: given a fixed seed and identical inputs, it always returns the same result.
- It is **intervention-specific**: different interventions may yield different outcome characteristics.
- It is **hidden**: the model and its parameters are not exposed to the strategy being evaluated, preventing the optimizer from "gaming" the result.

## Deterministic Seed

A **deterministic seed** is fixed so that benchmark runs are reproducible: the same event set and seed always produce the same outcomes.

## Simulated Outcomes

All outcomes and all **recovery amounts are simulated**:

- They are computed by the deterministic harness over the event set.
- They are **not** produced by real Razorpay transactions.
- They are labeled as **simulated** in all results and reporting.

## No Forced Positive Result

The benchmark does **not force positive outcomes**. There is no guarantee that RecoveryOS (or any strategy) succeeds. Honest, representative outcomes are expected, including failures and zero-recovery results where appropriate.

## Reporting Rule

Any revenue figure produced by the benchmark **must be labeled as simulated evaluation results**, never presented as production Razorpay revenue.

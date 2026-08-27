# RecoveryOS Architecture

## What RecoveryOS Is

RecoveryOS is an AI Revenue Recovery Control Plane for the Razorpay AI Buildathon 2026 (Revenue Recovery track). It is designed to recover failed, declined, or abandoned payments in a way that is **safe, auditable, and measurable**.

## Core Principle

> **AI recommends. Deterministic policy decides. Executor acts. Benchmark proves value.**

The most important design constraint is safety of authority:

- **AI is advisory.** The LLM provides reasoning and recommendations only.
- **Deterministic policy is authoritative.** A rule-based, deterministic policy gate is the final arbiter over any money-moving decision.
- **The LLM can never directly execute a money-moving action.** There is no code path through which model output alone can authorize a charge or refund.
- An **executor** performs the decided action.
- A **benchmark** proves the value of the system against controlled baselines.

## Locked V1 Architecture

```
Razorpay Test Mode
  → Event Ingestion
  → Event Context + Customer History
  → AI Reasoning
  → Deterministic Policy Gate
  → Intervention Selection
  → Real Razorpay Test Action OR Controlled Simulation
  → Outcome Engine
  → Append-only Audit Trail
  → Benchmark
  → Dashboard
```

Flow explanation:

1. **Event Ingestion** — inbound payment events arrive (from Razorpay Test Mode).
2. **Event Context + Customer History** — enrichment with surrounding context.
3. **AI Reasoning** — the LLM analyzes and recommends candidate interventions.
4. **Deterministic Policy Gate** — authoritative rules decide whether, and which, intervention proceeds. The LLM cannot bypass this gate.
5. **Intervention Selection** — the specific intervention is chosen.
6. **Real Razorpay Test Action OR Controlled Simulation** — the intervention is either executed against Razorpay Test Mode, or evaluated in a controlled simulation.
7. **Outcome Engine** — outcomes are tracked.
8. **Append-only Audit Trail** — every recommendation, decision, and action is recorded immutably.
9. **Benchmark** — results are measured against baselines.
10. **Dashboard** — results are surfaced for humans.

## REAL_RAZORPAY vs SIMULATED

RecoveryOS operates in two distinct modes:

- **REAL_RAZORPAY** — the intervention executes against Razorpay **Test Mode**. This is a controlled, non-production environment used to exercise the real integration.
- **SIMULATED** — the intervention is evaluated through a controlled simulation with a deterministic outcome model, used to measure RecoveryOS behavior at scale without touching any payment rails.

The two modes are kept clearly distinct so that results are never conflated.

## Benchmark Claims Are Simulated

All benchmark **recovery amounts are simulated evaluation results** — they are produced by the deterministic test harness, not by real Razorpay transactions. **RecoveryOS does not claim these as production Razorpay revenue.** They exist only to measure relative effectiveness of RecoveryOS against baselines under identical conditions.

## Current Implementation Status (Phase 4)

**Phase 4 is event generation and ingestion.** This repository currently contains:

- A scaffolded FastAPI backend exposing a deterministic health endpoint smoke test.
- The locked Phase 2 `PaymentEvent` domain contract and validation.
- The Phase 3 SQLite persistence layer (`payment_events` table).
- A deterministic, seedable synthetic `PaymentEvent` generator (`app/generator.py`). The same seed and parameters reproduce the same dataset. Generated events contain decision-time information only; benchmark ground truth is intentionally absent.
- A thin event ingestion boundary (`app/ingestion.py`) that validates every event through the `PaymentEvent` contract, persists via the Phase 3 layer, rejects duplicates deterministically, and surfaces persistence/ingestion failures explicitly.
- A minimal `POST /events` HTTP ingestion endpoint wired through the ingestion service.
- A scaffolded React + Vite frontend rendering a minimal RecoveryOS shell.

**The V1 pipeline described above is planned, not yet implemented.** None of the following exist yet: AI reasoning, the policy gate, intervention selection, executor, Razorpay integration, outcome engine, audit trail, benchmark, or dashboard. Future phases will build toward the locked V1 architecture.

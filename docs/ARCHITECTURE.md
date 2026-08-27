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

## Current Implementation Status (Phase 6)

**Phase 6 adds the deterministic policy safety gate.** This repository currently contains:

- A scaffolded FastAPI backend exposing a deterministic health endpoint smoke test.
- The locked Phase 2 `PaymentEvent` domain contract and validation.
- The Phase 3 SQLite persistence layer (`payment_events` table).
- Phase 4 deterministic, seedable synthetic `PaymentEvent` generation and a thin event ingestion boundary.
- The Phase 5 advisory classification contract (`app/classification.py`) with the locked root-cause and candidate-intervention taxonomies.
- A single configurable OmniRoute-backed classifier (`app/classifier.py`) that receives decision-time event information only, emits a structured JSON result, validates it strictly (at most one retry for malformed output), and fails explicitly on ML/provider errors.
- Phase 5 classification persistence (`classification_results` table, correlated with `payment_events` by `event_id`).
- A minimal `POST /events/{event_id}/classify` endpoint wiring load → classify → persist → return.
- The Phase 6 deterministic policy engine (`app/policy.py`): a pure, stateless Python gate that answers one question — *is this proposed intervention permitted?* It contains zero LLM calls, selects nothing, and executes nothing.
- Phase 6 policy persistence (`policy_decisions` table, preserving the decision contract) and minimal intervention history (`intervention_attempts` table, the future executor's record; Phase 6 writes no execution records).
- A `POST /events/{event_id}/policy` endpoint wiring load event → load classification → derive historical context → evaluate → persist → return.
- A scaffolded React + Vite frontend rendering a minimal RecoveryOS shell.

**The V1 pipeline described above is planned, not yet implemented.** None of the following exist yet: intervention selection, the executor, Razorpay integration, outcome engine, audit trail, benchmark, or dashboard. The AI classifier is advisory only; the policy engine authorizes only; nothing executes.

## The Policy Safety Gate (Phase 6)

The critical security property is:

```
LLM ─────X────→ Executor
```

The only valid future path is:

```
LLM
  ↓
Recommendation (advisory)
  ↓
Deterministic Policy — ALLOW / DENY
  ↓
Future Intervention Selection
  ↓
Future Executor
```

**Authority path:** the LLM recommends; the deterministic Python policy authorizes; the (future) executor acts. No `execute=true` ever originates from model output.

### The six locked rules

| # | Rule | Denial reason | Bound |
|---|------|---------------|-------|
| 1 | Fraud protection | `fraud_protection` | `risk_flag == fraud_suspect` always DENY |
| 2 | Max interventions per customer | `customer_intervention_limit_exceeded` | count of attempts in rolling 24h `>=` configured max (default 2) |
| 3 | Event cooldown | `event_cooldown_active` | elapsed since most-recent attempt on the event `<` 30 min |
| 4 | Configurable daily spend cap | `spend_cap_exceeded` | existing spend + proposed cost `>` cap (default ₹50,000; `==` ALLOWs) |
| 5 | Terminal failure block | `terminal_failure` | `root_cause_category == terminal` always DENY |
| 6 | Duplicate successful intervention | `duplicate_intervention` | same event already has a `successful` attempt |

The spend cap is **global** across all persisted intervention attempts within a rolling 24h window (a financial cost-control valve); per-customer spend is not modeled. Intervention costs are configured through `PolicyConfig.intervention_cost_paise` (all zero by default — the check still always runs). The daily/customer windows use actual datetime arithmetic relative to the explicit `evaluation_time`, never calendar-day counting and never string comparison.

### Deterministic evaluation order

Intervention validation is a fail-closed precondition (an unknown intervention raises `PolicyValidationError`; it is never evaluated as a candidate). The decision rules then run in this fixed order and the **first blocker determines the denial reason**:

```
1. Fraud protection
2. Terminal failure
3. Duplicate protection
4. Customer 24h intervention limit
5. Event cooldown
6. Spend cap
7. ALLOW
```

When allowed, `policy_rules_applied` lists the six passed checks in this order (e.g. `fraud_check_passed`, …, `spend_cap_passed`); when denied, it lists the single blocking rule. The same inputs always produce the same decision — evaluation is a pure function of (event, classification, history, proposed intervention, config, evaluation time).

### Fail-closed behavior

This is a financial safety boundary; it never fails open. Malformed input, an intervention outside the locked taxonomy, an event/classification `event_id` mismatch, a timezone-naive timestamp, and history that cannot be parsed are all surfaced as explicit controlled errors (`PolicyValidationError`). Database lookups that fail surface as explicit persistence errors. Policy never fabricates history, spend, or duplicate state.

### Policy vs selection boundary

Policy asks *"is this candidate permitted?"* It does not rank candidates, compute expected value, recovery probability, or choose the best intervention — that is the future selection phase. Multiple candidates are evaluated independently (each may be allowed or denied on its own merits).

### Time handling

All timestamps are timezone-aware ISO8601, normalized to UTC. `evaluation_time` is always supplied explicitly (the endpoint accepts it; when omitted it defaults to the server's current UTC time). Mixing naive/aware/local timestamps is prevented by fail-closed validation.

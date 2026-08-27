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
- **SIMULATED** — the operational step is recorded as executed without touching any payment rails. It carries **no recovery/outcome model**: Phase 7's simulated execution does not estimate, label, or predict whether any money would be recovered.

Neither mode produces a **revenue recovery outcome** in Phase 7. `SUCCESS` describes only whether the execution step itself ran. Whether revenue was actually recovered is answered later by the **outcome/benchmark layer**, which measures simulated evaluation results against baselines — not by selection or execution.

The two modes are kept clearly distinct so that results are never conflated.

## Benchmark Claims Are Simulated

All benchmark **recovery amounts are simulated evaluation results** — they are produced by the deterministic test harness, not by real Razorpay transactions. **RecoveryOS does not claim these as production Razorpay revenue.** They exist only to measure relative effectiveness of RecoveryOS against baselines under identical conditions.

## Current Implementation Status (Phase 7)

**Phase 7 adds deterministic intervention selection and bounded execution.** This repository currently contains:

- A scaffolded FastAPI backend exposing a deterministic health endpoint smoke test.
- The locked Phase 2 `PaymentEvent` domain contract and validation.
- The Phase 3 SQLite persistence layer (`payment_events` table).
- Phase 4 deterministic, seedable synthetic `PaymentEvent` generation and a thin event ingestion boundary.
- The Phase 5 advisory classification contract (`app/classification.py`) with the locked root-cause and candidate-intervention taxonomies.
- A single configurable OmniRoute-backed classifier (`app/classifier.py`) that receives decision-time event information only, emits a structured JSON result, validates it strictly (at most one retry for malformed output), and fails explicitly on ML/provider errors.
- Phase 5 classification persistence (`classification_results` table, correlated with `payment_events` by `event_id`).
- A minimal `POST /events/{event_id}/classify` endpoint wiring load → classify → persist → return.
- The Phase 6 deterministic policy engine (`app/policy.py`): a pure, stateless Python gate that answers one question — *is this proposed intervention permitted?* It contains zero LLM calls, selects nothing, and executes nothing.
- Phase 6 policy persistence (`policy_decisions` table, preserving the decision contract) and intervention history (`intervention_attempts` table, now written by the executor).
- A `POST /events/{event_id}/policy` endpoint wiring load event → load classification → derive historical context → evaluate → persist → return.
- The Phase 7 deterministic V1 selector (`app/selector.py`): picks exactly one intervention among candidates that have an authoritative ALLOW decision, using the locked priority `retry_delayed > payment_link > reminder > alternate_method_prompt > retry_immediate`; no LLM reasoning, no randomness, no economic optimization.
- The Phase 7 bounded executor (`app/executor.py`): independently requires `PolicyDecision.allowed == true`, rejects mismatched bindings, never executes `no_action`, never calls the LLM. Simulated interventions report `SIMULATED`/`SUCCESS`; `payment_link` executes through `REAL_RAZORPAY`.
- A Phase 7 isolated Razorpay client boundary (`app/razorpay_client.py`) wrapping the SDK (Test Mode only, credentials from the environment), returning genuine Payment Link references and explicit failures — never a fabricated URL.
- Phase 7 execution persistence (`execution_outcomes` table, correlated by `event_id`) and a `POST /events/{event_id}/execute` endpoint that accepts no client intervention or authorization — the chain classification → policy → selector → executor fully determines execution against server-side time.
- A scaffolded React + Vite frontend rendering a minimal RecoveryOS shell.

**The rest of the V1 pipeline is planned, not yet implemented.** None of the following exist yet: the outcome engine, the append-only audit dashboard, the benchmark harness, or the dashboard. Execution success is recorded only as an operation result; RecoveryOS claims no revenue.

## The Policy Safety Gate (Phase 6)

The critical security property is:

```
LLM ─────X────→ Executor
```

The only valid path is:

```
LLM
  ↓
Recommendation (advisory)
  ↓
Deterministic Policy — ALLOW / DENY
  ↓
V1 Intervention Selection (deterministic priority)
  ↓
Bounded Executor (requires authoritative ALLOW)
  ↓
ExecutionOutcome (SIMULATED | REAL_RAZORPAY)
```

**Authority path:** the LLM recommends; the deterministic Python policy authorizes; the V1 selector chooses among authorized candidates; the bounded executor acts. No `execute=true` ever originates from model output, and no client can supply an intervention or an `allowed` flag.

### The V1 selector (Phase 7)

The selector consumes the advisory candidates from the classifier and the authoritative per-candidate policy decisions, drops `no_action`, keeps only candidates whose decision is `allowed == true`, and applies the locked priority:

```
retry_delayed  >  payment_link  >  reminder  >  alternate_method_prompt  >  retry_immediate
```

When no actionable candidate is authorized, the explicit result is `no_action` — which is never executed and never simulated. The selector uses no LLM reasoning, no randomness, no recovery predictions, and no economic optimization.

### The bounded executor (Phase 7)

The executor's API is effectively `execute(event, intervention, policy_decision, razorpay_client)`. It is not a second policy engine:

- It **rejects** execution when `policy_decision.allowed` is not `true`, when the decision's `event_id`/`proposed_intervention` do not match, and for `no_action` or unknown interventions.
- Simulated interventions (`retry_immediate`, `retry_delayed`, `reminder`, `alternate_method_prompt`) report `execution_mode = SIMULATED` and `status = SUCCESS` for the operation itself.
- `payment_link` reports `execution_mode = REAL_RAZORPAY` and creates a genuine Payment Link through the isolated `razorpay_client` boundary (Razorpay Test Mode only). Provider/config failures produce explicit `FAILED` outcomes with detail; the URL is never fabricated.

Execution `SUCCESS` means only that the operational step ran. It is kept strictly separate from revenue recovery: there is **no outcome model** in Phase 7, simulated or otherwise — an execution outcome describes the operation, and whether revenue was recovered belongs to the later benchmark/outcome layer. No Phase 7 code and no Razorpay response is ever labeled as recovered revenue.

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

Policy asks *"is this candidate permitted?"* The V1 selector then asks *"which authorized candidate has the highest locked priority?"* Neither computes expected value, recovery probability, or cost/recovery ranking, and neither chooses the "best" intervention economically — that is a later rescue but is intentionally out of scope for V1.

### Time handling

All timestamps are timezone-aware ISO8601, normalized to UTC. The policy endpoint accepts an explicit `evaluation_time` (defaulting to the server's current UTC time); the execution endpoint always evaluates against server-side UTC time — a client can never choose a time that bypasses cooldown or customer limits. Mixing naive/aware/local timestamps is prevented by fail-closed validation.

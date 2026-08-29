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
  → Intervention Selection (V1 fixed priority; V2 economic optimizer since Phase 16)
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

## Current Implementation Status (Phase 14)

**Phase 8 delivered the evaluation boundary; Phase 9 the honest benchmark; Phase 10 the read-only Command Center & Decision Trace; Phase 11 real Razorpay Payment Link execution; Phase 12 the closed-loop verified webhook; Phase 13 adversarial policy/trace hardening; Phase 14 end-to-end live verification, evidence, and documentation of V1.** This repository currently contains:

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
- The Phase 8 evaluation boundary: a hidden, event-specific outcome model (`app/outcome_model.py`) and a deterministic recovery simulation (`app/outcome.py`), completely isolated from the decision path, with no persistence and no new endpoints.
- A React + Vite read-only operator frontend with three screens (Command Center, Event Decision Trace, Policy & Blocks) that render persisted backend state and never recompute policy or benchmark metrics.

The V1 pipeline is fully implemented through Phase 12: the Phase 9 benchmark runs on top of the Phase 8 evaluation foundation (hidden model + simulation); Phase 10 added the read-only Recovery Command Center & Decision Trace over persisted state; Phase 11 added real Razorpay Test Mode Payment Link execution; and Phase 12 added the closed-loop verified webhook outcome channel (documented below). The V2 optimizer does not exist yet. The benchmark measures **No Action**, **Naive Retry**, and the real **RecoveryOS** pipeline over ONE shared event set and ONE shared hidden outcome model on simulated, labeled recovery outcome amounts, reporting honest results with no forced RecoveryOS victory. The outcome engine answers *did an executed intervention recover the money?* It never decides whether anything executes, and RecoveryOS claims no real revenue.

Phase 14 verified this pipeline end to end against real Razorpay **Test Mode** infrastructure: a real classification, a deterministic authorization and selection, a real Test Mode Payment Link, a real manual payment, and a genuine `payment_link.paid` webhook that was HMAC-verified, correlated to the persisted link, and recorded as a single trusted recovery. See the README for the evidence and the honest limitations. **Test Mode is not production payment processing, and no production readiness is claimed.** Two limitations are load-bearing for interpreting the demo: the external LLM classification can vary between equivalent events even at `temperature = 0` (the deterministic policy and selector are reproducible only *given* a classifier output), and the **Phase 9** benchmark's hidden outcome model carries no signal, so it cannot demonstrate the value of targeting. Phase 17 addresses the second limitation with a separate signal-bearing benchmark; see below and `docs/BENCHMARK.md`.

## The Phase 12 closed-loop webhook (outcome channel)

Phase 12 adds a **verified, durable, audit-friendly OUTCOME channel** that turns a real Razorpay `payment_link.paid` webhook into a recovery outcome — it is architecturally separate from the execution channel and must never invoke the executor, selector, policy engine, classifier, or Payment Link creation.

- **Signature boundary** — `POST /webhook/razorpay` recomputes HMAC-SHA256 over the exact raw request body and compares constant-time (`hmac.compare_digest`). A bad or missing `X-Razorpay-Signature` is a 4xx before any parsing or side effect (fail-closed). The secret (`RAZORPAY_WEBHOOK_SECRET`) is never committed and never stored in SQLite.
- **Durable idempotency** — `X-Razorpay-Event-Id` is the SQLite PRIMARY KEY of `webhook_deliveries`, along with the SHA-256 of the raw body. Same id + same body = 2xx `deduplicated` no-op; same id + different body = 409 `conflict` (never overwritten, never a second recovery); persistent failure = 500 so Razorpay retries.
- **Crash-safe claim/retry** — a delivery still `claimed` is an in-flight attempt that crashed before completing; a re-delivered same id/body is reprocessed to completion, never dropped as a duplicate. The recovery write is `INSERT OR IGNORE` (idempotent), so a crash between that write and the terminal status update completes cleanly with no double-count.
- **Strict, event-specific validation** — a `payment_link.paid` event must carry a Payment Link id, report `status: paid`, and give a non-negative integer `amount_paid`; a malformed paid event is a 400, never silently unmatched. Unsupported events are recorded-and-ignored (2xx), never executed.
- **Trusted correlation** — matches the persisted Phase 11 `execution_outcomes.payment_link_id` (REAL_RAZORPAY + SUCCESS + payment_link), never amount/customer/email. The trusted recovery amount is the `amount_paid` observed on the link, never the original event amount. Verified outcomes persist to `webhook_recovery_outcomes` (delivery-id PRIMARY KEY) and the dashboard trace labels each real link `waiting` → `recovered`.

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
  ↓
Evaluation Boundary — Hidden Outcome Model + Deterministic Simulation
  ↓
RecoveryOutcome (did the money come back?)
```

**Authority path:** the LLM recommends; the deterministic Python policy authorizes; the V1 selector chooses among authorized candidates; the bounded executor acts. No `execute=true` ever originates from model output, and no client can supply an intervention or an `allowed` flag.

### The V1 selector (Phase 7)

The selector consumes the advisory candidates from the classifier and the authoritative per-candidate policy decisions, drops `no_action`, keeps only candidates whose decision is `allowed == true`, and applies the locked priority:

```
retry_delayed  >  payment_link  >  reminder  >  alternate_method_prompt  >  retry_immediate
```

When no actionable candidate is authorized, the explicit result is `no_action` — which is never executed and never simulated. The selector uses no LLM reasoning, no randomness, no recovery predictions, and no economic optimization.

### The V2 economic optimizer (Phase 16)

Phase 16 replaced fixed-priority selection **in production** with expected-value selection. `app/selector.py` is unchanged and still used, both as the pinned benchmark strategy and as the V2 tie-breaker.

```
Allowed Candidates  →  Recovery Probability Estimator  →  Economic Scoring  →  Best Candidate
```

The policy gate still runs first and remains authoritative. `EconomicInterventionOptimizer.select` accepts only an `AllowedCandidates` value, which carries the authoritative `PolicyDecision` objects and re-validates them on construction: an entry is authorized only if a real ALLOW names that exact intervention for that exact event. A policy-denied candidate is therefore **structurally** unable to reach economic evaluation, however valuable it looks, on every construction path rather than only via the convenience constructor. The optimizer reads only `decision.allowed`; it never sees a denial reason and never re-implements a policy rule.

For each allowed candidate, using integer paise and integer basis points throughout:

```
expected_value = probability × amount − intervention_cost − friction_cost
```

Selection is `argmax(expected_value)`, with the V1 priority ordering used only to break exact ties and the intervention name as a final stable term. The result is invariant under candidate reordering, and `no_action` semantics are preserved unchanged.

The optimizer selects; the existing bounded executor still executes. It performs no execution, no persistence, no LLM call, no network access, and has no benchmark or hidden-ground-truth dependency.

Full specification, coefficients, assumptions, and limitations: [ECONOMIC_MODEL.md](ECONOMIC_MODEL.md). Note that V2 probabilities, costs, and friction are RecoveryOS controlled evaluation assumptions, and **Phase 16 does not claim improved recovery performance**. Phase 17 supplies the experiment that can test that claim.

### The Phase 17 signal-bearing benchmark

Phase 16 built the decision engine; Phase 17 built the environment capable of honestly deciding whether it helps. It is a **second, separate** benchmark — the Phase 9 harness and its hidden model (`app/outcome_model.py`) are frozen and unchanged.

```
FROZEN 500 EVENTS  →  HIDDEN WORLD (app/hidden_world.py)
                              │  hidden from the SUT
   ┌──────────┬───────────────┼───────────────┬──────────┐
No Action   Naive Retry   RecoveryOS V1   RecoveryOS V2   Oracle
                               └──── policy gate ────┘   (eval only)
                                          │
                              simulated execution (never Razorpay)
                                          │
                               outcome realization  →  evaluation layer
```

Four things distinguish it from Phase 9:

- **The hidden world carries causal signal.** `P_true` is a function of `failure_reason`, `payment_method`, `customer_history`, subscription status and amount band — never of `event_id`. Different failure classes genuinely reward different interventions, so targeting can be rewarded or punished. It is independently authored from the V2 estimator and provably disagrees with it about intervention rankings.
- **Every intervention is simulatable offline.** `app/benchmark_simulation.py` runs `payment_link` as a `SIMULATED` execution with no credential and no network call, so the benchmark is no longer pinned to V1. The production executor still couples `payment_link` to `REAL_RAZORPAY`, unchanged.
- **There is an evaluation-only Oracle**, giving regret and value-capture metrics rather than only a revenue comparison.
- **Each event is an independent decision problem**, which makes the run invariant to strategy order and event order — both verified on every run.

Ground truth remains structurally unreachable from the classifier, policy, selector, estimator, optimizer, executor and dashboard. Methodology, frozen coefficients, metric formulas, the canonical result and the limitations: [BENCHMARK.md](BENCHMARK.md).

### The Phase 18 economic decision audit trail

Phase 16 built the economic decision and Phase 17 built the experiment that tests it, but the decision itself existed only in memory: an operator could see that `retry_delayed` ran and could see that policy permitted it, and could not see *why economics chose it over the four other permitted options*. Phase 18 closes that gap. It adds **no** optimizer logic, changes no coefficient, and changes no benchmark methodology.

```
AI Diagnosis  →  Policy  →  Economic Optimization  →  Execution  →  Outcome
                                     │
                          optimizer_decisions (append-only)
```

`app/optimizer_audit.py` defines the narrow `OptimizerDecisionRecord` contract, and `db.optimizer_decisions` stores it. A record carries the candidate set considered, the policy-approved subset, the per-candidate estimated economics, the selected intervention and the selection reason — copied verbatim from the `OptimizerDecision` the optimizer produced. The audit layer performs **no arithmetic at all** (asserted by an AST test), so `app/economics.py` remains the single implementation of the expected-value equation.

Three properties make it defensible:

- **Audit before action.** `execute_event` persists the decision *before* invoking the executor, so a failed, unconfigured or rejected execution still leaves evidence of what RecoveryOS decided. A failure to write the audit record stops the flow rather than proceeding into an unaudited action.
- **The record cannot describe an illegal decision.** Construction fails if an evaluated candidate is outside the policy-approved set, or if an approved candidate was never considered. A denied intervention cannot be selected, and cannot even appear as an evaluated candidate.
- **Determinism makes it idempotent.** Re-deciding the same event at the same evaluation time re-derives an identical record, which is reused; a *different* decision at the same timestamp is a contradiction and is raised.

The record is exposed additively on the existing `GET /events/{event_id}/trace` response as `optimizer_decisions`, and the Event Decision Trace renders an **Economic Optimization** stage between Policy and Execution showing, per candidate, the estimated recovery probability, estimated recovered amount, estimated intervention cost, modeled friction and estimated expected value, plus the selection and its rationale. Every figure is a persisted backend value labelled `MODEL ESTIMATE`; none is hardcoded, none is recomputed in the frontend, and no benchmark ground truth reaches the operator surface. The V1 arm records nothing, because no economic decision occurred — an absent stage is reported as absent rather than reconstructed.

### The bounded executor (Phase 7)

The executor's API is effectively `execute(event, intervention, policy_decision, razorpay_client)`. It is not a second policy engine:

- It **rejects** execution when `policy_decision.allowed` is not `true`, when the decision's `event_id`/`proposed_intervention` do not match, and for `no_action` or unknown interventions.
- Simulated interventions (`retry_immediate`, `retry_delayed`, `reminder`, `alternate_method_prompt`) report `execution_mode = SIMULATED` and `status = SUCCESS` for the operation itself.
- `payment_link` reports `execution_mode = REAL_RAZORPAY` and creates a genuine Payment Link through the isolated `razorpay_client` boundary (Razorpay Test Mode only). Provider/config failures produce explicit `FAILED` outcomes with detail; the URL is never fabricated.

Execution `SUCCESS` means only that the operational step ran. It is kept strictly separate from revenue recovery: there is **no outcome model** in Phase 7, simulated or otherwise — an execution outcome describes the operation, and whether revenue was recovered belongs to the later benchmark/outcome layer. No Phase 7 code and no Razorpay response is ever labeled as recovered revenue.

### The evaluation boundary (Phase 8)

Phase 8 adds the **evaluation-only** layer that answers *did an executed intervention actually recover the money?* It is the ONE place the System Under Test meets hidden ground truth. It follows the executed `ExecutionOutcome`, never precedes it.

```
event + intervention (already selected/executed)
   + HiddenOutcomeModel (seed, per-event per-intervention probabilities)
            ↓
OutcomeSimulator.simulate(event, intervention)
   → one deterministic Bernoulli draw
   → RecoveryOutcome(event_id, intervention, recovered, recovered_amount_paise)
```

- **Hidden outcome model (`app/outcome_model.py`)** — every synthetic event owns a recovery probability for **exactly the locked interventions**, including `no_action` (the natural zero-intervention baseline). Probabilities are drawn from a per-event `random.Random(f"{seed}:{event_id}")`, so they depend only on (seed, event identity). The same seed + event set always yields the identical model; a different seed yields a different one. Validation is explicit: `0 <= p <= 1` or an error (`InvalidOutcomeProbabilityError`), non-integer seed → `InvalidSeedError`, unknown event/intervention → `MissingGroundTruthError`. Nothing is clamped, defaulted, or guessed.
- **Deterministic simulation (`app/outcome.py`)** — recovery is a single Bernoulli draw `rng.random() < p` from a private `random.Random(f"{seed}:{event_id}:{intervention}")`. The result for a (seed, event, intervention) triple is identical regardless of evaluation order, strategy order, or prior simulations — the benchmark can evaluate competing strategies over the same hidden environment fairly. `RecoveryOutcome.recovered_amount_paise` derives from the event: `event.amount_paise` when recovered, else `0`. Hidden probabilities never appear in the record.
- **Ground-truth isolation** — hidden probabilities never flow into the classifier (input or prompt), the policy gate, the selector, the executor, the Razorpay boundary, any API response, or normal logs. Integrity tests enforce this at the source, behavior, and API levels. **Possible recovery ≠ authorized recovery:** a `fraud_suspect` event is still always DENYed by policy even when the hidden model assigns it a recovery probability of `1.0`; a terminal root cause is likewise blocked.
- **No persistence, no new endpoints** — the model and simulated outcomes are harness-side in-memory state, regenerated from a seed; nothing Phase 8 writes to the database and no `/benchmark` or `/ground-truth` endpoint exists. The manual DB-regeneration flow (delete → regenerate events → regenerate hidden model → simulate) needs no hand-written benchmark records.
- **Execution success ≠ recovery success** — the simulation is independent of execution. An intervention that executed with `SUCCESS` can simulate `recovered == false` (and vice versa); `no_action` is never executed, but the evaluation layer models its natural baseline. No Phase 8 logic changes anything about what the System Under Test decides or does.

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

Policy asks *"is this candidate permitted?"* Selection then asks *"which authorized candidate should we run?"* — answered by locked priority in V1, and by expected value in V2 (Phase 16). Policy itself never computes expected value, recovery probability, or cost ranking, and selection never authorizes: it can only ever narrow the set policy already approved.

### Time handling

All timestamps are timezone-aware ISO8601, normalized to UTC. The policy endpoint accepts an explicit `evaluation_time` (defaulting to the server's current UTC time); the execution endpoint always evaluates against server-side UTC time — a client can never choose a time that bypasses cooldown or customer limits. Mixing naive/aware/local timestamps is prevented by fail-closed validation.

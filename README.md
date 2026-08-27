# RecoveryOS

An **AI Revenue Recovery Control Plane** for the Razorpay AI Buildathon 2026 (Revenue Recovery track).

## Core Principle

> **AI recommends. Deterministic policy decides. Executor acts. Benchmark proves value.**

The LLM never has direct authority over a money-moving action. AI output is advisory; a deterministic policy gate is authoritative; an executor performs the action; a benchmark proves value against baselines.

> **Important:** This repository is in **Phase 9 — Honest Three-Strategy Benchmark**. RecoveryOS performs **no production revenue recovery**: it can select one intervention deterministically and run it either as an explicit simulation or as a real **Razorpay Test Mode** Payment Link, and the benchmark now proves value by comparison — over ONE shared 500-event synthetic set and ONE shared hidden outcome model, it measures **No Action**, **Naive Retry**, and the real **RecoveryOS** pipeline (classifier → policy → selector → executor) on simulated, labeled recovery outcome amounts. The audit dashboard and V2 optimizer remain future work.

## Locked Architecture

```
Razorpay Test Mode
  → Event Ingestion
  → Event Context + Customer History
  → AI Reasoning
  → Deterministic Policy Gate
  → Intervention Selection
  → Real Razorpay Test Action OR Controlled Simulation
  → Evaluation Boundary (Hidden Outcome Model + Deterministic Simulation)
  → Outcome Engine
  → Append-only Audit Trail
  → Benchmark
  → Dashboard
```

See `docs/ARCHITECTURE.md` for the detailed design, including the distinction between `REAL_RAZORPAY` and `SIMULATED`, and the rule that benchmark recovery amounts are simulated evaluation results (not production Razorpay revenue). The Phase 8 evaluation boundary is described there too: deterministic, order-independent, hidden from the decision path.

## Repository Structure

```
recoveryos/
├── backend/       FastAPI application + pytest test suite
│   ├── app/       Application package (models, persistence, generator, ingestion, classifier, policy, selector, executor, razorpay client)
│   ├── tests/     pytest tests
│   ├── requirements.txt
│   └── .env.example
├── frontend/      React + Vite application
├── docs/          ARCHITECTURE.md, BENCHMARK.md, PITCH_NOTES.md
├── README.md
└── .gitignore
```

## Local Setup

Prerequisites: Python 3.11, Node.js, npm.

### Backend

```bash
cd backend
python3.11 -m venv .venv
source .venv/bin/activate       # Windows: .venv\Scripts\activate
pip install -r requirements.txt

# Copy the environment template (edit as needed, never commit .env)
cp .env.example .env
```

### Backend Tests

```bash
cd backend
python -m pytest            # or: python -m pytest tests/ -v
```

### Frontend

```bash
cd frontend
npm install
npm run dev                 # Vite dev server
```

Open the printed local URL to view the RecoveryOS shell page.

## Run the Backend

```bash
cd backend
uvicorn app.main:app --reload
```

Health check: `http://127.0.0.1:8000/health` returns `{"status": "ok"}`.

## Current Development Phase

**Phase 9 — Honest Three-Strategy Benchmark.** A payment event flows end-to-end through generation → ingestion → SQLite → load → AI classification → SQLite → deterministic policy evaluation (every actionable candidate) → selection → **bounded execution** → persisted outcome, exactly as in Phases 7–8. On top of that, Phase 9 delivers the benchmark harness that measures RecoveryOS against two baselines over the **same** event set and the **same** hidden outcome model, using simulated, labeled recovery outcome amounts — honestly, with no forced positive result.

### Phase 9 — Honest Three-Strategy Benchmark

```bash
cd backend
python -m pytest                            # full suite (Phase 9 benchmark tests included)

# Run the canonical benchmark (500 events, seed 42) and print the summary
python -m app.benchmark --seed 42 --count 500
```

- **Three strategies over ONE shared event set and ONE shared hidden outcome model** (`app/benchmark.py`) — `no_action` (the control, attempts nothing and values every event at its modeled natural baseline), `naive_retry` (`retry_immediate` on every eligible non-fraud event, no AI/policy/selector, so its retries are modeled directly by the simulator — never a fabricated authorization), and `recovery_os` (the REAL pipeline: advisory classification → deterministic policy gate → selection → bounded execution through the existing SQLite schema, with recovery simulated only after execution was decided). No `benchmark_recoveryos.py` reimplementation and no `benchmark_mode` branches inside the frozen modules.
- **Fair, deterministic outcomes** — recovery for (seed, event, intervention) is drawn from the Phase 8 per-triple `random.Random(f"{seed}:{event_id}:{intervention}")`, so the outcome is identical regardless of strategy order and prior simulations. Faithful strategies can never influence each other's draws.
- **Ground-truth isolation, enforced** — hidden recovery probabilities are consulted only to simulate the outcome of an already-selected intervention. They never enter classification, policy, selection, or execution; the controlled `DeterministicClassifier` reads decision-time event fields only.
- **Spelled-out rules with no mandate to win** — fraud events are never executed by RecoveryOS (fraud interventions are measured, target 0, never hardcoded) and terminal events are never executed either; exceptions are visible and are never double-counted as not-recovered or failed; skipped is distinguished from failed from exception; `processed + skipped + exceptions == event_count` on every run. The benchmark can — and on the canonical seed does — report that RecoveryOS recovers less simulated revenue than a baseline; that honest result is reported, not suppressed.
- **Metrics** (`app/benchmark_metrics.py`) — simulated recovered revenue (integer paise, labeled simulated), recovery rate (denominator = shared event count), intervention count (No Action = 0), recovery efficiency (recovered paise per intervention, `None` when zero interventions — never a division by zero), incremental revenue over No Action, and RecoveryOS vs Naive Retry. **False-intervention rate is reported as METRIC DEFINITION AMBIGUITY** — the repository defines no canonical false-intervention threshold, so a threshold would be invented rather than measured; the raw per-event attempted/recovered foundation it would need is carried on every record.
- **No new DB schema, no new endpoints, no real Razorpay calls** — RecoveryOS runs through the existing frozen modules against an in-memory SQLite database (a stub/absent client means no provider is ever contacted), and the benchmark exposes only the CLI. All results are simulated evaluation results, never production Razorpay revenue.

### Phase 8 — Deterministic Outcome Simulation + Honest Benchmark (Foundation)

```bash
cd backend
python -m pytest                            # full suite (Phase 8 integrity tests included)

# The evaluation harness regenerates events and the hidden model from a seed,
# then simulates each intervention's outcome — fully in memory, no records needed
python -c "
from app.generator import generate_events
from app.outcome_model import generate_hidden_outcome_model
from app.outcome import OutcomeSimulator
events = generate_events(seed=42, count=10)
model = generate_hidden_outcome_model(events, 42)
for event in events[:2]:
    print(OutcomeSimulator(model).simulate(event, 'retry_delayed').to_dict())
"
```

- **Hidden outcome model (`app/outcome_model.py`)** — each synthetic event gets its own recovery probability for **exactly the locked interventions** (including `no_action`, the natural baseline). The model is evaluation-owned: the classifier, policy gate, selector, executor, and Razorpay boundary never receive or refer to it.
- **Event-specific and deterministic** — probabilities are drawn from a per-event `random.Random(f"{seed}:{event_id}")`. A probability depends only on (seed, event identity), not on event-set size, evaluation order, or any shared RNG. No `random.seed()`, no module-global mutable RNG, explicit integer seed only.
- **Explicit bounds, never clamped** — every probability must satisfy `0 <= p <= 1`; anything else (including a missing event/intervention) fails with an explicit error (`InvalidSeedError`, `InvalidOutcomeProbabilityError`, `MissingGroundTruthError`). No guessed defaults, no `except: pass`.
- **Deterministic recovery simulation (`app/outcome.py`)** — one Bernoulli draw per (event, intervention) from a private `random.Random(f"{seed}:{event_id}:{intervention}")`; the outcome for a triple is identical no matter how many simulations happen before it or in what order. `RecoveryOutcome` is minimal: `event_id`, `intervention`, `recovered`, `recovered_amount_paise` (derived: `event.amount_paise` when recovered, else 0). Hidden probabilities never appear in the record.
- **Execution success ≠ recovery success** — an executed intervention can have `ExecutionOutcome.status == "SUCCESS"` and yet simulate `recovered == false` (and vice versa). Recovery is decided only at the evaluation boundary, independently of execution.
- **Hard isolation** — hidden ground truth never reaches the LLM input/prompt, policy decisions, selection, execution, API responses, or normal logs (enforced by source-level, behavioral, and API-level integrity tests). **Possible recovery ≠ authorized recovery**: a `fraud_suspect` event stays denied even when the hidden model says recovery is certain.
- **No persistence, no new endpoints** — the model and outcomes are harness-side state, regenerated deterministically; nothing is written to the database and no `/benchmark` or `/ground-truth` API exists. Deleting the DB and re-running the pipeline requires no manual benchmark records.

### Phase 4 — Spinning Up Development Data

```bash
cd backend
python -m pytest                              # full suite
python -m app.generator --seed 42 --count 10  # deterministic dev dataset (JSON)
```

- **What an ingested PaymentEvent represents** — a single failed, declined, or abandoned payment attempt from the locked Phase 2 domain contract, validated on entry and stored as one row in `payment_events`. Ingestion never alters, deletes, or silently overwrites an existing event.
- **How synthetic events are generated** — `app/generator.py` draws from the locked domain value sets (payment methods, risk flags), realistic INR amounts in paise, deterministic ISO8601 timestamps, and unique `evt_`/`order_`/`pay_` identifiers. Customer IDs may repeat across events for the same synthetic customer.
- **Seeded generation is deterministic** — the generator uses `random.Random(seed)`, never the global random source. The same seed and generation parameters reproduce the exact same dataset (and the same identifiers), on any machine.
- **Decision-time information only** — generated events contain only information available at decision time. No recovery probability, true outcome, benchmark score, simulated revenue, or any other future-knowledge field exists. **Benchmark ground truth is intentionally absent.**

### Phase 5 — Advisory AI Classification

```bash
cd backend
source .venv/bin/activate
# configure the model layer, then run the API
export OMNIROUTE_API_KEY=...                    # never commit this
export OMNIROUTE_MODEL=...                      # see .env.example
uvicorn app.main:app

# classify an ingested event (advisory output, no execution)
curl -X POST http://127.0.0.1:8000/events/<event_id>/classify
```

- **AI is advisory** — the classifier (via OmniRoute) diagnoses the likely root cause and recommends candidate interventions only. It cannot authorize, select, or execute an action, and it never calls the payment provider.
- **Structured classification contract** — every model response is validated against the locked Phase 5 contract: `root_cause_category` must be one of `transient`, `customer_action_needed`, `fraud_suspect`, `terminal`; `confidence` must be between 0 and 1; every `candidate_interventions` entry must be one of `retry_immediate`, `retry_delayed`, `payment_link`, `reminder`, `alternate_method_prompt`, `no_action`. Malformed or invalid output triggers at most one retry, then an explicit failure — never a fabricated classification.
- **Decision-time information only** — the model receives only the locked PaymentEvent fields. Benchmark ground truth, true outcomes, and recovery probabilities are structurally absent.
- **Policy remains authoritative** — the deterministic policy gate (Phase 6) decides whether a proposed intervention is permitted. This phase ends at classification and persistence.

### Phase 6 — Deterministic Policy Safety Gate

```bash
cd backend
source .venv/bin/activate
uvicorn app.main:app

# evaluate one proposed intervention (no execution, no provider calls)
curl -X POST http://127.0.0.1:8000/events/<event_id>/policy \
  -H "Content-Type: application/json" \
  -d '{"proposed_intervention": "retry_delayed", "evaluation_time": "2026-08-27T13:00:00+00:00"}'
```

- **Authority path** — the LLM only recommends; the deterministic policy engine authorizes (ALLOW/DENY); the future executor acts. There is never an `execute=true` supplied by the LLM.
- **Six locked rules, evaluated in a fixed order** — (1) fraud protection (`fraud_suspect` events are always denied), (2) max 2 interventions per customer per rolling 24h, (3) 30-minute event cooldown, (4) configurable daily spend cap (rolling 24h, global), (5) terminal failure block, (6) duplicate successful-intervention protection. The first blocker determines the denial reason; the same inputs always produce the same decision.
- **Fail-closed by construction** — malformed input, unknown interventions, timezone-naive timestamps, and history lookups that cannot be determined safely produce explicit controlled errors; policy never fabricates history, spend, or duplicates and never fails open.
- **Historical facts come from persisted state** — the customer 24h count, most-recent event intervention, successful-duplicate flag, and daily spend are computed by the persistence boundary (`intervention_attempts`) with actual datetime arithmetic against the explicit evaluation timestamp, never from the LLM and never from shadow in-memory state.
- **Persistence** — every evaluation is persisted to `policy_decisions` (correlated by `event_id`, preserving the decision contract). No execution exists: nothing in Phase 6 records a successful intervention.

### Phase 7 — Deterministic Selection + Bounded Execution

```bash
cd backend
source .venv/bin/activate
uvicorn app.main:app

# select + execute the authorized intervention for an event (server decides everything)
curl -X POST http://127.0.0.1:8000/events/<event_id>/execute     # no body, no client authorization
```

- **Never an LLM → executor path** — the LLM only proposes candidates. The deterministic policy gate independently authorizes each candidate; the selector picks exactly one among those with `allowed == true`; the executor independently requires an authoritative ALLOW decision before acting.
- **Locked V1 priority** — `retry_delayed > payment_link > reminder > alternate_method_prompt > retry_immediate`. When every actionable candidate is denied (or none exists), the explicit result is `no_action` — which is never executed and never simulated.
- **Deterministic selection** — the selector (Phase 7) uses no LLM reasoning, no randomness, no recovery predictions, and no economic optimization; it only intersects candidates with authoritative ALLOW decisions and applies the fixed priority.
- **Bounded executor** — the executor author ≠ policy engine: it rejects any `PolicyDecision.allowed == false`, rejects mismatched event/intervention bindings, refuses `no_action`, never calls the LLM, and never decides recoverability.
- **Explicit execution modes** — `retry_immediate`, `retry_delayed`, `reminder`, and `alternate_method_prompt` run as `SIMULATED` (the operation is recorded as executed; there is **no outcome/recovery model**, simulated or real); `payment_link` runs as `REAL_RAZORPAY` (Razorpay **Test Mode** only — `rzp_live_` keys are rejected at the client boundary, via the isolated `razorpay_client` boundary). Provider failures and configuration gaps produce explicit `FAILED` outcomes — never fabricated success, never a guessed Payment Link URL.
- **No client bypass** — `POST /events/{event_id}/execute` accepts no intervention and no `allowed` flag; the authoritative chain (classification → policy → selection → executor) fully determines what executes, evaluated against server-side time.
- **Persistence** — outcomes are appended to `execution_outcomes` (correlated by `event_id`); each execution also records an `intervention_attempts` row so Phase 6 policy facts (customer limits, cooldown, spend, duplicates) stay derived from persisted state. Historical decisions and outcomes are never overwritten.
- **Execution success ≠ revenue recovery outcome** — `SUCCESS` means only that the operation itself ran. Phase 7 contains no outcome model: whether revenue was recovered is answered by the later benchmark/outcome layer, never by selection or execution.

What Phase 9 can and cannot do: it can measure RecoveryOS against No Action and Naive Retry over a shared event set and shared hidden model, report simulated metrics honestly, and note metric-definition ambiguity where the repo defines no canonical threshold. It cannot recover real revenue, rank interventions by expected value, or add a dashboard or V2 optimizer.

What Phase 8 can and cannot do: it can deterministically simulate whether an executed intervention recovered an event, and it keeps that ground truth strictly out of the decision path. It does not itself run the final benchmark strategies/metrics on that simulation (that is now Phase 9), cannot rank interventions by expected value, and does not add any dashboard or V2 optimizer.

What Phase 7 can and cannot do: it can select and execute (simulate or create a Test Mode Payment Link) a single authorized intervention per event. It cannot benchmark, cannot estimate recovery, cannot rank by expected value, and there is no audit dashboard or V2 optimizer.

What Phase 6 can and cannot do: it can evaluate and persist advisory policy decisions. It cannot select the best intervention, rank candidates, or execute anything. Selection, executor, Razorpay integration, benchmark, and dashboard remain planned for later phases.

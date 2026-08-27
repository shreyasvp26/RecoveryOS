# RecoveryOS

An **AI Revenue Recovery Control Plane** for the Razorpay AI Buildathon 2026 (Revenue Recovery track).

## Core Principle

> **AI recommends. Deterministic policy decides. Executor acts. Benchmark proves value.**

The LLM never has direct authority over a money-moving action. AI output is advisory; a deterministic policy gate is authoritative; an executor performs the action; a benchmark proves value against baselines.

> **Important:** This repository is in **Phase 7 — Intervention Selection + Bounded Execution**. RecoveryOS performs **no production revenue recovery**: it can select one intervention deterministically and run it either as an explicit simulation or as a real **Razorpay Test Mode** Payment Link. The recovery pipeline (outcome engine, audit dashboard, benchmark) is planned, not implemented.

## Locked Architecture

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

See `docs/ARCHITECTURE.md` for the detailed design, including the distinction between `REAL_RAZORPAY` and `SIMULATED`, and the rule that benchmark recovery amounts are simulated evaluation results (not production Razorpay revenue).

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

**Phase 7 — Intervention Selection + Bounded Execution.** A payment event now flows end-to-end through generation → ingestion → SQLite → load → AI classification → SQLite → deterministic policy evaluation (every actionable candidate) → selection → **bounded execution** → persisted outcome. The selection is a pure, deterministic priority rule; the executor requires authoritative policy authorization and either simulates the action or creates a real Razorpay Test Mode Payment Link. Execution success is never claimed as revenue recovery.

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
- **Explicit execution modes** — `retry_immediate`, `retry_delayed`, `reminder`, and `alternate_method_prompt` run as `SIMULATED`; `payment_link` runs as `REAL_RAZORPAY` (Razorpay **Test Mode** only, via the isolated `razorpay_client` boundary). Provider failures and configuration gaps produce explicit `FAILED` outcomes — never fabricated success, never a guessed Payment Link URL.
- **No client bypass** — `POST /events/{event_id}/execute` accepts no intervention and no `allowed` flag; the authoritative chain (classification → policy → selection → executor) fully determines what executes, evaluated against server-side time.
- **Persistence** — outcomes are appended to `execution_outcomes` (correlated by `event_id`); each execution also records an `intervention_attempts` row so Phase 6 policy facts (customer limits, cooldown, spend, duplicates) stay derived from persisted state. Historical decisions and outcomes are never overwritten.
- **Execution success ≠ recovery success** — `SUCCESS` means only that the operation itself ran; whether money was recovered is a later-phase concern.

What Phase 7 can and cannot do: it can select and execute (simulate or create a Test Mode Payment Link) a single authorized intervention per event. It cannot benchmark, cannot estimate recovery, cannot rank by expected value, and there is no audit dashboard or V2 optimizer.

What Phase 6 can and cannot do: it can evaluate and persist advisory policy decisions. It cannot select the best intervention, rank candidates, or execute anything. Selection, executor, Razorpay integration, benchmark, and dashboard remain planned for later phases.

# RecoveryOS

An **AI Revenue Recovery Control Plane** for the Razorpay AI Buildathon 2026 (Revenue Recovery track).

## Core Principle

> **AI recommends. Deterministic policy decides. Executor acts. Benchmark proves value.**

The LLM never has direct authority over a money-moving action. AI output is advisory; a deterministic policy gate is authoritative; an executor performs the action; a benchmark proves value against baselines.

> **Important:** This repository is in **Phase 6 — Deterministic Policy Engine / Safety Gate**. RecoveryOS does **not yet perform revenue recovery**. Event generation, ingestion, advisory AI classification, and the deterministic policy gate exist; the recovery pipeline (intervention selection, executor, Razorpay, benchmark, dashboard) is planned, not implemented.

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
│   ├── app/       Application package (models, persistence, generator, ingestion, classifier, policy)
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

**Phase 6 — Deterministic Policy Engine / Safety Gate.** A payment event now flows end-to-end through generation → ingestion → SQLite → load → AI classification → SQLite → **deterministic policy evaluation** → persisted decision. The engine is pure Python, contains zero LLM calls, and answers one question per request: *is this proposed intervention permitted?* It never selects the best intervention, never executes anything, and produces an ALLOW/DENY decision with an explicit reason and the checks applied.

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

What Phase 6 can and cannot do: it can evaluate and persist advisory policy decisions. It cannot select the best intervention, rank candidates, or execute anything. Selection, executor, Razorpay integration, benchmark, and dashboard remain planned for later phases.

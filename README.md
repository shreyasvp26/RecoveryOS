# RecoveryOS

An **AI Revenue Recovery Control Plane** for the Razorpay AI Buildathon 2026 (Revenue Recovery track).

## Core Principle

> **AI recommends. Deterministic policy decides. Executor acts. Benchmark proves value.**

The LLM never has direct authority over a money-moving action. AI output is advisory; a deterministic policy gate is authoritative; an executor performs the action; a benchmark proves value against baselines.

> **Important:** This repository is in **Phase 4 — Event Generation & Ingestion**. RecoveryOS does **not yet perform revenue recovery**. Event generation and ingestion exist; the recovery pipeline (AI reasoning, policy, executor, Razorpay, benchmark, dashboard) is planned, not implemented.

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
│   ├── app/       Application package (models, persistence, generator, ingestion)
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

**Phase 4 — Event Generation & Ingestion.** A deterministic, seedable synthetic payment event generator and a thin event ingestion boundary exist, plus a minimal `POST /events` API endpoint. Ingestion validates every event through the locked Phase 2 `PaymentEvent` contract and persists it through the Phase 3 SQLite layer.

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

Everything beyond generation + ingestion — AI, OmniRoute, policy, executor, Razorpay integration, benchmark, and dashboard — remains planned for later phases and does not exist yet.

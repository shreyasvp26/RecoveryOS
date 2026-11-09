# RecoveryOS

### Autonomous, Policy-Bounded & Economically-Optimized Failed-Payment Recovery Engine

[![Status](https://img.shields.io/badge/status-production--ready-00C853?style=flat-square)](https://github.com/shreyasvp26/RecoveryOS)
[![Python](https://img.shields.io/badge/Python-3.12%20%7C%203.13-3776AB?style=flat-square&logo=python&logoColor=white)](https://www.python.org/)
[![FastAPI](https://img.shields.io/badge/FastAPI-0.135.2-009688?style=flat-square&logo=fastapi&logoColor=white)](https://fastapi.tiangolo.com/)
[![React](https://img.shields.io/badge/React-19.0-61DAFB?style=flat-square&logo=react&logoColor=black)](https://react.dev/)
[![Vite](https://img.shields.io/badge/Vite-8.2-646CFF?style=flat-square&logo=vite&logoColor=white)](https://vitejs.dev/)
[![SQLite](https://img.shields.io/badge/SQLite-WAL%20Atomic-003B57?style=flat-square&logo=sqlite&logoColor=white)](https://www.sqlite.org/)
[![Tests](https://img.shields.io/badge/tests-1%2C749%20passed%20(67%20modules)-success?style=flat-square)](https://github.com/shreyasvp26/RecoveryOS)
[![License](https://img.shields.io/badge/license-MIT-green?style=flat-square)](LICENSE)

> **Payments fail. Recovery shouldn't be an uncontrolled retry loop.**  
> RecoveryOS is an AI-assisted, policy-bounded, economically-optimized failed-payment recovery engine engineered for Indian payment rail physics (UPI, Card, NetBanking, Wallet, and RBI e-Mandates / UPI Autopay) with closed-loop Razorpay verification.

---

## 1. Executive Summary & 5-Stage Architecture

Conventional payment recovery treats failures as dumb exceptions: `payment.failed` triggers immediate, blind retries. In the real world, blind retries saturate networks, accelerate card issuer blocklists, waste SMS and gateway fees on dead cards, and degrade merchant trust.

RecoveryOS treats recovery as an **economic decision problem under uncertainty and regulatory constraints**. It enforces a strict separation of concerns across a 5-stage pipeline:

```
┌─────────────────────────────────────────────────────────────────────────────┐
│ 1. INGESTION & CONCURRENCY CONTROL                                          │
│    • Raw-body HMAC-SHA256 signature verification (POST /webhook/razorpay)    │
│    • SHA-256 payload conflict detection (idempotent replay vs. 409 Conflict)│
│    • Timezone-aware ISO-8601 persistence into raw SQLite3 (WAL mode)        │
└──────────────────────────────────────┬──────────────────────────────────────┘
                                       │
                                       ▼
┌─────────────────────────────────────────────────────────────────────────────┐
│ 2. STAGE 1 — ADVISORY AI DIAGNOSIS                                          │
│    • Google Gemini REST API (gemini-2.5-flash, temperature: 0, JSON schema) │
│    • Diagnoses root cause & suggests candidate interventions                │
│    • ADVISORY ONLY: LLM suggestions never authorize or execute an action     │
└──────────────────────────────────────┬──────────────────────────────────────┘
                                       │
                                       ▼
┌─────────────────────────────────────────────────────────────────────────────┐
│ 3. STAGE 2 — AUTHORITATIVE 6-RULE DETERMINISTIC POLICY GATE                 │
│    Evaluates in locked precedence order:                                    │
│    [1] fraud_protection               [4] customer_intervention_limit       │
│    [2] terminal_failure               [5] event_cooldown_active             │
│    [3] duplicate_intervention         [6] daily_spend_cap_exceeded          │
│    • Type-level invariant: allowed ⊆ policy_approved_candidates             │
└──────────────────────────────────────┬──────────────────────────────────────┘
                                       │
                                       ▼
┌─────────────────────────────────────────────────────────────────────────────┐
│ 4. STAGE 3 — ECONOMIC INTERVENTION OPTIMIZER                                │
│    • Two-Level Hierarchical Empirical-Bayes Recovery Probability Estimator  │
│    • Integer-Paise Expected Value computation:                              │
│      EV = ⌊amount × p_bps / 10,000⌋ - cost - ⌊amount × friction_bps / 10,000⌋│
│    • Zero float literals, zero try/except (enforced by AST tests)           │
│    • Audit records persisted BEFORE any execution attempt                   │
└──────────────────────────────────────┬──────────────────────────────────────┘
                                       │
                                       ▼
┌─────────────────────────────────────────────────────────────────────────────┐
│ 5. STAGE 4 & 5 — BOUNDED EXECUTION & CLOSED-LOOP OUTCOME VERIFICATION        │
│    • Atomic compare-and-set locks (execution_claims table)                  │
│    • Real Razorpay test mode (payment_link generation)                      │
│    • Ambiguous timeout safety: PROVIDER_RESULT_UNKNOWN retains lock         │
│    • Outcome loop: webhook_recovery_outcomes (paid) + polled expired links   │
└─────────────────────────────────────────────────────────────────────────────┘
```

---

## 2. Key Engineering Highlights

### 2.1 Indian Payment Rail & RBI Mandate Physics
RecoveryOS models the real physical constraints of India's payment rails rather than treating all payments as generic card tokens:
- **Dead-Card Retry Saturation**: When failure is `expired_card` or `card_declined_permanent`, the recovery probability is mathematically collapsed to $0\text{ bps}$. Retrying dead instruments guarantees 100% failure, racking up gateway rejection penalties and customer irritation.
- **Customer-Present Rail Constraints**: For `upi`, `netbanking`, and `wallet` transactions initiated without an active mandate, automated backend retries without customer presence are structurally impossible on Indian rails. RecoveryOS immediately selects customer-initiated interventions (`payment_link`, `reminder`, or `alternate_method_prompt`).
- **RBI ₹15,000 e-Mandate AFA Threshold**: Under Reserve Bank of India (RBI) recurring mandate regulations, transactions above ₹15,000.00 (`1,500,000` paise) require explicit Additional Factor of Authentication (AFA / OTP). Automated standing instruction retries over this limit fail instantly; RecoveryOS diverts them to authenticated payment links.
- **NPCI UPI Response Code Mapping**: Ingests and maps NPCI error codes to actionable root causes:
  - `U30`: Debit timed out $\rightarrow$ Delayed retry after settlement window
  - `BT`: Acquirer/Issuer bank down $\rightarrow$ Outage-aware reroute or alternate method
  - `Z9`: Insufficient funds $\rightarrow$ Smart scheduled retry / salary-cycle reminder
  - `ZM`: Invalid MPIN $\rightarrow$ Customer prompt (immediate retry forbidden)
  - `ZA`: Customer account inactive/dormant $\rightarrow$ Terminal policy deny
  - `U16`: Risk threshold exceeded $\rightarrow$ Policy fraud gate trigger

### 2.2 Concurrency, Idempotency & Ambiguous Timeout Safety
- **SQLite WAL Compare-and-Set**: Execution concurrency is guarded by `execution_claims` with `PRIMARY KEY (event_id, intervention)`. Concurrent requests to execute an intervention on the same event collide at SQLite commit time; exactly one succeeds and the other receives an existing claim or `409 Conflict`.
- **Ambiguous Timeout Safety (`PROVIDER_RESULT_UNKNOWN`)**: If a network timeout or HTTP 5xx occurs during a Razorpay API call, the claim is transitioned to `provider_result_unknown` and the lock is **retained**. Subsequent retry attempts cannot issue duplicate payment links or charge customers twice while the external state is unresolved.
- **Webhook Raw-Body HMAC-SHA256**: All incoming Razorpay webhook payloads are verified against the secret using the unparsed raw request body bytes before JSON deserialization, preventing hash mismatch from JSON formatting differences.
- **Conflict Rejection**: Ingestion computes a SHA-256 hash of every payload. If a webhook delivery arrives with a previously seen ID but a differing hash, the API responds with `409 Conflict` rather than silently overwriting audit state.

### 2.3 Right-Censored Two-Level Hierarchical Empirical-Bayes Calibration
RecoveryOS uses an Empirical-Bayes shrinkage estimator to dynamically update recovery probabilities from verified operational outcomes:
- **Level-1 Intervention Posterior**: Intervention success rates are pooled across all events and shrunk toward the baseline cold-start scorecard prior with strength $N_0 = 20$, activated only when sample size $N \ge 10$.
- **Level-2 Segment Posterior**: High-granularity segment posteriors for `(intervention, failure_reason, payment_method)` are shrunk toward the Level-1 intervention posterior with strength $N_0 = 10$, activated when segment sample size $N \ge 5$.
- **Right-Censoring & Positive-Selection Guard**: Calibration cannot be updated solely from `payment_link.paid` webhooks; doing so creates severe survivor bias. RecoveryOS explicitly tracks negative outcomes by polling Razorpay for `expired` or `cancelled` links (`POST /estimation/payment-links/{id}/refresh`), persisting `NOT_RECOVERED` outcomes into `provider_payment_link_outcomes` so posteriors reflect true Bernoulli parameters.

### 2.4 Outage-Aware Routing & Counterfactual Policy Lab
- **Rolling Window Outage Detection**: Computes rolling 28-day vs. 28-day baseline degradation rates for `(bank, payment_method)` tuples (e.g. HDFC UPI). When significant degradation is detected, active incidents are exposed to the operator dashboard and decisions reflect rail unavailability.
- **Counterfactual Policy Replay**: Implements Common Random Numbers (CRN) using deterministic `blake2b` hashing. Risk and finance operators can test hypothetical policy scenarios (e.g., changing cooldowns from 30m to 15m or adjusting daily spend caps) against historical event distributions, observing exact counterfactual revenue lift, cost deltas, and regret without production risk.

---

## 3. Phase 17 Multi-Seed Benchmark Results

RecoveryOS evaluates recovery performance using a controlled synthetic benchmark against a hidden ground-truth model of the world. All monetary figures use exact integer paise ($\text{₹}1.00 = 100\text{ paise}$).

### 3.1 Single-Seed Detailed Benchmark (Seed 42, 500 Events)

| Strategy | Recovered Revenue | Incremental vs No Action | Attempts | True Economic Value | Economic Regret | Optimal Selection | Safety Violations |
| :--- | :---: | :---: | :---: | :---: | :---: | :---: | :---: |
| **No Action** (Baseline) | ₹263,751.00 | ₹0.00 | 0 | ₹259,354.71 | ₹627,955.89 | 0.0% | 0 |
| **Naive Retry** (Blind) | ₹286,095.00 | ₹22,344.00 | 228 | ₹205,679.61 | *n/a* | 0.0% | 0 |
| **Ordered Playbook** (Rules) | ₹935,659.00 | ₹671,908.00 | 168 | ₹847,550.96 | ₹39,759.64 | 65.5% | 0 |
| **RecoveryOS V1** (Fixed Priority)| ₹766,938.00 | ₹503,187.00 | 168 | ₹657,781.98 | ₹229,528.62 | 38.1% | 0 |
| **RecoveryOS V2** (Economic Opt) | **₹990,118.00** | **₹726,367.00** | 168 | **₹836,275.84** | **₹51,034.76** | **54.2%** | **0** |
| **Oracle** (Theoretical Upper Bound)| ₹983,039.00 | ₹719,288.00 | 168 | ₹887,310.60 | ₹0.00 | 100.0% | 0 |

> **Single Run Key Metrics (Seed 42)**:
> - **V2 vs V1 True EV Lift**: **+₹178,493.86** (`Result: V2 WON`)
> - **V2 vs V1 Revenue Lift**: **+₹223,180.00**
> - **V2 Incremental Oracle Capture**: **91.9%** (vs. V1's 63.4%)
> - **V2 False Intervention Rate**: **0.0%** (vs. V1's 7.7% wasteful actions)
> - **Selection Disagreements (V1 vs V2)**: **106 events** where economic optimization chose differently than fixed rules.

---

### 3.2 Multi-Seed Statistical Benchmark (Seeds 42, 43, 44, 45, 46 — 2,500 Total Events)

To prove that economic optimization is not an artifact of random seed selection, RecoveryOS executes a 5-seed statistical test battery:

| Benchmark Metric | Mean | Min | Max | Median | Status |
| :--- | :---: | :---: | :---: | :---: | :---: |
| **V2 vs V1 True EV Delta** | **+₹191,732.30** | +₹138,997.30 | +₹268,776.32 | +₹178,493.86 | **5/5 Wins (100%)** |
| **V2 vs V1 Recovered Revenue**| **+₹143,672.00** | +₹30,744.00 | +₹233,292.00 | +₹143,672.00 | **Positive Across All Seeds** |
| **V2 vs Playbook Revenue** | **+₹21,533.80** | -₹8,459.00 | +₹54,459.00 | +₹21,533.80 | **Positive Expected Lift** |
| **Fraud Intervention Rate** | **0.0%** | 0.0% | 0.0% | 0.0% | **0 Violations (100% Pass)** |
| **Unauthorized Attempts** | **0** | 0 | 0 | 0 | **0 Violations (100% Pass)** |
| **Runtime Exceptions** | **0** | 0 | 0 | 0 | **100% Zero-Crash** |
| **Fairness Verification** | **PASS** | PASS | PASS | PASS | **Deterministic & Order-Invariant** |

---

## 4. Hard Architectural & AST Isolation Contracts

RecoveryOS enforces strict software architecture boundaries via automated AST parsing and token-stream inspection in `backend/tests/`:

1. **Decision Engine Mathematical Isolation (`test_optimizer_isolation.py`)**:
   - **Zero `try / except` blocks**: `economics.py`, `estimator.py`, and `optimizer.py` must never mask math errors.
   - **Zero `float` literals**: All calculations must use integer paise and basis points ($0..10{,}000\text{ bps}$).
   - **Zero arithmetic operators in audit (`optimizer_audit.py`)**: The audit recorder records what the optimizer decided; it is structurally forbidden from recomputing or modifying the arithmetic.
   - **Zero I/O or network imports**: `socket`, `httpx`, `requests`, `urllib`, `sqlite3`, `time`, `random`, and `os` are banned directly and transitively from the decision modules.
2. **SUT vs. Ground-Truth Boundary (`test_phase17_isolation.py`)**:
   - System Under Test (SUT) modules can **never** import `hidden_world`, `outcome_model`, or `benchmark*`.
   - Banned tokens in executable SUT code: `HiddenWorld`, `HiddenOutcome`, `true_probability_bps`, `true_expected_value_paise`, `OracleEvaluation`, etc.
3. **Incident & Intelligence Read-Only Safety (`test_incident_safety.py`, `test_feedback_integrity.py`)**:
   - Forbids network clients, clock calls (`datetime.now`), random generators, and database mutation operations (`insert_`, `update_`, `delete`) in incident analysis and recovery intelligence services.

---

## 5. Technology Stack & Zero-Hallucination Ground Truth

- **Backend**:
  - Python 3.12 / 3.13 runtime with `FastAPI 0.135.2` and `Uvicorn 0.42.0`.
  - Persistence: **Raw `sqlite3`** (WAL mode enabled, 13 normalized tables, `sqlite3.Row` factory).
  - Domain Modeling: Immutable standard library `@dataclass(frozen=True)` models (`app/models.py`).
  - *No SQLAlchemy, no Alembic, no Pydantic models, no Celery, no Redis, no Stripe.*
- **AI Diagnostics**:
  - Google Gemini REST API (`gemini-2.5-flash`) called via HTTP with `temperature: 0` and structured JSON response formatting.
- **Provider Integration**:
  - Official `razorpay 2.0.1` client operating in test mode (`rzp_test_*` keys enforced; `rzp_live_*` rejected at runtime).
- **Frontend**:
  - React 19 SPA built with `Vite 8.2` and linted with `oxlint`.
  - 8 operational views: Command Center, Revenue Health, Recovery Ops, Recovery Intelligence, Estimator Evidence, Event Trace, Policy Blocks, and Policy Lab.
  - Transparent proxy injecting operator API keys from `backend/.env`.

---

## 6. Quickstart & Operational Commands

### 6.1 Prerequisites & Environment Setup
Clone the repository and set up the Python virtual environment:
```bash
git clone https://github.com/shreyasvp26/RecoveryOS.git
cd RecoveryOS

# Setup backend venv
python3 -m venv .venv
source .venv/bin/activate
pip install -r backend/requirements.txt
```

Create `backend/.env` (copy from `backend/.env.example`):
```bash
cp backend/.env.example backend/.env
```
Ensure the following keys are populated in `backend/.env`:
```ini
RECOVERYOS_OPERATOR_API_KEY=test_operator_key_secret_123
GEMINI_API_KEY=your_gemini_api_key_here
RAZORPAY_KEY_ID=rzp_test_your_key_id
RAZORPAY_KEY_SECRET=your_razorpay_secret
RAZORPAY_WEBHOOK_SECRET=your_webhook_secret
```

### 6.2 Seed Database with Deterministic Events & Persist Benchmark
```bash
(cd backend && python -m app.populate --reset)
```
*Initializes 13 SQLite tables, migrates schemas, populates 500 deterministic events, classifies, executes, and persists the Phase 17 benchmark summary into `backend/recoveryos.db`.*

### 6.3 Launch Backend Server
```bash
(cd backend && uvicorn app.main:app --reload --port 8000)
```
Backend API will be live at `http://127.0.0.1:8000`.

### 6.4 Launch Frontend Operator Dashboard
In a separate terminal:
```bash
cd frontend
npm install
npm run dev
```
Open `http://localhost:5173` to access the Command Center and operational suites.

---

## 7. Verification & Benchmark Commands

Every change to RecoveryOS must pass all three gates before merging:

### Gate 1: Full Backend Test Suite (1,749 Tests)
```bash
(cd backend && python -m pytest -q)
```
*Expected: `1749 passed` in ~60 seconds with 0 failures.*

### Gate 2: Statistical Multi-Seed Benchmark
```bash
(cd backend && python -m app.benchmark_phase17 --seeds 42,43,44,45,46)
```
*Expected: `5/5 Wins (100.0%)`, `Safety Checks: PASS`, `Fairness Checks: PASS`.*

### Gate 3: Frontend Linter & Production Build
```bash
(cd frontend && npm run lint && npm run build)
```
*Expected: `0 warnings, 0 errors`, Vite production bundle built successfully.*

---

## 8. Repository Structure

```text
RecoveryOS/
├── AGENTS.md                          # Authoritative agent operating manual & contracts
├── README.md                          # Technical system overview & benchmarks
├── backend/
│   ├── app/
│   │   ├── adaptive_estimation.py     # Hierarchical Empirical-Bayes shrinkage estimator
│   │   ├── benchmark_phase17.py       # Signal-bearing 5-arm benchmark runner
│   │   ├── benchmark_phase17_report.py# Verdict, regret & summary formatting
│   │   ├── classifier.py              # Gemini 2.5 Flash advisory diagnostics
│   │   ├── db.py                      # Raw SQLite3 persistence (13 tables, WAL mode)
│   │   ├── economics.py               # Integer paise EV formula & candidate evaluation
│   │   ├── estimator.py               # Cold-start additive basis-point scorecard
│   │   ├── execution_service.py       # Bounded execution flow & provenance capture
│   │   ├── executor.py                # Razorpay test mode payment link client
│   │   ├── hidden_world.py            # Ground-truth evaluation Bernoulli simulator
│   │   ├── optimizer.py               # Non-positive EV filtering & argmax selection
│   │   ├── policy.py                  # 6-rule deterministic policy gate
│   │   ├── populate.py                # Reproducible seeding script (--reset supported)
│   │   └── routes/                    # FastAPI endpoints (operator auth & webhooks)
│   └── tests/                         # 67 test modules (AST isolation, safety & logic)
└── frontend/
    ├── src/
    │   ├── components/                # React 19 UI (CommandCenter, PolicyLab, etc.)
    │   ├── core/                      # API client, formatters, state management
    │   └── App.jsx                    # Operator navigation router
    └── vite.config.js                 # Proxy injecting Bearer auth from backend/.env
```

---

## 9. License

MIT License. Designed and engineered for resilient, explainable, and compliant payment recovery.

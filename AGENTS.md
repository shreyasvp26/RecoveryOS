# AGENTS.md — RecoveryOS Principal Engineering & Autonomous Agent Contract

This document is the authoritative operating manual for autonomous coding agents (including Antigravity / Gemini 3.8 Flash) working inside **RecoveryOS**. Read and obey every invariant in this file before modifying any module or test.

---

## 1. System Overview & 5-Stage Pipeline Architecture

**RecoveryOS** is an AI-assisted, policy-bounded, economically-optimized failed-payment recovery engine built for Indian payment rails (UPI, Card, NetBanking, Wallet, and RBI e-Mandates / UPI Autopay) with closed-loop Razorpay verification.

### 1.1 Tech Stack & Zero-Hallucination Ground Truth
- **Backend (`backend/app/`, Python 3.12, `fastapi==0.135.2`, `uvicorn==0.42.0`, `httpx==0.28.1`, `razorpay==2.0.1`, `pytest==9.0.2`)**:
  - Persistence uses **raw `sqlite3`** (`backend/app/db.py`, 13 tables, `sqlite3.Row` row factory) and immutable stdlib `@dataclass(frozen=True)` domain models (`backend/app/models.py`).
  - **NEVER import or invent SQLAlchemy, Alembic, Pydantic models, Celery, Redis, or Stripe.** None of these exist in this repository.
- **Frontend (`frontend/src/`, React 19, Vite 8, `oxlint`)**:
  - 7 operator views (`CommandCenter.jsx`, `RevenueHealth.jsx`, `RecoveryOps.jsx`, `RecoveryIntelligence.jsx`, `EstimatorEvidence.jsx`, `EventTrace.jsx`, `PolicyBlocks.jsx`, `PolicyLab.jsx`) plus `LandingPage.jsx`.
  - `frontend/vite.config.js` loads `../backend/.env` via `loadEnv` and injects `Authorization: Bearer <RECOVERYOS_OPERATOR_API_KEY>` into `/api` proxy requests; `frontend/src/core/api.js` also reads `import.meta.env.VITE_OPERATOR_API_KEY` and `localStorage.getItem("recoveryos_operator_api_key")`.

### 1.2 The 5-Stage Decision & Execution Pipeline
1. **Ingestion (`ingestion.py`, `failed_payment_ingestion.py`, `razorpay_webhook.py`)**:
   - Accepts synthetic workloads (`populate.py` / `generator.py`), operator `POST /events`, or HMAC-SHA256 verified Razorpay `payment.failed` webhooks (`POST /webhook/razorpay`).
   - Persists immutable rows into `payment_events` with timezone-aware ISO-8601 timestamps.
2. **Stage 1 — Advisory AI Diagnosis (`classifier.py`, `classification.py`)**:
   - Uses the native Google Gemini REST API (`GeminiClassifier` calling `https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent` with `x-goog-api-key: <GEMINI_API_KEY>`, default model `gemini-2.5-flash`, `temperature: 0`, `responseMimeType: "application/json"`).
   - Outputs `ClassificationResult(event_id, root_cause_category, confidence, reasoning, candidate_interventions)`.
   - Advisory only: the LLM can suggest candidates and diagnose root cause, but it **never** authorizes or executes an intervention.
3. **Stage 2 — Authoritative Deterministic Policy Gate (`policy.py`, `db.get_policy_history`)**:
   - Evaluates 6 safety rules in locked precedence order (`DETERMINISTIC_RULE_ORDER`):
     1. `fraud_protection` (`RULE_FRAUD`)
     2. `terminal_failure` (`RULE_TERMINAL`)
     3. `duplicate_intervention` (`RULE_DUPLICATE`)
     4. `customer_intervention_limit_exceeded` (`RULE_CUSTOMER_LIMIT`)
     5. `event_cooldown_active` (`RULE_COOLDOWN`)
     6. `spend_cap_exceeded` (`RULE_SPEND_CAP`)
   - Persists every candidate verdict (`ALLOW` or `DENY`) to `policy_decisions`.
4. **Stage 3 — Economic Intervention Optimizer (`economics.py`, `estimator.py`, `adaptive_estimation.py`, `optimizer.py`, `optimizer_audit.py`)**:
   - `AllowedCandidates` enforces the type-level invariant `allowed ⊆ policy_approved_candidates`. Denied candidates are never scored or selected.
   - `RecoveryProbabilityEstimator` (cold-start additive basis-point scorecard) wrapped by `CalibratedRecoveryProbabilityEstimator` (Empirical-Bayes Beta-binomial shrinkage from verified outcomes).
   - Computes integer-paise Expected Value:
     $$\text{EV}_{\text{paise}} = \lfloor \text{amount\_paise} \times p_{\text{bps}} / 10{,}000 \rfloor - \text{cost\_paise} - \lfloor \text{amount\_paise} \times \text{friction\_bps} / 10{,}000 \rfloor$$
   - Refuses non-positive EV candidates (`expected_value_paise <= 0` $\rightarrow$ `no_action` with `REASON_NEGATIVE_EXPECTED_VALUE`).
   - Persists audit records to `optimizer_decisions` **before** execution.
5. **Stage 4 & 5 — Bounded Execution & Closed-Loop Outcome Verification (`execution_service.py`, `executor.py`, `razorpay_client.py`, `webhook_service.py`, `outcome_feedback.py`, `calibration_service.py`)**:
   - Acquires atomic SQLite claim in `execution_claims` (`PRIMARY KEY (event_id, intervention)`).
   - `payment_link` runs in `REAL_RAZORPAY` mode (`rzp_test_*` keys enforced; `rzp_live_*` keys rejected). Ambiguous network/5xx errors mark the claim `provider_result_unknown` and retain the lock so retries cannot create duplicate payment links.
   - `payment_link.paid` webhooks correlate by `payment_link_id` into `webhook_recovery_outcomes` (unique per `payment_link_id`); expired/cancelled links polled via `POST /estimation/payment-links/{id}/refresh` record `NOT_RECOVERED` in `provider_payment_link_outcomes`.

---

## 2. Exact Build, Test, Benchmark & Seed Commands

Always run commands from the repository root (`/Users/shreyasvp/Desktop/Projects/RecoveryOS`) using the project virtual environment (`../.venv/bin/python`):

1. **Backend Test Suite (Must pass 100% with 0 failures)**:
   ```bash
   (cd backend && ../.venv/bin/python -m pytest -q)
   ```
2. **Phase 17 Signal-Bearing Benchmark (Must report `Result: V2 WON`, 0% safety violations, 100% fairness PASS)**:
   ```bash
   (cd backend && ../.venv/bin/python -m app.benchmark_phase17 --seed 42 --count 500)
   ```
3. **Frontend Linter & Production Bundle Build (Must report 0 warnings, 0 errors)**:
   ```bash
   (cd frontend && npm run lint && npm run build)
   ```
4. **Seed Local SQLite Operational Database (`backend/recoveryos.db`)**:
   ```bash
   (cd backend && ../.venv/bin/python -m app.populate --reset)
   ```

---

## 3. Hard AST & Structural Test Isolation Contracts (NEVER VIOLATE)

RecoveryOS enforces architectural boundaries via static AST and token-stream inspection in `backend/tests/`. Violating any of these rules will immediately break the test suite:

### 3.1 Decision Engine & Audit Isolation (`backend/tests/test_optimizer_isolation.py`)
Inspects the AST of `economics.py`, `estimator.py`, `optimizer.py`, and `optimizer_audit.py`:
- **NO `try / except` (`ast.ExceptHandler`)** anywhere in `economics.py`, `estimator.py`, or `optimizer.py`. (In `optimizer_audit.py`, any `except` block must explicitly `raise`).
- **NO float literals (`float`)** anywhere in `economics.py`, `estimator.py`, or `optimizer.py`.
- **NO arithmetic operators (`+`, `-`, `*`, `//`, `/`, `%`)** anywhere in `optimizer_audit.py` (it records the optimizer's arithmetic; it must never recompute it).
- **NO forbidden stdlib or I/O imports** (`random`, `secrets`, `socket`, `http`, `urllib`, `requests`, `httpx`, `razorpay`, `time`, `uuid`, `os`, `subprocess`, `sqlite3`) directly or **transitively** from `economics.py`, `estimator.py`, `optimizer.py`, or `optimizer_audit.py`.
- **NO execution, persistence, LLM, or evaluation imports** (`db`, `executor`, `execution_service`, `razorpay_client`, `razorpay_webhook`, `classifier`, `benchmark*`, `outcome*`, `hidden_world`) directly or transitively.
- **NO subscripting by `event_id`** (`node.slice` containing `event_id`) in `economics.py`, `estimator.py`, `optimizer.py`, or `hidden_world.py`.
- **NO policy rule duplication in `optimizer.py`**: `optimizer.py` must never mention `fraud_suspect`, `risk_flag`, `cooldown_minutes`, `max_interventions_per_customer`, `daily_spend_cap`, `has_successful_intervention`, `PolicyEngine`, `PolicyInput`, `PolicyConfig`, `PolicyHistory`, or `denial_reason`. It may only read `decision.allowed`.

### 3.2 SUT vs. Hidden Ground Truth Isolation (`backend/tests/test_phase17_isolation.py`)
Inspects `classifier.py`, `classification.py`, `policy.py`, `selector.py`, `estimator.py`, `economics.py`, `optimizer.py`, `executor.py`, `execution_service.py`, and `dashboard.py`:
- **NEVER import evaluation modules** (`benchmark*`, `outcome*`, `hidden_world`) from any SUT module.
- **NEVER mention ground-truth identifiers** in executable code (`HiddenWorld`, `HiddenOutcome`, `true_probability_bps`, `true_expected_value_paise`, `deterministic_draw_bps`, `OracleEvaluation`, `evaluate_oracle`, `oracle_true_ev_paise`, `true_ev_paise`, `hidden_probability`, `ground_truth`, `Oracle`, `oracle`).
- **Event identity exclusion in `hidden_world.py`**: Functions `true_probability_bps`, `_customer_history_bps`, and `true_expected_value_paise` in `hidden_world.py` must NEVER reference `event_id`, `order_id`, `payment_id`, `customer_id`, or `timestamp`.

### 3.3 Revenue Health Incident Safety (`backend/tests/test_incident_safety.py`)
Tokenizes executable code in `app/incidents.py`, `app/incident_analysis.py`, and `app/routes/incidents.py`:
- **FORBIDS tokens** (case-insensitive in executable code):
  - Provider/network: `razorpay`, `payment_link`, `httpx`, `requests`, `socket`
  - Ground truth/probabilities: `true_probability`, `true_ev`, `oracle`, `recovery_probability`
  - Nondeterminism/clock: `datetime.now`, `utcnow`, `uuid`, `random.`, `time.time`
  - Database mutation: `insert`, `upsert`, `update`, `delete`, `commit`
- If connecting incident data to other views (e.g., Phase 39), put the integration logic in `recovery_operations.py` / `dashboard.py` — do NOT add forbidden tokens to `incidents.py`, `incident_analysis.py`, or `routes/incidents.py`.

### 3.4 Outcome Feedback & Recovery Intelligence Integrity (`backend/tests/test_feedback_integrity.py`)
Inspects `app/outcome_feedback.py`, `app/recovery_intelligence.py`, and `app/routes/intelligence.py`:
- **FORBIDS importing**: `hidden_world`, `outcome_model`, `outcome`, `benchmark*`, `executor`, `execution_service`, `razorpay_webhook`, `webhook_service`, `policy_scenario`, `estimator`, `optimizer`, `classifier`, `replay`.
- From `app.policy`, **only** `parse_aware_datetime` and `PolicyValidationError` may be imported.
- **FORBIDS database mutation substrings** (`insert_`, `update_`, `upsert_`, `delete`, `claim_`, `commit`) in `outcome_feedback.py` and `recovery_intelligence.py`.
- `app/routes/intelligence.py` may only register `GET`/`HEAD` endpoints.

### 3.5 Policy Scenario Identity (`backend/tests/test_policy_scenario_identity.py`)
Inspects `app/policy_scenario.py`:
- **FORBIDS** `os.environ`, `os.getenv`, or any string literal starting with `"POLICY_*"` inside `app/policy_scenario.py` (must resolve runtime policy strictly via `config.build_policy_config()`).
- **FORBIDS** restating `CUSTOM_MIN_*` or `CUSTOM_MAX_*` constants outside `app/policy_scenario.py`.

---

## 4. Coding & Arithmetic Conventions

1. **Integer Paise Everywhere**:
   - All monetary values (`amount_paise`, `cost_paise`, `daily_spend_cap_paise`, `expected_value_paise`, `recovered_amount_paise`) must be plain Python `int` (never `float`, never `bool` — always guard with `isinstance(v, int) and not isinstance(v, bool)`).
   - ₹1.00 = `100` paise; ₹15,000.00 = `1_500_000` paise; ₹50,000.00 = `5_000_000` paise.
2. **Integer Basis Points (`0..10_000`)**:
   - All probabilities and friction rates use integer basis points (`PROBABILITY_SCALE = 10_000`, where `100 bps = 1.00%` and `10_000 bps = 100.00%`).
   - Use integer floor division (`// PROBABILITY_SCALE`) when converting basis points to paise.
3. **Read-Only Coefficient Tables**:
   - All static coefficient tables in `estimator.py` and `hidden_world.py` must be wrapped in `_frozen(...)` (`MappingProxyType`, including nested mappings) so runtime mutation attempts raise `TypeError`.
   - Whenever new static tables or constants are added to `estimator.py` or `hidden_world.py`, include them in `_estimator_fingerprint()` (`benchmark_config.py`) and `hidden_world_fingerprint()` (`hidden_world.py`) so configuration fingerprints remain sensitive to every model parameter.
4. **Timezone-Aware Timestamps**:
   - Every timestamp across `PaymentEvent`, `PolicyDecision`, `ExecutionOutcome`, and SQLite persistence must be an explicit timezone-aware ISO-8601 string (parsed via `parse_aware_datetime`). Never use naive `datetime.now()`.

---

## 5. Mandatory Git Workflow (Every Phase)

For every phase or feature change, follow this exact sequence:

1. **Verify Clean Working Tree on `main`**:
   ```bash
   git status && git checkout main
   ```
2. **Create Dedicated Phase Branch**:
   ```bash
   git checkout -b feat/phase<N>-<short-slug>
   ```
3. **Read Before Editing**:
   - Inspect all target source files and their corresponding test files before making edits.
4. **Implement & Add Regression Tests**:
   - Update implementation and unit/integration tests in lockstep.
5. **Run All 3 Verification Gates Before Committing**:
   ```bash
   (cd backend && ../.venv/bin/python -m pytest -q)
   (cd backend && ../.venv/bin/python -m app.benchmark_phase17 --seed 42 --count 500)
   (cd frontend && npm run lint && npm run build)
   ```
6. **Verify Staged Files**:
   - Check `git status` and `git diff` to ensure untracked/local artifacts (`backend/.env`, `backend/*.db`, `frontend/dist/`) are **never** staged.
7. **Commit & Merge to `main` with `--no-ff`**:
   ```bash
   git add <explicit-file-list>
   git commit -m "Add Phase <N> <concise summary>"
   git checkout main
   git merge --no-ff feat/phase<N>-<short-slug> -m "Merge branch 'feat/phase<N>-<short-slug>'"
   ```

# RecoveryOS — Complete System Architecture & Component Deep Dive (Phases 1 & 2)

> **Verification Legend**: Every claim in this document is tagged as **VERIFIED** (directly observed in source code with `path:line` citation), **INFERRED** (logically derived from cited source code), or **UNKNOWN** (cannot be determined from static source alone).

## 1. Repository Inventory & Stack Overview (Phase 1)

### 1.1 File Tree & Line Count Summary

- **Total Non-Vendor Files**: **183 files** (**182 text files** totaling **60,058 lines** + **1 binary SQLite database** `backend/recoveryos.db` at 1,306,624 bytes / 1,001 raw newline bytes = **61,059 raw lines**) — **VERIFIED** (`FILE_COVERAGE.md`).
- **Breakdown by Area** — **VERIFIED**:
  - **Root & CI**: 3 files (`.gitignore` [57L], `.github/workflows/ci.yml` [45L], `README.md` [715L]).
  - **Backend Config & DB**: 4 files (`backend/.env` [45L], `backend/.env.example` [57L], `backend/requirements.txt` [11L], `backend/recoveryos.db` [1,306,624B]).
  - **Backend Application (`backend/app/`)**: 45 files (37 domain/service modules + 8 route modules in `backend/app/routes/`, including two 0-byte `__init__.py` files) totaling **17,058 lines**.
  - **Backend Tests (`backend/tests/`)**: 67 files (`conftest.py` + 66 `test_*.py` files) totaling **17,982 lines**.
  - **Documentation (`docs/`)**: 17 markdown files totaling **4,241 lines**.
  - **Frontend (`frontend/`)**: 27 files (21 source/config files + 1 `package-lock.json` [1,316L] + 1 `public/favicon.svg` + 4 built `dist/` files) totaling **20,804 lines**.

### 1.2 Languages, Frameworks, Libraries & Versions

- **Backend (`backend/requirements.txt:1-11`, `.github/workflows/ci.yml:17`)** — **VERIFIED**:
  - **Python**: `3.12` in CI (`.github/workflows/ci.yml:17`); local `.venv` uses Python 3.
  - **`fastapi==0.135.2`**: HTTP API framework (`backend/app/main.py:13`).
  - **`uvicorn[standard]==0.42.0`**: ASGI server (`backend/requirements.txt:2`).
  - **`httpx==0.28.1`**: Synchronous HTTP client used exclusively by `OmniRouteClassifier` (`backend/app/classifier.py:14,121`).
  - **`python-dotenv==1.2.2`**: Loads `backend/.env` with `override=False` at module import time (`backend/app/config.py:14,32`).
  - **`razorpay==2.0.1`**: Official Razorpay Python SDK (`requests`-based) wrapped by `RazorpayPaymentLinkClient` (`backend/app/razorpay_client.py:14,151`).
  - **`pytest==9.0.2`**: Backend test runner (`backend/requirements.txt:3`).
  - **Database & Models**: Standard-library `sqlite3` (`backend/app/db.py:18`) and standard-library `dataclasses` (`backend/app/models.py:12`). Despite `README.md:9,363-364` claiming `SQLAlchemy`, `Pydantic`, and `Stripe`, **neither SQLAlchemy, Pydantic, nor Stripe exists anywhere in `requirements.txt` or `backend/app/`** — **VERIFIED**.
- **Frontend (`frontend/package.json:1-23`, `.github/workflows/ci.yml:34`)** — **VERIFIED**:
  - **Node.js**: `22` in CI (`.github/workflows/ci.yml:34`).
  - **`react@^19.2.4` & `react-dom@^19.2.4`**: UI rendering (`frontend/package.json:12-13`).
  - **`vite@^8.0.1` & `@vitejs/plugin-react@^6.0.1`**: Dev server and bundler (`frontend/package.json:16-17`, `frontend/vite.config.js:1-18`).
  - **`oxlint@^1.56.0`**: Linter configured with `react` and `react-hooks` plugins (`frontend/.oxlintrc.json:1-8`).

### 1.3 Environment Variables, Secrets & Configuration Handling

Configuration is loaded in `backend/app/config.py:1-210` via `load_dotenv(_ENV_PATH, override=False)` (`config.py:32`) where `_ENV_PATH` resolves to `backend/.env` (`config.py:29`).

| Environment Variable | Default in Code | Consumed At | Purpose & Security Notes |
|---|---|---|---|
| `DATABASE_URL` | `sqlite:///./recoveryos.db` | `backend/app/db.py:31-52` | Resolved to local SQLite file path (`db.py:38-52`). Non-`sqlite:///` schemes raise `ValueError` (`db.py:44-45`, **VERIFIED**). |
| `RECOVERYOS_OPERATOR_API_KEY` | None (Fail-Closed) | `backend/app/auth.py:28-63`, `config.py:148-153` | Shared bearer token required on all 7 operator routers (`main.py:83-90`). When unset/blank, all operator endpoints return **HTTP 503** (`auth.py:36-41`). **CRITICAL GAP**: Not set in `backend/.env` and never sent by `frontend/src/core/api.js` (**VERIFIED**). |
| `RECOVERYOS_CORS_ORIGINS` | `""` (empty list) | `backend/app/config.py:156-163`, `main.py:42-49` | Comma-separated CORS origin allowlist. Empty by default (`main.py:42`, **VERIFIED**). |
| `OMNIROUTE_API_KEY` | None | `backend/app/classifier.py:181-190` | Bearer token for OmniRoute `/chat/completions` (`classifier.py:134`). **Present in plaintext on disk in `backend/.env:45`** (**VERIFIED**). |
| `OMNIROUTE_BASE_URL` | `https://api.omniroute.ai/v1` | `backend/app/classifier.py:25,94-112,182` | Validated by `_validate_base_url` (`classifier.py:94-112`): requires `https://` unless host is `localhost` or `127.0.0.1`. Note: `backend/.env:43` sets `http://localhost:20128/v1` (**VERIFIED**). |
| `OMNIROUTE_MODEL` | `gpt-4o-mini` | `backend/app/classifier.py:26,183` | Model identifier sent in chat completion payload (`classifier.py:138`). Note: `backend/.env:44` sets `kr/claude-sonnet-4.5` (**VERIFIED**). |
| `RAZORPAY_KEY_ID` | None | `backend/app/razorpay_client.py:124-149`, `routes/events.py:72-90` | Must start with `rzp_test_`; `rzp_live_` is explicitly rejected with `RazorpayConfigurationError` (`razorpay_client.py:136-145`). **Present on disk in `backend/.env:39`** (**VERIFIED**). |
| `RAZORPAY_KEY_SECRET` | None | `backend/app/razorpay_client.py:146-151` | Razorpay test secret. **Present on disk in `backend/.env:40`** (**VERIFIED**). |
| `RAZORPAY_WEBHOOK_SECRET` | None | `backend/app/routes/webhook.py:42-48`, `razorpay_webhook.py:75-115` | HMAC-SHA256 secret for `X-Razorpay-Signature`. **Present on disk in `backend/.env:41`** (**VERIFIED**). |
| `POLICY_MAX_INTERVENTIONS_PER_CUSTOMER_24H` | `2` | `backend/app/config.py:21,88-93` | Rolling 24-hour customer intervention cap (`policy.py:106`). |
| `POLICY_EVENT_COOLDOWN_MINUTES` | `30` | `backend/app/config.py:22,94-99` | Per-event minimum cooldown between intervention attempts (`policy.py:107`). |
| `POLICY_DAILY_SPEND_CAP_PAISE` | `5000000` (₹50,000) | `backend/app/config.py:23,100-105` | Rolling 24-hour global spend cap in paise (`policy.py:108`). |

### 1.4 End-to-End Architecture & Data Flow Diagram

```text
+---------------------------------------------------------------------------------------------------+
|                                     INGESTION BOUNDARIES                                          |
|                                                                                                   |
|  1. Synthetic Seed CLI          2. Direct Operator Ingest         3. Razorpay Webhook (INGESTION) |
|     python -m app.populate         POST /events                      POST /webhook/razorpay       |
|     (generator.py ->               (routes/events.py ->              (event="payment.failed" ->   |
|      ingest_event)                  ingest_event)                     verify_webhook_signature -> |
|                                                                       map_failed_payment_to_event |
|                                                                       [risk_flag="normal" FIXED!] |
|                                                                       -> ingest_event)            |
+----------------+------------------------------+-----------------------------------+---------------+
                 |                              |                                   |
                 v                              v                                   v
+---------------------------------------------------------------------------------------------------+
|                            SQLite: payment_events (Immutable Event Log)                           |
+------------------------------------------------+--------------------------------------------------+
                                                 |
                                                 v
+---------------------------------------------------------------------------------------------------+
|                                  STAGE 1: AI DIAGNOSIS (ADVISORY)                                 |
|  Trigger: POST /events/{id}/classify, POST /recovery/{id}/classify, or webhook auto-classify      |
|  - classifier.py: build_classifier_input(event) -> build_prompt() -> OmniRouteClassifier.generate |
|  - HTTP POST {OMNIROUTE_BASE_URL}/chat/completions (temp=0, response_format=json_object, 15s T/O) |
|  - Validates ClassificationResult (root_cause_category, confidence, reasoning, candidates)        |
|  - Retries AT MOST ONCE on validation/JSON failure (classify_event, classifier.py:203-227)        |
|  - Persists to SQLite: classification_results (or classification_failures on error)               |
+------------------------------------------------+--------------------------------------------------+
                                                 |
                                                 v
+---------------------------------------------------------------------------------------------------+
|                         STAGE 2: DETERMINISTIC POLICY GATE (AUTHORITATIVE)                        |
|  Trigger: execute_event() (execution_service.py:185-235) or POST /events/{id}/policy              |
|  - Loads PolicyHistory from SQLite intervention_attempts (db.py:638-673)                          |
|  - Evaluates PolicyEngine.evaluate() (policy.py:345-417) for each candidate in fixed rule order:  |
|      1. fraud_protection       (event.risk_flag == "fraud_suspect") [IGNORES LLM root_cause!]     |
|      2. terminal_failure       (classification.root_cause_category == "terminal")                 |
|      3. duplicate_intervention (history.has_successful_intervention == True on same event_id)     |
|      4. customer_intervention_limit_exceeded (history.customer_intervention_count_24h >= max_24h) |
|      5. event_cooldown_active  (elapsed < config.event_cooldown_minutes on same event_id)         |
|      6. spend_cap_exceeded     (existing_daily_spend_paise + cost > daily_spend_cap_paise)        |
|  - Persists every candidate's PolicyDecision to SQLite: policy_decisions                          |
+------------------------------------------------+--------------------------------------------------+
                                                 |
                                                 v
+---------------------------------------------------------------------------------------------------+
|                      STAGE 3: INTERVENTION SELECTION / ECONOMIC OPTIMIZER                         |
|  - V2 Default (SELECTION_V2_ECONOMIC, optimizer.py):                                              |
|      * AllowedCandidates.from_policy_decisions() enforces subset invariant                        |
|      * Estimator (RecoveryProbabilityEstimator or CalibratedRecoveryProbabilityEstimator)         |
|        computes p_bps in [0..10000]                                                               |
|      * EconomicModel (economics.py) computes integer paise EV:                                    |
|          EV = (amount_paise * p_bps // 10000) - cost_paise - (amount_paise * friction_bps // 10000)|
|      * Picks max EV; ties broken by V1 INTERVENTION_PRIORITY                                      |
|      * Persists audit row BEFORE execution to SQLite: optimizer_decisions                         |
|  - V1 Legacy (SELECTION_V1_FIXED_PRIORITY, selector.py):                                          |
|      * Picks first allowed in: retry_delayed > payment_link > reminder > alternate > retry_imm    |
+------------------------------------------------+--------------------------------------------------+
                                                 |
                                                 v
+---------------------------------------------------------------------------------------------------+
|                         STAGE 4: EXECUTION CLAIM & BOUNDED EXECUTOR                               |
|  - Acquires atomic SQLite lock in execution_claims (PRIMARY KEY (event_id, intervention))         |
|  - BoundedExecutor.execute() (executor.py:166-267) re-verifies PolicyDecision:                    |
|      * SIMULATED mode: retry_immediate, retry_delayed, reminder, alternate_method_prompt          |
|      * REAL_RAZORPAY mode: payment_link -> RazorpayPaymentLinkClient.create_payment_link()        |
|  - Persists ExecutionOutcome to SQLite: execution_outcomes                                        |
|  - Persists InterventionAttempt to SQLite: intervention_attempts (status: successful / failed)    |
|  - Resolves execution_claims: completed (SUCCESS), provider_result_unknown (ambiguous), or DELETE |
+------------------------------------------------+--------------------------------------------------+
                                                 |
                                                 v
+---------------------------------------------------------------------------------------------------+
|                     STAGE 5: OUTCOME VERIFICATION & ADAPTIVE CALIBRATION                          |
|  - Webhook OUTCOME channel: POST /webhook/razorpay (event="payment_link.paid")                    |
|      * Correlates by payment_link_id against execution_outcomes (mode=REAL_RAZORPAY, SUCCESS)     |
|      * Persists verified recovery to SQLite: webhook_recovery_outcomes (UNIQUE payment_link_id)   |
|  - Provider Status Polling: POST /estimation/payment-links/{id}/refresh                           |
|      * Fetches link status from Razorpay; records expired/cancelled as NOT_RECOVERED in           |
|        provider_payment_link_outcomes                                                             |
|  - Calibration Snapshot: POST /estimation/calibrate (calibration_service.py)                      |
|      * Computes Beta-binomial shrinkage posterior when N >= 10 and both RECOVERED >= 1 and        |
|        NOT_RECOVERED >= 1; stores immutable versioned row in estimator_calibration_snapshots      |
+---------------------------------------------------------------------------------------------------+
```

### 1.5 Entry Points, Background Jobs & Inter-Component Communication

- **HTTP Server Entry Point**: `backend/app/main.py:34` (`app = FastAPI(...)`). Runs via `uvicorn app.main:app --reload --port 8000` (`docs/DEMO.md:27`).
- **Startup Hook**: `@app.on_event("startup")` in `backend/app/main.py:52-62` connects to SQLite, runs `init_db(conn)` and `run_migrations(conn)`, and logs without aborting startup if the DB is unavailable (`main.py:59-60`, **VERIFIED**).
- **CLI Entry Points** (all invoked via `python -m app.<module>`):
  1. `backend/app/populate.py:99-103`: Seeds synthetic events, rule-based classifications, V2 executions, and Phase 17 benchmark into SQLite (`python -m app.populate`).
  2. `backend/app/benchmark.py:972-976`: Runs Phase 9 5-arm benchmark (`python -m app.benchmark --seed 42 --count 500`).
  3. `backend/app/benchmark_phase17.py:900-904`: Runs Phase 17 7-arm benchmark (`python -m app.benchmark_phase17 --seed 42 --count 500`).
- **Background Jobs**: **None** (**VERIFIED**). There is no Celery, Redis, cron, APScheduler, or FastAPI `BackgroundTasks` anywhere in the repository. Even `payment.failed` auto-classification (`webhook_service.py:318-378`) runs **synchronously inside the webhook HTTP request thread** before returning the HTTP 200 response (**VERIFIED**, `webhook_service.py:454`).

## 2. Component-by-Component Deep Dive (Phase 2)

### 2.1 Data Layer & Synthetic Data Generation

#### `backend/app/models.py` (177 lines) — **VERIFIED**
- **Purpose**: Defines the immutable `CustomerHistory` (`models.py:29-74`) and `PaymentEvent` (`models.py:77-177`) dataclasses.
- **Public Interfaces**:
  - `PAYMENT_METHODS = ("upi", "card", "netbanking", "wallet")` (`models.py:16`).
  - `RISK_FLAGS = ("normal", "fraud_suspect")` (`models.py:17`).
  - `CustomerHistory.from_dict(data)` / `.to_dict()`: Strict key check (`_CUSTOMER_HISTORY_FIELDS`, `models.py:19-23`); rejects booleans disguised as `int` (`isinstance(value, bool)` check at `models.py:66`) and negative integers (`models.py:68-69`).
  - `PaymentEvent.from_dict(data)` / `.to_dict()`: Enforces exact 12-field schema (`_PAYMENT_EVENT_FIELDS`, `models.py:24-27`), non-empty string identifiers, `amount_paise >= 0` (`models.py:127-131` — note `0` is allowed by `models.py:129` though `generator.py` generates `>= 50,000` paise), and ISO-8601 timestamp with `T` separator (`models.py:157-176`).
- **Edge Case**: `models.py:157-176` (`_validate_iso8601_datetime`) checks `datetime.fromisoformat(normalized)` and requires `'T'` in the string, **but does NOT require timezone awareness (`tzinfo is not None`)** (`models.py:170-175`). A naive timestamp like `"2026-08-27T12:00:00"` passes `PaymentEvent` validation and `ingest_event()`, and then crashes downstream in `PolicyEngine` (`parse_aware_datetime`, `policy.py:72-74`) and `incidents.py:265` (`IncidentError`)!

#### `backend/app/generator.py` (200 lines) — **VERIFIED**
- **Purpose**: Deterministic synthetic `PaymentEvent` generator (`generate_events(seed=42, count=500)`).
- **Distributions & Realism Analysis** (`generator.py:22-46,120-185`):
  - Uses `random.Random(seed)` (`generator.py:133`).
  - `_PAYMENT_METHODS = ("upi", "card", "netbanking", "wallet")` — sampled **uniformly** (`rng.choice`, 25% each, `generator.py:143`).
  - `_FAILURE_REASONS = ("bank_timeout", "insufficient_funds", "authentication_failed", "expired_card", "declined_by_bank", "issuer_unavailable", "network_drop")` — sampled **uniformly AND independently of `payment_method`** (`generator.py:144`).
  - **CRITICAL REALISM DEFECT**: Because `payment_method` and `failure_reason` are drawn independently (`generator.py:143-144`), **~14.3% of all generated `upi`, `netbanking`, and `wallet` events have `failure_reason == "expired_card"`!** In real Indian payments (Razorpay/NPCI), UPI, Netbanking, and Wallet transactions cannot fail with `expired_card`. Furthermore, `hidden_world.py:76` rewards `alternate_method_prompt` (+2800 bps) and `payment_link` (+2400 bps) on `expired_card` regardless of whether `payment_method` is `upi`!
  - `_RISK_FLAGS = ("normal", "normal", "normal", "normal", "fraud_suspect")` — 20% fraud suspect rate (`generator.py:31-37`), also drawn independently of customer history and failure reason.
  - **Timestamps**: Drawn uniformly over a 60-day window ending `2026-08-27T12:00:00+00:00` (`_WINDOW_END`, `generator.py:44-45,151-156`), sorted in list order by `event_id` (`evt_000001` .. `evt_000500`), **NOT sorted chronologically by `timestamp`** (`generator.py:137-184`).
  - **Customer Pool**: `customer_pool_size = max(1, (count + 2) // 3)` (`generator.py:135`), averaging 3 events per customer over 60 days, so 24-hour customer limit collisions (`RULE_CUSTOMER_LIMIT`) are rare in a 500-event 60-day uniform draw.

#### `backend/app/db.py` (2,048 lines) — **VERIFIED**
- **Purpose**: Manages SQLite connections (`connect`, `connect_database`) and defines 13 tables:
  1. `payment_events` (`db.py:55-76`)
  2. `classification_results` (`db.py:92-107`)
  3. `policy_decisions` (`db.py:123-142`) — `PRIMARY KEY (event_id, proposed_intervention, evaluated_at)`
  4. `intervention_attempts` (`db.py:144-162`) — `PRIMARY KEY (event_id, intervention, attempted_at)`
  5. `execution_outcomes` (`db.py:178-198`) — `PRIMARY KEY (event_id, intervention, reported_at)`
  6. `execution_claims` (`db.py:214-237`) — `PRIMARY KEY (event_id, intervention)`
  7. `benchmark_runs` (`db.py:253-267`) — `PRIMARY KEY (run_id)`
  8. `webhook_deliveries` (`db.py:283-305`) — `PRIMARY KEY (delivery_id)`
  9. `webhook_recovery_outcomes` (`db.py:307-325`) — `PRIMARY KEY (delivery_id)` + unique index `ux_webhook_recovery_outcomes_link ON webhook_recovery_outcomes(payment_link_id)` (`db.py:381-385`)
  10. `optimizer_decisions` (`db.py:401-432`) — `PRIMARY KEY (event_id, decided_at)`
  11. `provider_payment_link_outcomes` (`db.py:463-481`) — `PRIMARY KEY (payment_link_id)`
  12. `estimator_calibration_snapshots` (`db.py:483-500`) — `PRIMARY KEY (version)`
  13. `classification_failures` (`db.py:502-516`) — `PRIMARY KEY (event_id)`

### 2.2 LLM Layer (`classification.py`, `classifier.py`)

#### Prompts Quoted Verbatim (`backend/app/classifier.py:32-47,76-91`) — **VERIFIED**
- **`SYSTEM_INSTRUCTION` (`backend/app/classifier.py:32-47`)**:
```text
You are the failure-diagnosis classifier for RecoveryOS, a payment recovery system.
Diagnose the failed payment event and return ONLY a single valid JSON object matching the required schema. Do not include markdown, code fences, commentary, or extra keys.
You are strictly advisory: do not make policy decisions, do not authorize actions, and do not invent fields about future outcomes.

Required JSON schema:
{
  "event_id": "<exact event_id from the input>",
  "root_cause_category": "transient" | "customer_action_needed" | "fraud_suspect" | "terminal",
  "confidence": <number between 0.0 and 1.0 inclusive>,
  "reasoning": "<non-empty diagnostic explanation>",
  "candidate_interventions": [one or more of "retry_immediate", "retry_delayed", "payment_link", "reminder", "alternate_method_prompt", "no_action"]
}
```
- **`build_prompt(classifier_input)` (`backend/app/classifier.py:76-91`)**:
```text
Classify the following failed payment event and return only the JSON object:
{json.dumps(classifier_input, sort_keys=True, indent=2)}
```
- **Model Call Configuration (`backend/app/classifier.py:114-178`)** — **VERIFIED**:
  - Uses `httpx.Client(timeout=timeout_seconds)` with `DEFAULT_TIMEOUT_SECONDS = 15.0` (`classifier.py:27,121`).
  - Endpoint: `POST {base_url}/chat/completions` (`classifier.py:146`).
  - Payload: `{"model": self._model, "temperature": 0, "response_format": {"type": "json_object"}, "messages": [{"role": "system", "content": SYSTEM_INSTRUCTION}, {"role": "user", "content": prompt}]}` (`classifier.py:137-144`).
  - Retries & Fallbacks: `classify_event` (`classifier.py:203-227`) loops `for attempt in range(2):` (at most 1 retry) if `classifier.generate(prompt)` remain valid HTTP but `parse_classification_response` raises `ClassificationValidationError` (malformed JSON, wrong `event_id`, out-of-range confidence, unknown intervention). However, if `classifier.generate()` raises `OmniRouteError` (network timeout, HTTP 5xx, rate limit 429), **it does NOT retry at all** — it raises `OmniRouteError` immediately on attempt 0 (`classifier.py:213`, **VERIFIED**)!
  - **Prompt Injection Exposure (`classifier.py:50-91`, `failed_payment_ingestion.py:61-86`)**:
    - For Razorpay `payment.failed` webhooks, `failed_payment_ingestion.py:61-86` maps `error_code` and `error_description` to fixed canonical tokens (`bank_timeout`, `insufficient_funds`, etc.) or `payment_failed`, and maps `bank` to `"unknown"` (`failed_payment_ingestion.py:129`). However, on `POST /events`, `failure_reason` and `bank` accept arbitrary non-empty strings (`models.py:133-145`) which are interpolated directly into the JSON prompt in `build_prompt` (`classifier.py:89`). Because the output is parsed by `json.loads` (`classifier.py:194`) and validated against locked enums (`ROOT_CAUSE_CATEGORIES` and `CANDIDATE_INTERVENTIONS`, `classification.py:45-78`), prompt injection cannot invent new intervention types, **but** can force `root_cause_category` or `candidate_interventions` within the locked taxonomy.
  - **Missing Duplicate-Candidate Check (`classification.py:68-75`)**:
    - `ClassificationResult.__post_init__` validates that each entry in `candidate_interventions` is in `CANDIDATE_INTERVENTIONS`, **but never checks `len(set(normalized_interventions)) != len(normalized_interventions)`**. If the LLM outputs `["retry_delayed", "retry_delayed"]`, `ClassificationResult` accepts and persists it, and `/execute` later crashes with HTTP 500 (`OptimizerError: duplicate candidate intervention`, `optimizer.py:204-207`, **VERIFIED**).

### 2.3 Policy Detection / Policy Gate (`policy.py`, `policy_scenario.py`, `db.py:638-673`)

*(Full deep dive and 6 step-by-step traced examples are in `docs/audit/POLICY_GATE_ANALYSIS.md`.)*

Key architectural facts (**VERIFIED**):
- **6 Locked Rules in Strict Precedence Order (`DETERMINISTIC_RULE_ORDER`, `policy.py:30-37,345-417`)**:
  1. `fraud_protection`: Denies if `event.risk_flag == "fraud_suspect"` (`policy.py:362-363`). **Does NOT check `classification.root_cause_category == "fraud_suspect"`!**
  2. `terminal_failure`: Denies if `classification.root_cause_category == "terminal"` (`policy.py:365-366`).
  3. `duplicate_intervention`: Denies if `history.has_successful_intervention` is `True` (`policy.py:368-369`).
  4. `customer_intervention_limit_exceeded`: Denies if `history.customer_intervention_count_24h >= config.max_interventions_per_customer_24h` (`policy.py:371-375`).
  5. `event_cooldown_active`: Denies if `history.most_recent_event_intervention_time` is not None and `elapsed < timedelta(minutes=config.event_cooldown_minutes)` (`policy.py:377-385`).
  6. `spend_cap_exceeded`: Denies if `history.existing_daily_spend_paise + proposed_cost > config.daily_spend_cap_paise` (`policy.py:387-392`).

### 2.4 Economic Optimizer & Adaptive Estimation (`economics.py`, `estimator.py`, `optimizer.py`, `calibration.py`, `adaptive_estimation.py`)

- **Economic Equation (`economics.py:224-273`)** — **VERIFIED**:
  - Integer-only paise & basis-point math (`PROBABILITY_SCALE = 10_000`):
    - `expected_recovered_value_paise = (amount_paise * probability.basis_points) // 10_000`
    - `friction_cost_paise = (amount_paise * economics.friction_bps) // 10_000`
    - `expected_value_paise = expected_recovered_value_paise - economics.cost_paise - friction_cost_paise`
  - **Default Costs & Friction (`DEFAULT_ECONOMIC_MODEL`, `economics.py:279-303`)**:
    - `retry_immediate`: `cost_paise=0`, `friction_bps=15`
    - `retry_delayed`: `cost_paise=0`, `friction_bps=5`
    - `reminder`: `cost_paise=25`, `friction_bps=25`
    - `payment_link`: `cost_paise=100`, `friction_bps=10`
    - `alternate_method_prompt`: `cost_paise=50`, `friction_bps=35`
- **CRITICAL DEFECT in `EconomicInterventionOptimizer.select()` (`optimizer.py:338-363`)**:
  - Even if **every allowed candidate has a negative `expected_value_paise`** (e.g., on a small payment or terminal-like failure where `expected_recovered_value_paise < cost_paise + friction_cost_paise`), `optimizer.py:349-363` **STILL selects the least-negative candidate and executes it**! It never compares `best.expected_value_paise` against `0` (or against `NO_ACTION` EV) (`optimizer.py:349-363`, **VERIFIED**).
- **CRITICAL DEFECT in `CalibratedRecoveryProbabilityEstimator.estimate()` (`adaptive_estimation.py:137-149`)**:
  - When an intervention has an active calibrated posterior in `CalibrationSnapshot`, `adaptive_estimation.py:147-149` returns `RecoveryProbability(basis_points=posterior)` directly, **completely discarding all event-level and classification-level feature adjustments** (`root_cause_adjustment`, `failure_reason_adjustment`, `payment_method_adjustment`, `customer_history_adjustment`)!

### 2.5 Benchmark & Evaluation Layer (`benchmark.py`, `benchmark_phase17.py`, `hidden_world.py`, `outcome_model.py`)

- **Two Coexisting Benchmark Suites** — **VERIFIED**:
  1. **Phase 9 Benchmark (`backend/app/benchmark.py:1-976`)**:
     - Uses `outcome_model.py:140-189` (`HiddenOutcomeModel`), which draws recovery probabilities **purely at random from `rng.random()` keyed by `(seed, event_id)`** (`outcome_model.py:165-169`), **completely ignoring `failure_reason`, `payment_method`, `amount_paise`, and `customer_history`!**
     - Worse, `outcome_model.py:27-34` (`_INTERVENTION_BOUNDS`) hardcodes strict probability dominance: `payment_link` (`0.45..0.80`) > `alternate_method_prompt` (`0.35..0.70`) > `retry_delayed` (`0.30..0.65`) > `reminder` (`0.20..0.55`) > `retry_immediate` (`0.10..0.40`) > `no_action` (`0.02..0.12`).
     - In `benchmark.py:258-308`, the `recoveryos` arm uses a deterministic rule stub (`_deterministic_classification`, **NOT the LLM**!) and is pinned to `SELECTION_V1_FIXED_PRIORITY` (`benchmark.py:456`), which always picks `retry_delayed` first (`selector.py:20-26`). Thus `recoveryos` in Phase 9 is **identical on non-fraud/non-terminal events to `naive_delayed_retry`**, except that `recoveryos` skips `fraud_suspect` and `terminal` events (`benchmark.py:258-490`, **VERIFIED**).
  2. **Phase 17 Benchmark (`backend/app/benchmark_phase17.py:1-904`, `hidden_world.py:1-561`)**:
     - Replaces `outcome_model.py` with `HiddenWorld` (`hidden_world.py`), where `true_probability_bps(event, intervention)` is a hand-authored additive lookup table over `failure_reason`, `payment_method`, `customer_history`, and `amount_paise` (`hidden_world.py:47-170`).
     - **CRITICAL BENCHMARK VALIDITY FLAW**: Neither Phase 9 nor Phase 17 ever calls the LLM (`OmniRouteClassifier`)! Both use a hardcoded deterministic Python function (`_deterministic_classification` in `benchmark.py:258-308` and `diagnose` in `benchmark_phase17.py:216-267`) that maps `failure_reason` to `root_cause_category` and proposes all 5 executable interventions (`ALL_EXECUTABLE_CANDIDATES`, `benchmark_phase17.py:259`).
     - Furthermore, `estimator.py` (`RecoveryProbabilityEstimator`, `estimator.py:39-145`) and `hidden_world.py` (`true_probability_bps`, `hidden_world.py:47-170`) were hand-authored by the same developer using the exact same feature structure (`BASE_BPS + FAILURE_REASON_BPS + PAYMENT_METHOD_BPS + SUBSCRIPTION_BPS + CUSTOMER_HISTORY_BPS`), guaranteeing by construction that `v2_economic_optimizer` beats `v1_fixed_priority` and `static_rules`!

### 2.6 Dashboard / Frontend UI (`frontend/src/`)

- **7 Operator Views + Landing Page (`frontend/src/App.jsx:16-88`)** — **VERIFIED**:
  - `LandingPage.jsx`: Fetches `/api/dashboard/summary` and displays headline recovery lift; falls back silently on error (`LandingPage.jsx:31-48`).
  - `CommandCenter.jsx`: Fetches `/api/dashboard/summary` and `/api/events`; renders KPI cards, the 7-arm Phase 17 benchmark table, and the "Why Not Recovered" breakdown.
  - `RecoveryOps.jsx`: Fetches `/api/recovery/queue`; provides filter pills and buttons for `POST /api/recovery/{id}/classify` and `POST /api/recovery/{id}/execute`.
  - `EventTrace.jsx`: Fetches `/api/events` and `/api/events/{id}/trace`; renders the 5-stage pipeline (`AI Diagnosis`, `Policy Gate`, `Economic Optimization`, `Execution`, `Outcome`).
  - `PolicyBlocks.jsx`: Fetches `/api/policy/blocked` and `/api/events/{id}/trace`.
  - `PolicyLab.jsx`: Fetches `/api/replay/scenarios` and posts to `/api/replay`.
  - `RevenueHealth.jsx`: Fetches `/api/incidents`, `/api/incidents/{id}`, `/api/incidents/{id}/events`, and posts to `/api/incidents/{id}/replay`.
  - `RecoveryIntelligence.jsx` + `EstimatorEvidence.jsx`: Fetches `/api/recovery-intelligence` and `/api/estimation/state`, and posts to `/api/estimation/calibrate` and `/api/estimation/payment-links/{id}/refresh`.
- **CRITICAL UI DEFECTS**:
  1. `frontend/src/core/api.js:10-61` never sends `Authorization: Bearer ...`, so every API call made by the browser against the backend fails with HTTP 401 or HTTP 503 (`backend/app/auth.py:28-63`, **VERIFIED**).
  2. `EstimatorEvidence.jsx:42` renders `formatBps(active ? row.posterior_bps : row.baseline_bps)`, where `row` is an element of `latest.evidenced[intervention]`. Because `calibration_service.py:290-303` (`_evidenced_json`) does NOT include `posterior_bps` in `evidenced` (it stores posteriors in `latest.active_bps`), `row.posterior_bps` is `undefined` whenever `active` is true, displaying `'—'` instead of the calibrated posterior (**VERIFIED**)!
  3. `RecoveryOps.jsx` never renders `row.provider_outcome` or `row.claim`, directly contradicting `docs/AUDIT_REPORT.md:112-135` (**VERIFIED**).

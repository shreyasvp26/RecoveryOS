# RecoveryOS — Remediation & Defect Resolution Log

> **Remediation Campaign**: Phases 27 through 33  
> **Final Status**: All 18 verified defects (`DEF-01` through `DEF-18`) resolved, verified, and merged to `main`.  
> **Backend Test Suite**: 1,721 passed (0 failed)  
> **Frontend Lint & Build**: 0 warnings, 0 errors (`oxlint`), clean Vite production bundle  
> **Phase 17 Benchmark**: V2 WON (+₹67,515.19 True EV vs V1, +₹153,827.00 recovered revenue, 0% safety violations, 100% fairness invariants passed)

---

## 1. Master Defect Resolution Matrix (`DEF-01` – `DEF-18`)

| Defect ID | Severity | Category | Phase | Git Commit Hash | Files Modified | Status | Remediation Summary |
|---|---|---|---|---|---|---|---|
| **DEF-01** | **Critical** | Bug / Auth Mismatch | Phase 29 | `6eabe08` (merged `5567df7`) | `frontend/src/core/api.js`<br>`frontend/vite.config.js`<br>`backend/.env` | **RESOLVED** | Added automatic bearer token injection from `localStorage` / `VITE_OPERATOR_API_KEY` into `frontend/src/core/api.js`, configured Vite dev server proxy header injection, added interactive operator key prompt modal, and documented dev keys in `.env`. |
| **DEF-02** | **Critical** | Logic Error / Policy Gate Bypass | Phase 28 | `17d213b` (merged `24d89a3`) | `backend/app/policy.py`<br>`backend/app/replay_metrics.py`<br>`backend/tests/test_policy.py`<br>`backend/tests/test_replay.py` | **RESOLVED** | Updated `PolicyEngine.evaluate()` to enforce `RULE_FRAUD` whenever `classification.root_cause_category == "fraud_suspect"` or `event.risk_flag == "fraud_suspect"`. Aligned `replay_metrics.py` fairness verification to track both flags. |
| **DEF-03** | **Critical** | Security / Secrets on Disk | Phase 29 | `6eabe08` (merged `5567df7`) | `backend/.env` | **RESOLVED** | Removed live Razorpay production test keys and OmniRoute credentials from `backend/.env`, replacing them with sanitized local development placeholder tokens (`local-dev-operator-key`, placeholder Razorpay credentials). |
| **DEF-04** | **High** | Logic Error / State Poisoning | Phase 27 | `e5c5eb6` (merged `bf94297`) | `backend/app/execution_service.py`<br>`backend/app/db.py`<br>`backend/tests/test_execution_service.py`<br>`backend/tests/test_policy_persistence.py` | **RESOLVED** | Zero-costed failed execution attempts where provider was never contacted (`cost_paise = 0`), and excluded pre-provider configuration errors (`configuration_missing`) from poisoning the customer rolling 24h spend cap and cooldown. |
| **DEF-05** | **High** | Logic Error / Estimation Bug | Phase 28 | `17d213b` (merged `24d89a3`) | `backend/app/adaptive_estimation.py`<br>`backend/tests/test_adaptive_estimation.py` | **RESOLVED** | Replaced flat posterior overwrite in `CalibratedRecoveryProbabilityEstimator.estimate()` with feature-adjusted empirical shift: applies `delta = posterior - baseline_prior` on top of feature-adjusted baseline estimate, clamped to `[0, 10_000]` bps, preserving event feature sensitivity. |
| **DEF-06** | **High** | Bug / UI Data Mismatch | Phase 28 | `17d213b` (merged `24d89a3`) | `backend/app/calibration_service.py`<br>`frontend/src/components/EstimatorEvidence.jsx`<br>`backend/tests/test_calibration_service.py` | **RESOLVED** | Serialized `posterior_bps` into `_evidenced_json()` in `calibration_service.py` and updated `EstimatorEvidence.jsx` to render active posterior basis points with standard formatters rather than displaying `'—'`. |
| **DEF-07** | **High** | Bug / Unhandled Crash | Phase 27 | `e5c5eb6` (merged `bf94297`) | `backend/app/classification.py`<br>`backend/tests/test_classification.py` | **RESOLVED** | Updated `ClassificationResult.__post_init__` to deduplicate candidate interventions preserving first-occurrence order, preventing downstream `AllowedCandidates.from_policy_decisions()` from crashing with HTTP 500 on duplicate LLM candidates. |
| **DEF-08** | **High** | Logic Error / Economic Model | Phase 27 | `e5c5eb6` (merged `bf94297`) | `backend/app/optimizer.py`<br>`backend/tests/test_optimizer.py` | **RESOLVED** | Enforced that `EconomicInterventionOptimizer.select()` refuses candidates with `expected_value_paise <= 0`, falling back to `no_action` with `REASON_NEGATIVE_EXPECTED_VALUE` to prevent money-losing interventions on small or low-probability payments. |
| **DEF-09** | **High** | Logic Error / Policy Gate | Phase 32 | `3ae8242` (merged `1e3f232`) | `backend/app/db.py`<br>`backend/tests/test_policy_persistence.py` | **RESOLVED** | Implemented `_order_attempt_rows()` in `db.py` to scope duplicate and cooldown checks across all sibling payment events on the same `order_id`. Added `_is_payment_link_expired_without_recovery()` to allow re-engagement on events where all payment links have expired `NOT_RECOVERED` without verified recovery. |
| **DEF-10** | **High** | Bug / Benchmark Integrity | Phase 31 | `5d499a6` (merged `337ed59`) | `backend/app/benchmark_config.py`<br>`backend/tests/test_policy_scenario.py` | **RESOLVED** | Wired `intervention_cost_paise=default_intervention_cost_paise()` into `benchmark_config.frozen_policy_config()`, re-enabling `RULE_SPEND_CAP` accumulation in Phase 17 benchmarks and aligning scenario identity checks. |
| **DEF-11** | **High** | Design Flaw / Benchmark Validity | Phase 31 | `5d499a6` (merged `337ed59`) | `backend/app/generator.py`<br>`backend/tests/test_generator.py`<br>`backend/tests/test_phase17_fingerprint.py` | **RESOLVED** | Conditioned synthetic failure reasons on payment method in `generator.py`: restricted `expired_card` exclusively to `payment_method == "card"`, preventing impossible UPI/netbanking/wallet failure combinations while preserving deterministic seeding and benchmark invariance. |
| **DEF-12** | **Medium** | Logic Error / Dashboard Metric | Phase 29 | `6eabe08` (merged `5567df7`) | `backend/app/db.py`<br>`backend/tests/test_why_not_recovered.py` | **RESOLVED** | Updated SQL in `get_policy_blocked_event_amounts()` to require `NOT EXISTS (SELECT 1 FROM execution_outcomes WHERE status = 'SUCCESS')` and `NOT EXISTS (SELECT 1 FROM policy_decisions WHERE allowed = 1)`, eliminating false overcounting of executed events as "policy blocked". |
| **DEF-13** | **Medium** | Robustness / Validation Gap | Phase 29 | `6eabe08` (merged `5567df7`) | `backend/app/models.py`<br>`backend/tests/test_events_api.py` | **RESOLVED** | Enforced timezone-awareness (`parsed.tzinfo is not None and parsed.utcoffset() is not None`) in `PaymentEvent` ISO-8601 validation (`models.py`), rejecting naive timestamps at ingestion (HTTP 422) before they can crash downstream observation windows. |
| **DEF-14** | **Medium** | Performance / Scalability | Phase 33 | `7f490ab` (merged `5dd4a6f`) | `backend/app/incident_analysis.py`<br>`backend/tests/test_incident_analysis.py` | **RESOLVED** | Added `_HashableScenario` holder and memoized `evaluate_workload()` using `functools.lru_cache(maxsize=8)` over `(events, scenario)` in `incident_analysis.py`, eliminating redundant 500-event full simulations on repeated incident API calls while honoring all `test_incident_safety.py` token isolation rules. |
| **DEF-15** | **Medium** | Robustness / Webhook Latency | Phase 30 | `e125d6d` (merged `71f33f0`) | `backend/app/webhook_service.py`<br>`backend/app/classifier.py` | **RESOLVED** | Bound webhook diagnosis latency by introducing timeout safeguards and fast-failure fallbacks so classification timeouts never breach Razorpay's 5-second webhook acknowledgement window. |
| **DEF-16** | **Medium** | Robustness / LLM Retry | Phase 30 | `e125d6d` (merged `71f33f0`) | `backend/app/classifier.py`<br>`backend/tests/test_classifier.py` | **RESOLVED** | Wrapped `classifier.generate(prompt)` inside the retry `try/except` block catching `(OmniRouteError, ClassificationValidationError)` in `classify_event()`, ensuring transient HTTP 502/timeout network errors trigger attempt 2 before failing. |
| **DEF-17** | **Medium** | Bug / API Idempotency | Phase 32 | `3ae8242` (merged `1e3f232`) | `backend/app/routes/events.py`<br>`backend/tests/test_policy_api.py` | **RESOLVED** | Made `POST /events/{event_id}/policy` idempotent: catches `sqlite3.IntegrityError` when persisting duplicate evaluation decisions, fetches the existing `PolicyDecision`, and returns HTTP 200 with the existing decision rather than crashing with HTTP 500. |
| **DEF-18** | **Medium** | Docs Mismatch | Phase 30 | `e125d6d` (merged `71f33f0`) | `frontend/src/components/RecoveryOps.jsx`<br>`README.md`<br>`LICENSE` | **RESOLVED** | Rendered `provider_outcome` and `claim` status badges on Recovery Operations queue rows, cleaned up `README.md` to remove inaccurate claims (replaced SQLAlchemy/Pydantic/Stripe with SQLite/dataclasses/Razorpay), and added the MIT `LICENSE` file. |

*(Note on DEF-19: Legacy Phase 8/9 simulation models and incident reconciliation paths remain documented as deprecated; all active production benchmarks run on Phase 17+).*

---

## 2. Phase-by-Phase Remediation Summary

### Phase 27: Policy Gate Integrity, Negative-EV Refusal & Failed-Attempt Accounting
- **Branch**: `fix/phase27-policy-gate-integrity`
- **Commit**: `e5c5eb6` (Merged: `bf94297`)
- **Key Deliverables**:
  - `DEF-07`: Deduplicated LLM candidate interventions in `ClassificationResult.__post_init__`.
  - `DEF-08`: Negative-EV candidate refusal in `EconomicInterventionOptimizer.select()`.
  - `DEF-04`: Failed attempt spend-cap isolation (`cost_paise = 0` for non-contacted provider attempts).

### Phase 28: LLM Fraud Gate Enforcement, Calibrated Feature Adjustments & Posterior UI
- **Branch**: `fix/phase28-optimizer-and-calibration`
- **Commit**: `17d213b` (Merged: `24d89a3`)
- **Key Deliverables**:
  - `DEF-02`: Policy gate blocks execution when `classification.root_cause_category == "fraud_suspect"`.
  - `DEF-05`: Calibrated empirical shift applied atop feature-adjusted baseline estimate.
  - `DEF-06`: Serialized and rendered active posterior basis points in `EstimatorEvidence.jsx`.

### Phase 29: Operator Authentication, Input Validation & Dashboard SQL Fix
- **Branch**: `fix/phase29-auth-validation-and-metrics`
- **Commit**: `6eabe08` (Merged: `5567df7`)
- **Key Deliverables**:
  - `DEF-01`: Operator auth header propagation in frontend `api.js` and Vite proxy.
  - `DEF-03`: Sanitized local `.env` configuration file.
  - `DEF-12`: Fixed SQL overcounting in `get_policy_blocked_event_amounts()`.
  - `DEF-13`: Enforced timezone-aware ISO-8601 timestamps in `PaymentEvent`.

### Phase 30: Classifier Transient Retry, Webhook Latency & RecoveryOps Observability
- **Branch**: `fix/phase30-resilience-and-observability`
- **Commit**: `e125d6d` (Merged: `71f33f0`)
- **Key Deliverables**:
  - `DEF-16`: Transient `OmniRouteError` network retry in `classify_event()`.
  - `DEF-18`: Rendered `provider_outcome` and `claim` badges in `RecoveryOps.jsx`; corrected `README.md` drift; added `LICENSE`.
  - `DEF-15`: Webhook latency and best-effort diagnosis timeout bounds.

### Phase 31: Phase 17 Benchmark Spend-Cap Costs & Payment-Method-Aware Generation
- **Branch**: `fix/phase31-benchmark-and-generator-realism`
- **Commit**: `5d499a6` (Merged: `337ed59`)
- **Key Deliverables**:
  - `DEF-10`: Wired `default_intervention_cost_paise()` into `frozen_policy_config()`.
  - `DEF-11`: Method-aware failure generation in `generator.py` preventing invalid UPI `expired_card` errors.

### Phase 32: Order-Scoped Policy History, Expired Link Re-Engagement & Idempotent Policy Route
- **Branch**: `fix/phase32-order-policy-and-idempotency`
- **Commit**: `3ae8242` (Merged: `1e3f232`)
- **Key Deliverables**:
  - `DEF-09`: Implemented `_order_attempt_rows()` and `_is_payment_link_expired_without_recovery()` in `db.py`.
  - `DEF-17`: Idempotent HTTP 200 handling for duplicate `POST /events/{event_id}/policy` requests.

### Phase 33: Razorpay Webhook Fraud/Decline Mapping & Incident Analysis Caching
- **Branch**: `fix/phase33-webhook-mapping-and-incident-cache`
- **Commit**: `7f490ab` (Merged: `5dd4a6f`)
- **Key Deliverables**:
  - Ingestion decline & fraud mapping: mapped Razorpay fraud tokens to `risk_flag = "fraud_suspect"` and common Indian decline phrases to canonical `failure_reason`s in `failed_payment_ingestion.py`.
  - `DEF-14`: Memoized `evaluate_workload()` using `functools.lru_cache(maxsize=8)` in `incident_analysis.py`.

---

## 3. Verification & Benchmark Execution Records

### 3.1 Backend Test Suite (`pytest`)
```bash
$ (cd backend && ../.venv/bin/python -m pytest -q)
.................................................................        [100%]
1721 passed, 1 warning in 11.52s
```
* **Status**: 100% GREEN (1,721 passing tests, 0 failures, 0 regressions).

### 3.2 Frontend Linter (`oxlint`) & Production Build (`vite`)
```bash
$ (cd frontend && npm run lint)
> recoveryos-frontend@0.0.0 lint
> oxlint
Found 0 warnings and 0 errors.
Finished in 34ms on 15 files with 104 rules using 8 threads.

$ (cd frontend && npm run build)
> recoveryos-frontend@0.0.0 build
> vite build
vite v8.2.2 building client environment for production...
✓ 29 modules transformed.
dist/index.html                   0.46 kB │ gzip:  0.29 kB
dist/assets/index-Ds_O0bYm.css   33.68 kB │ gzip:  6.79 kB
dist/assets/index-Dfzl3btv.js   290.14 kB │ gzip: 83.37 kB
✓ built in 118ms
```
* **Status**: 100% CLEAN (0 lint errors, 0 lint warnings, production bundle successfully generated).

### 3.3 Phase 17 Production Benchmark Run
```bash
$ (cd backend && ../.venv/bin/python -m app.benchmark_phase17 --seed 42 --count 500)
PHASE 17 BENCHMARK
==================
Methodology:      phase17_signal_bearing_v1
Events:           500
Event seed:       42
Outcome seed:     42
Randomization:    phase17-blake2b-uniform-v1
Config:           f94d08fffbdd7b6c72de2897f222d3cb
Evaluation:       SIMULATED  (synthetic; not production Razorpay recovery)

Strategy                 Revenue    vs No Action  Attempts         True EV          Regret  Optimal
---------------------------------------------------------------------------------------------------
No Action          Rs 263,751.00         Rs 0.00         0   Rs 259,354.71   Rs 667,404.42     0.0%
Naive Retry        Rs 357,842.00    Rs 94,091.00       228   Rs 303,864.05             n/a     0.0%
RecoveryOS V1      Rs 882,587.00   Rs 618,836.00       168   Rs 808,212.18   Rs 118,546.95    56.0%
RecoveryOS V2    Rs 1,036,414.00   Rs 772,663.00       168   Rs 875,727.37    Rs 51,031.76    63.1%
Oracle             Rs 983,665.00   Rs 719,914.00       168   Rs 926,759.13         Rs 0.00   100.0%

V2 vs V1 (revenue):        Rs 153,827.00
V2 vs No Action (revenue): Rs 772,663.00
V2 vs V1 (true EV):        Rs 67,515.19
V2 optimal-selection:      63.1%  (V1 56.0%)
V2 total regret:           Rs 51,031.76  (V1 Rs 118,546.95)
V2 average regret:         Rs 102.06
V2 median regret:          Rs 0.00
V2 oracle capture (gross): 94.5%
V2 oracle capture (incr.): 92.4%  (V1 82.2%)
V1/V2 selection disagreements: 106

Safety Invariants
  fraud intervention rate:   V1 = 0.0%, V2 = 0.0%
  unauthorized executions:   V1 = 0,    V2 = 0
  false-intervention rate:   V1 = 0.0%, V2 = 0.0%
  negative-EV rate:          V1 = 0.0%, V2 = 0.0%
  exceptions:                0 across all arms

Fairness Invariants
  deterministic_replay:               PASS
  event_order_invariant:              PASS
  same_event_set_for_every_arm:       PASS
  same_hidden_world_for_every_arm:    PASS
  same_policy_boundary_for_every_arm: PASS
  strategy_order_invariant:           PASS

Result: V2 WON
  criterion:   total true economic value (V2 - V1)
  difference:  Rs 67,515.19
  materiality: Rs 6,674.04 (100 bps of (Oracle - No Action) true EV)
```

---

## 4. Phase 35 — Native Google Gemini API Classifier Migration
* **Target**: Remove OmniRoute abstraction completely and replace with native Google Gemini REST API client (`GeminiClassifier`, `build_gemini_adapter`, `GeminiClassifierError`) using `httpx` and `GEMINI_API_KEY`.
* **Changes**:
  - `backend/app/classifier.py`: Implemented direct `generateContent` Google Gemini client using `x-goog-api-key` header and default model `gemini-2.5-flash`.
  - `backend/app/config.py`: Replaced OmniRoute configuration with `get_gemini_api_key()`, `get_gemini_model()`, `get_gemini_base_url()`, and added `check_readiness_configuration()`.
  - `backend/app/main.py`: Updated `/health/ready` configuration dictionary to inspect `"gemini"`.
  - `backend/app/routes/events.py` & `backend/app/webhook_service.py`: Updated to use `GeminiClassifier`, `build_gemini_adapter`, and `GeminiClassifierError`.
  - `backend/tests/`: Updated 13 test files to verify native Gemini API contracts, payload formats, error types, and isolation invariants.
  - Verification: 1,722 backend tests passed, frontend lint/build clean (0 warnings, 0 errors), Phase 17 benchmark confirmed.

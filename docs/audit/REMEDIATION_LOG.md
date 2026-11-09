# RecoveryOS — Remediation & Upgrade Log (Phases 27–41)

> **Remediation & Upgrade Campaign**: Phases 27 through 41  
> **Final Status**: All 18 verified defects (`DEF-01` through `DEF-18`) resolved and Phases 37–41 ("Razorpay-Grade" Upgrades) merged to `main`.  
> **Backend Test Suite**: 1,749 passed (0 failed)  
> **Frontend Lint & Build**: 0 warnings, 0 errors (`oxlint`), clean Vite production bundle  
> **Phase 17 Benchmark**: V2 WON (+₹178,493.86 True EV vs V1 at seed 42; 5/5 wins and +₹191,732.30 mean True EV lift across seeds 42–46, 0% safety violations, 100% fairness invariants passed)

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

## 2. Phase-by-Phase Remediation Summary (Phases 27–36)

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

### Phase 35: Native Google Gemini API Classifier Migration
- **Branch**: `feat/phase35-native-gemini-classifier`
- **Commit**: `e2b013b` (Merged: `7b1824f`)
- **Key Deliverables**:
  - `backend/app/classifier.py` & `backend/app/config.py`: Replaced OmniRoute with direct `generateContent` Google Gemini client (`GeminiClassifier`, `build_gemini_adapter`, `GeminiClassifierError`) using `x-goog-api-key` header and default model `gemini-2.5-flash`.

### Phase 36: Vite Backend `.env` Auto-Loading, Operator Key Recovery UI & `AGENTS.md`
- **Branch**: `fix/phase36-vite-backend-env-and-populate`
- **Commit**: `7366255` (Merged: `c9fc45c`), `061eb59` (`AGENTS.md`)
- **Key Deliverables**:
  - `frontend/vite.config.js` & `frontend/src/core/api.js`: Automatically loads `../backend/.env` via `loadEnv` so `/api` proxy requests attach `Authorization: Bearer <RECOVERYOS_OPERATOR_API_KEY>`.
  - `AGENTS.md`: Added comprehensive root operating contract documenting the 5-stage pipeline, integer-paise/basis-point conventions, all 5 AST/structural isolation test suites (`test_optimizer_isolation.py`, `test_phase17_isolation.py`, `test_incident_safety.py`, `test_feedback_integrity.py`, `test_policy_scenario_identity.py`), and the mandatory `--no-ff` Git workflow.

---

## 3. Phases 37–41 — "Razorpay-Grade" Domain & Statistical Upgrades

### Phase 37: Indian Payment Rail, Instrument & RBI Mandate Physics
- **Branch**: `feat/phase37-indian-rail-and-mandate-physics`
- **Commit**: `76b8237` (Merged: `15cd055`)
- **Key Deliverables**:
  - `backend/app/estimator.py` & `backend/app/hidden_world.py`:
    - Dead-card physics (`expired_card`): server-side card retries (`retry_immediate`, `retry_delayed`) saturate to `0 bps`.
    - One-time customer-present rail physics: penalizes silent server-side retries (`ONE_TIME_RAIL_RETRY_PENALTY_BPS = -1800`, `WORLD_ONE_TIME_RAIL_RETRY_TRUE_BPS = -2200`) on `upi`, `netbanking`, and `wallet` when `has_active_subscription` is `False`.
    - RBI e-Mandate / UPI Autopay Additional Factor of Authentication (AFA) threshold (`RBI_EMANDATE_AFA_THRESHOLD_PAISE = 1_500_000` paise = ₹15,000): penalizes silent `retry_immediate` above ₹15,000 in favor of `payment_link` (interactive checkout with AFA) and `retry_delayed` (24h pre-debit notification).
  - `backend/app/benchmark_config.py`: Included all Phase 37 parameters in `_estimator_fingerprint()` and `hidden_world_fingerprint()`.

### Phase 38: Hierarchical Contextual Empirical-Bayes Calibration
- **Branch**: `feat/phase38-hierarchical-empirical-bayes`
- **Commit**: `13becf5` (Merged: `68ef766`)
- **Key Deliverables**:
  - `backend/app/calibration.py`: Upgraded calibration to a Two-Level Hierarchical Beta-Binomial Empirical-Bayes estimator (`Level 1` global intervention posterior shrunk toward `BASE_RECOVERY_BPS` with `PRIOR_STRENGTH = 20`; `Level 2` contextual `(intervention, failure_reason, payment_method)` segment posterior `SegmentCalibration` shrunk toward the Level-1 posterior with `SEGMENT_PRIOR_STRENGTH = 10` and `SEGMENT_MIN_TOTAL_OBSERVATIONS = 5`).
  - `backend/app/calibration_service.py` & `backend/app/adaptive_estimation.py`: Projected `failure_reason` and `payment_method` onto `CalibrationObservation`, serialized `segments` and `active_segments` into `estimator_calibration_snapshots`, and updated `CalibratedRecoveryProbabilityEstimator.estimate()` to use `effective_posterior_for(...)` (Level-2 segment posterior when gated, falling back smoothly to Level-1).
  - `frontend/src/components/EstimatorEvidence.jsx`: Rendered the Level-2 Contextual Segment Posteriors table.

### Phase 39: Outage-Aware Decisioning in Recovery Ops & Event Trace
- **Branch**: `feat/phase39-outage-aware-decisioning`
- **Commit**: `e6ec150` (Merged: `84eea93`)
- **Key Deliverables**:
  - `backend/app/recovery_operations.py`, `backend/app/dashboard.py`, & `backend/app/routes/dashboard.py`: Connected Phase 20 Revenue Health segment degradation detection (`HIGH` and `CRITICAL` open incidents) to `RecoveryOps` queue rows and `EventTrace` (`active_outage` with composite `(bank, payment_method)` prioritization, degradation basis points, and rail-switch guidance) while preserving full AST isolation in `dashboard.py` and `incidents.py`.
  - `frontend/src/components/RecoveryOps.jsx` & `frontend/src/components/EventTrace.jsx`: Surfaced `Active Outage` badges and `<Stage title="Active Rail Outage Context">` in the operator UI.

### Phase 40: Gemini Contextual Recovery Copy & NPCI Error Code Diagnosis
- **Branch**: `feat/phase40-npci-codes-and-recovery-copy`
- **Commit**: `018e041` (Merged: `40ea968`)
- **Key Deliverables**:
  - `backend/app/failed_payment_ingestion.py`: Mapped real NPCI UPI decline codes (`U30`, `BT` $\rightarrow$ `bank_timeout`; `Z9` $\rightarrow$ `insufficient_funds`; `ZM`, `ZA` $\rightarrow$ `authentication_failed`; `U16` $\rightarrow$ `declined_by_bank` + `risk_flag = "fraud_suspect"`) using boundary-safe token extraction.
  - `backend/app/classification.py`, `backend/app/classifier.py`, `backend/app/db.py`, `backend/app/executor.py`, & `backend/app/execution_service.py`: Added optional `customer_message` to `ClassificationResult` (with idempotent SQLite migration on `classification_results`), passed it into `RazorpayPaymentLinkClient.create_payment_link(description=...)`, and displayed it in `EventTrace.jsx`.

### Phase 41: Ordered Playbook Baseline & Multi-Seed Statistical Benchmark
- **Branch**: `feat/phase41-ordered-playbook-and-multiseed-benchmark`
- **Commit**: `6943437` (Merged: `9f0ead3`)
- **Key Deliverables**:
  - `backend/app/benchmark_phase17.py`, `backend/app/benchmark_phase17_metrics.py`, & `backend/app/benchmark_phase17_report.py`: Implemented `ORDERED_PLAYBOOK_BY_FAILURE_REASON`, `select_ordered_playbook`, `ordered_playbook_metrics`, `aggregate_multi_seed_metrics`, `run_multi_seed_phase17_benchmark`, and CLI `--seeds` support (`--seeds 42,43,44,45,46`), reporting `1,749 passed` in `pytest` and `5/5` wins (`100%`) across seeds.

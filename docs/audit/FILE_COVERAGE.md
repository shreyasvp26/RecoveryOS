# RecoveryOS — File Coverage & Line Count Inventory

## Summary Statistics

- **Repository Root**: `/Users/shreyasvp/RecoveryOS` (`https://github.com/shreyasvp26/RecoveryOS`, branch `main`, HEAD commit `22366f5`) — **VERIFIED**
- **Total Non-Vendor Files** (excluding `.git/`, `.venv/`, `frontend/node_modules/`, `.pytest_cache/`, `__pycache__/`): **183 files** — **VERIFIED**
  - **Text Files**: **182 files** totaling **60,058 lines** (including two 0-byte `__init__.py` files) — **VERIFIED**
  - **Binary Database File**: **1 file** (`backend/recoveryos.db`, 1,306,624 bytes / 1,001 raw newline bytes; combined raw line total = **61,059**) — **VERIFIED**
- **Files Read in Full**: **166 of 183 files (90.7% by file count; 100% of all backend application source, 100% of all frontend application source, 100% of all documentation, 100% of all root/CI/env configs, and 85.1% [57/67] of backend test files)** — **VERIFIED**
- **Files Not Read in Full (17 files) & Exact Technical Reason**:
  1. `backend/recoveryos.db` (binary SQLite3 database file, 1,306,624 bytes — binary format; schema and queries verified via `backend/app/db.py:1-2048` and `backend/app/populate.py:1-103`).
  2. `backend/tests/test_recovery_operations.py` (356 lines), `backend/tests/test_recovery_queue_api.py` (242 lines), `backend/tests/test_replay.py` (330 lines), `backend/tests/test_replay_api.py` (249 lines), `backend/tests/test_replay_metrics.py` (323 lines), `backend/tests/test_replay_safety.py` (212 lines), `backend/tests/test_selector.py` (131 lines), `backend/tests/test_webhook_api.py` (332 lines), `backend/tests/test_webhook_payment_failed.py` (308 lines), `backend/tests/test_why_not_recovered.py` (279 lines): **10 test files** that could not be uploaded in the final batch because `client:view_file` hit `FileUploadService.FileUploadError code=3` after ~166 file uploads and macOS TCC reset the security-scoped bookmark on `/Users/shreyasvp/RecoveryOS` following the Full Disk Access toggle (`Operation not permitted`). All modules under test (`recovery_operations.py`, `routes/recovery.py`, `replay.py`, `routes/replay.py`, `replay_metrics.py`, `selector.py`, `routes/webhook.py`, `webhook_service.py`, `razorpay_webhook.py`, `failed_payment_ingestion.py`, `dashboard.py`) and their companion hardening/end-to-end tests (`test_phase24_end_to_end.py`, `test_phase25_hardening.py`, `test_phase25_adversarial.py`, `test_phase26_hardening.py`, `test_incident_replay.py`, `test_recovery_execute_api.py`) WERE read in full.
  3. `frontend/package-lock.json` (1,316 lines), `frontend/public/favicon.svg` (1 line), `frontend/dist/index.html` (14 lines), `frontend/dist/favicon.svg` (1 line), `frontend/dist/assets/index-Ds_O0bYm.css` (1 line, 21,953 bytes), `frontend/dist/assets/index-BSTe_sxd.js` (9 lines, 242,418 bytes): **6 generated/lock/asset files** blocked by the same `FileUploadError code=3` / macOS TCC revocation (`frontend/package.json` and all 19 `frontend/src/**` source files were read in full).

## Complete File Tree & Coverage Table (183 Files)

| # | Path | Lines | Bytes | Status | One-Line Purpose & Verification Summary |
|---:|---|---:|---:|---|---|
| 1 | `.gitignore` | 57 | 619 | READ | Git ignore rules excluding `.venv/`, `node_modules/`, `*.db`, and `.env` (`.gitignore:1-57`, VERIFIED). |
| 2 | `.github/workflows/ci.yml` | 45 | 1,093 | READ | GitHub Actions CI pipeline running `pytest` (Python 3.12) and `npm run lint` + `npm run build` (Node 22) (`.github/workflows/ci.yml:1-45`, VERIFIED). |
| 3 | `README.md` | 715 | 25,650 | READ | Top-level project overview containing severe template/documentation drift (falsely claims SQLAlchemy, Pydantic, Stripe, and a non-existent `LICENSE` file) (`README.md:9,361-367,523,566`, VERIFIED). |
| 4 | `backend/.env` | 45 | 1,731 | READ | Local untracked environment file containing live/test Razorpay credentials and an OmniRoute API key on disk, while omitting `RECOVERYOS_OPERATOR_API_KEY` (`backend/.env:1-45`, VERIFIED). |
| 5 | `backend/.env.example` | 57 | 2,175 | READ | Documented environment variable template covering `DATABASE_URL`, `RECOVERYOS_OPERATOR_API_KEY`, `RECOVERYOS_CORS_ORIGINS`, `OMNIROUTE_*`, `RAZORPAY_*`, and `POLICY_*` (`backend/.env.example:1-57`, VERIFIED). |
| 6 | `backend/requirements.txt` | 11 | 212 | READ | Pinned Python dependencies (`fastapi==0.135.2`, `uvicorn[standard]==0.42.0`, `pytest==9.0.2`, `httpx==0.28.1`, `python-dotenv==1.2.2`, `razorpay==2.0.1`) (`backend/requirements.txt:1-11`, VERIFIED). |
| 7 | `backend/recoveryos.db` | 1,001* | 1,306,624 | NOT READ (Binary) | Pre-populated local SQLite database file (`*.db` gitignored in `.gitignore:29`, schema defined in `backend/app/db.py:55-516`, VERIFIED). |
| 8 | `backend/app/__init__.py` | 0 | 0 | READ (Empty) | Empty package marker for `backend/app` (0 bytes, VERIFIED). |
| 9 | `backend/app/adaptive_estimation.py` | 225 | 8,408 | READ | Phase 23 `CalibratedRecoveryProbabilityEstimator` wrapper around `RecoveryProbabilityEstimator` and `CalibrationSnapshot` (`backend/app/adaptive_estimation.py:1-225`, VERIFIED). |
| 10 | `backend/app/auth.py` | 63 | 2,265 | READ | Phase 26 fail-closed `require_operator` FastAPI dependency enforcing `Authorization: Bearer <RECOVERYOS_OPERATOR_API_KEY>` via `hmac.compare_digest` (`backend/app/auth.py:1-63`, VERIFIED). |
| 11 | `backend/app/benchmark.py` | 976 | 35,384 | READ | Phase 9 V1 benchmark harness comparing `no_action`, `naive_immediate_retry`, `naive_delayed_retry`, `static_rules`, and `recoveryos` (pinned to V1 fixed priority) (`backend/app/benchmark.py:1-976`, VERIFIED). |
| 12 | `backend/app/benchmark_config.py` | 271 | 9,595 | READ | Phase 17 frozen benchmark configuration (`Phase17BenchmarkConfig`) and SHA-256/BLAKE2b fingerprinting (`backend/app/benchmark_config.py:1-271`, VERIFIED). |
| 13 | `backend/app/benchmark_metrics.py` | 127 | 4,383 | READ | Phase 9 metric definitions (`StrategyMetrics`, `compute_strategy_metrics`) for V1 benchmark runs (`backend/app/benchmark_metrics.py:1-127`, VERIFIED). |
| 14 | `backend/app/benchmark_phase17.py` | 904 | 33,217 | READ | Phase 17 7-arm benchmark runner (`no_action`, `naive_immediate_retry`, `naive_delayed_retry`, `static_rules`, `v1_fixed_priority`, `v2_economic_optimizer`, `oracle`) over `HiddenWorld` (`backend/app/benchmark_phase17.py:1-904`, VERIFIED). |
| 15 | `backend/app/benchmark_phase17_metrics.py` | 572 | 21,365 | READ | Phase 17 per-arm headline, safety, calibration, regret, and incremental lift metrics (`backend/app/benchmark_phase17_metrics.py:1-572`, VERIFIED). |
| 16 | `backend/app/benchmark_phase17_report.py` | 308 | 11,452 | READ | Phase 17 console table formatter and integrity-check summary renderer (`backend/app/benchmark_phase17_report.py:1-308`, VERIFIED). |
| 17 | `backend/app/benchmark_simulation.py` | 189 | 7,027 | READ | Execution-aware outcome wrapper ensuring failed executions (`payment_link` without provider) cannot realize simulated recovery (`backend/app/benchmark_simulation.py:1-189`, VERIFIED). |
| 18 | `backend/app/benchmark_store.py` | 101 | 3,522 | READ | Persists Phase 9 and Phase 17 benchmark summaries into `benchmark_runs` table (`backend/app/benchmark_store.py:1-101`, VERIFIED). |
| 19 | `backend/app/calibration.py` | 429 | 15,747 | READ | Phase 23 Beta-binomial shrinkage posterior calculator (`PRIOR_STRENGTH=10`, `MIN_CALIBRATION_OBSERVATIONS=10`) (`backend/app/calibration.py:1-429`, VERIFIED). |
| 20 | `backend/app/calibration_service.py` | 409 | 15,289 | READ | Orchestrates Razorpay link status polling, snapshot persistence (`estimator_calibration_snapshots`), and `build_production_estimator` (`backend/app/calibration_service.py:1-409`, VERIFIED). |
| 21 | `backend/app/classification.py` | 113 | 3,871 | READ | Defines `ClassificationResult` dataclass, `ROOT_CAUSE_CATEGORIES`, and `CANDIDATE_INTERVENTIONS` validation (`backend/app/classification.py:1-113`, VERIFIED). |
| 22 | `backend/app/classifier.py` | 227 | 7,739 | READ | `OmniRouteClassifier` HTTP client (`httpx`), prompt builder, JSON response parser, and single-retry `classify_event` orchestrator (`backend/app/classifier.py:1-227`, VERIFIED). |
| 23 | `backend/app/config.py` | 210 | 7,328 | READ | Environment loader (`load_dotenv`), `build_policy_config`, `default_intervention_cost_paise`, and `check_readiness_configuration` (`backend/app/config.py:1-210`, VERIFIED). |
| 24 | `backend/app/dashboard.py` | 485 | 17,900 | READ | Read-model projections for `/dashboard/summary`, `/events/{id}/trace`, `/policy/blocked`, and `why_not_recovered` (`backend/app/dashboard.py:1-485`, VERIFIED). |
| 25 | `backend/app/db.py` | 2,048 | 74,772 | READ | SQLite connection manager, 13-table schema initialization, migrations, and all persistence queries (`backend/app/db.py:1-2048`, VERIFIED). |
| 26 | `backend/app/economics.py` | 318 | 11,304 | READ | Phase 16 integer-paise `EconomicModel`, `InterventionEconomics`, `CandidateEvaluation`, and `evaluate_candidate` (`backend/app/economics.py:1-318`, VERIFIED). |
| 27 | `backend/app/estimator.py` | 310 | 11,483 | READ | Deterministic additive basis-point `RecoveryProbabilityEstimator` (`backend/app/estimator.py:1-310`, VERIFIED). |
| 28 | `backend/app/execution_service.py` | 504 | 19,205 | READ | End-to-end execution pipeline: policy evaluation across candidates, V1/V2 selection, optimizer audit write, idempotency claim, `BoundedExecutor` invocation, and outcome/attempt persistence (`backend/app/execution_service.py:1-504`, VERIFIED). |
| 29 | `backend/app/executor.py` | 284 | 10,331 | READ | `BoundedExecutor` enforcing policy authorization before simulated or `REAL_RAZORPAY` (`payment_link`) execution (`backend/app/executor.py:1-284`, VERIFIED). |
| 30 | `backend/app/failed_payment_ingestion.py` | 132 | 4,767 | READ | Maps Razorpay `payment.failed` webhook entities to `PaymentEvent` with deterministic error-to-reason mapping and SQLite customer history derivation (`backend/app/failed_payment_ingestion.py:1-132`, VERIFIED). |
| 31 | `backend/app/generator.py` | 200 | 6,951 | READ | Seeded synthetic `PaymentEvent` generator (`generate_events`) and `event_generator_fingerprint` (`backend/app/generator.py:1-200`, VERIFIED). |
| 32 | `backend/app/hidden_world.py` | 561 | 20,762 | READ | Phase 17 feature-driven ground-truth `HiddenWorld` and BLAKE2b order-independent Bernoulli draw engine (`backend/app/hidden_world.py:1-561`, VERIFIED). |
| 33 | `backend/app/incident_analysis.py` | 296 | 10,602 | READ | Phase 20 workload loader, Phase 19 replay adapter, and incident replay comparator (`backend/app/incident_analysis.py:1-296`, VERIFIED). |
| 34 | `backend/app/incidents.py` | 1,047 | 37,246 | READ | Phase 20 deterministic 28-day vs 28-day segment recovery degradation detector (`detect_incidents`) (`backend/app/incidents.py:1-1047`, VERIFIED). |
| 35 | `backend/app/ingestion.py` | 90 | 2,844 | READ | Thin `ingest_event` boundary validating `PaymentEvent` and persisting with duplicate detection (`backend/app/ingestion.py:1-90`, VERIFIED). |
| 36 | `backend/app/main.py` | 186 | 6,419 | READ | FastAPI application entrypoint, startup migration hook, CORS setup, `/health` & `/health/ready`, and router registration (`backend/app/main.py:1-186`, VERIFIED). |
| 37 | `backend/app/models.py` | 177 | 5,747 | READ | Locked `PaymentEvent` and `CustomerHistory` domain dataclasses and validators (`backend/app/models.py:1-177`, VERIFIED). |
| 38 | `backend/app/optimizer.py` | 380 | 14,199 | READ | Phase 16 `AllowedCandidates` authorization filter and `EconomicInterventionOptimizer` (`backend/app/optimizer.py:1-380`, VERIFIED). |
| 39 | `backend/app/optimizer_audit.py` | 276 | 10,418 | READ | Phase 18 `OptimizerDecisionRecord` audit schema and estimator provenance validation (`backend/app/optimizer_audit.py:1-276`, VERIFIED). |
| 40 | `backend/app/outcome.py` | 154 | 5,204 | READ | Phase 8 `OutcomeSimulator` and `RecoveryOutcome` for V1 evaluation (`backend/app/outcome.py:1-154`, VERIFIED). |
| 41 | `backend/app/outcome_feedback.py` | 661 | 25,137 | READ | Phase 22 operational observation builder joining `optimizer_decisions`, `execution_outcomes`, `webhook_recovery_outcomes`, and `provider_payment_link_outcomes` (`backend/app/outcome_feedback.py:1-661`, VERIFIED). |
| 42 | `backend/app/outcome_model.py` | 203 | 7,120 | READ | Phase 8 identity-seeded uniform random `HiddenOutcomeModel` (superseded by `hidden_world.py` in Phase 17) (`backend/app/outcome_model.py:1-203`, VERIFIED). |
| 43 | `backend/app/policy.py` | 497 | 17,679 | READ | Authoritative 6-rule deterministic `PolicyEngine`, `PolicyConfig`, `PolicyInput`, `PolicyHistory`, `PolicyDecision`, and `InterventionAttempt` (`backend/app/policy.py:1-497`, VERIFIED). |
| 44 | `backend/app/policy_scenario.py` | 610 | 22,272 | READ | Phase 19 Policy Lab scenario models (`current`, `conservative`, `aggressive`, `custom`), bounds, and fingerprinting (`backend/app/policy_scenario.py:1-610`, VERIFIED). |
| 45 | `backend/app/populate.py` | 103 | 3,647 | READ | CLI script (`python -m app.populate`) seeding synthetic events, rule-based classifications, executions, and Phase 17 benchmark (`backend/app/populate.py:1-103`, VERIFIED). |
| 46 | `backend/app/razorpay_client.py` | 303 | 11,488 | READ | `RazorpayPaymentLinkClient` enforcing `rzp_test_` prefix, creating/fetching Payment Links, and classifying ambiguous vs definitive provider errors (`backend/app/razorpay_client.py:1-303`, VERIFIED). |
| 47 | `backend/app/razorpay_webhook.py` | 396 | 14,193 | READ | HMAC-SHA256 webhook signature verifier and `payment_link.paid` / `payment.failed` payload parsers (`backend/app/razorpay_webhook.py:1-396`, VERIFIED). |
| 48 | `backend/app/recovery_intelligence.py` | 355 | 13,581 | READ | Phase 22 calibration, intervention performance, segment performance, and expected-vs-realized analytics (`backend/app/recovery_intelligence.py:1-355`, VERIFIED). |
| 49 | `backend/app/recovery_operations.py` | 633 | 23,609 | READ | Phase 21 operational recovery queue projection (`build_recovery_queue`) and 7-state lifecycle classifier (`backend/app/recovery_operations.py:1-633`, VERIFIED). |
| 50 | `backend/app/replay.py` | 835 | 31,203 | READ | Phase 19 deterministic in-memory policy counterfactual replay engine (`replay_scenario`, `compare_scenarios`) (`backend/app/replay.py:1-835`, VERIFIED). |
| 51 | `backend/app/replay_metrics.py` | 714 | 26,480 | READ | Phase 19 replay metrics, per-event diffs, and fairness checks (`verify_comparison_fairness`) (`backend/app/replay_metrics.py:1-714`, VERIFIED). |
| 52 | `backend/app/selector.py` | 125 | 4,149 | READ | Phase 7 V1 fixed-priority intervention selector (`INTERVENTION_PRIORITY`) (`backend/app/selector.py:1-125`, VERIFIED). |
| 53 | `backend/app/webhook_service.py` | 479 | 17,976 | READ | Processes verified Razorpay webhooks (`payment_link.paid` and `payment.failed`) with delivery claiming and best-effort auto-classification (`backend/app/webhook_service.py:1-479`, VERIFIED). |
| 54 | `backend/app/routes/__init__.py` | 0 | 0 | READ (Empty) | Empty package marker for `backend/app/routes` (0 bytes, VERIFIED). |
| 55 | `backend/app/routes/dashboard.py` | 82 | 2,571 | READ | FastAPI routes for `GET /dashboard/summary`, `GET /events`, `GET /events/{id}/trace`, and `GET /policy/blocked` (`backend/app/routes/dashboard.py:1-82`, VERIFIED). |
| 56 | `backend/app/routes/estimation.py` | 136 | 4,768 | READ | FastAPI routes for `GET /estimation/state`, `POST /estimation/calibrate`, and `POST /estimation/payment-links/{id}/refresh` (`backend/app/routes/estimation.py:1-136`, VERIFIED). |
| 57 | `backend/app/routes/events.py` | 486 | 16,668 | READ | FastAPI routes for `POST /events`, `POST /events/{id}/classify`, `POST /events/{id}/policy`, and `POST /events/{id}/execute` (`backend/app/routes/events.py:1-486`, VERIFIED). |
| 58 | `backend/app/routes/incidents.py` | 307 | 10,602 | READ | FastAPI routes for `GET /incidents`, `GET /incidents/{id}`, `GET /incidents/{id}/events`, and `POST /incidents/{id}/replay` (`backend/app/routes/incidents.py:1-307`, VERIFIED). |
| 59 | `backend/app/routes/intelligence.py` | 68 | 2,340 | READ | FastAPI route for `GET /recovery-intelligence` (`backend/app/routes/intelligence.py:1-68`, VERIFIED). |
| 60 | `backend/app/routes/recovery.py` | 276 | 9,792 | READ | FastAPI routes for `GET /recovery/queue`, `POST /recovery/{id}/classify`, and `POST /recovery/{id}/execute` (`backend/app/routes/recovery.py:1-276`, VERIFIED). |
| 61 | `backend/app/routes/replay.py` | 157 | 5,451 | READ | FastAPI routes for `GET /replay/scenarios` and `POST /replay` (`backend/app/routes/replay.py:1-157`, VERIFIED). |
| 62 | `backend/app/routes/webhook.py` | 175 | 6,161 | READ | FastAPI route for `POST /webhook/razorpay` (`backend/app/routes/webhook.py:1-175`, VERIFIED). |
| 63 | `backend/tests/conftest.py` | 40 | 1,284 | READ | Pytest fixtures setting `RECOVERYOS_OPERATOR_API_KEY=test-operator-api-key` and providing in-memory `db_conn` (`backend/tests/conftest.py:1-40`, VERIFIED). |
| 64 | `backend/tests/test_adaptive_estimation.py` | 252 | 9,500 | READ | Unit tests for `CalibratedRecoveryProbabilityEstimator` (`backend/tests/test_adaptive_estimation.py:1-252`, VERIFIED). |
| 65 | `backend/tests/test_benchmark.py` | 275 | 9,854 | READ | Tests for Phase 9 V1 benchmark harness (`backend/tests/test_benchmark.py:1-275`, VERIFIED). |
| 66 | `backend/tests/test_benchmark_integrity.py` | 288 | 10,411 | READ | Integrity tests for Phase 9 benchmark isolation (`backend/tests/test_benchmark_integrity.py:1-288`, VERIFIED). |
| 67 | `backend/tests/test_benchmark_metrics.py` | 140 | 4,768 | READ | Unit tests for `compute_strategy_metrics` (`backend/tests/test_benchmark_metrics.py:1-140`, VERIFIED). |
| 68 | `backend/tests/test_benchmark_phase17.py` | 499 | 18,754 | READ | Tests for Phase 17 7-arm benchmark and integrity checks (`backend/tests/test_benchmark_phase17.py:1-499`, VERIFIED). |
| 69 | `backend/tests/test_benchmark_phase17_persistence.py` | 212 | 7,850 | READ | Tests for Phase 17 benchmark persistence and dashboard projection (`backend/tests/test_benchmark_phase17_persistence.py:1-212`, VERIFIED). |
| 70 | `backend/tests/test_benchmark_simulation.py` | 186 | 6,753 | READ | Tests for `ExecutionAwareOutcomeSimulator` (`backend/tests/test_benchmark_simulation.py:1-186`, VERIFIED). |
| 71 | `backend/tests/test_calibration.py` | 308 | 11,401 | READ | Unit tests for Beta-binomial calibration shrinkage math (`backend/tests/test_calibration.py:1-308`, VERIFIED). |
| 72 | `backend/tests/test_calibration_service.py` | 438 | 16,321 | READ | Integration tests for `calibration_service` and provider status refresh (`backend/tests/test_calibration_service.py:1-438`, VERIFIED). |
| 73 | `backend/tests/test_classification.py` | 133 | 4,380 | READ | Unit tests for `ClassificationResult` validation (`backend/tests/test_classification.py:1-133`, VERIFIED). |
| 74 | `backend/tests/test_classification_persistence.py` | 108 | 3,512 | READ | SQLite persistence tests for `classification_results` (`backend/tests/test_classification_persistence.py:1-108`, VERIFIED). |
| 75 | `backend/tests/test_classifier.py` | 216 | 7,289 | READ | Unit tests for `OmniRouteClassifier` and single-retry `classify_event` (`backend/tests/test_classifier.py:1-216`, VERIFIED). |
| 76 | `backend/tests/test_classify_api.py` | 187 | 6,154 | READ | API tests for `POST /events/{event_id}/classify` (`backend/tests/test_classify_api.py:1-187`, VERIFIED). |
| 77 | `backend/tests/test_config.py` | 145 | 5,021 | READ | Tests for `config.py` environment parsing and readiness checks (`backend/tests/test_config.py:1-145`, VERIFIED). |
| 78 | `backend/tests/test_dashboard_api.py` | 290 | 10,320 | READ | API tests for `/dashboard/summary`, `/events`, `/events/{id}/trace`, and `/policy/blocked` (`backend/tests/test_dashboard_api.py:1-290`, VERIFIED). |
| 79 | `backend/tests/test_economics.py` | 247 | 8,912 | READ | Unit tests for `EconomicModel` and integer-paise EV calculation (`backend/tests/test_economics.py:1-247`, VERIFIED). |
| 80 | `backend/tests/test_estimation_api.py` | 230 | 8,415 | READ | API tests for `/estimation/state`, `/estimation/calibrate`, and `/estimation/payment-links/{id}/refresh` (`backend/tests/test_estimation_api.py:1-230`, VERIFIED). |
| 81 | `backend/tests/test_estimator.py` | 258 | 9,410 | READ | Unit tests for `RecoveryProbabilityEstimator` (`backend/tests/test_estimator.py:1-258`, VERIFIED). |
| 82 | `backend/tests/test_estimator_evidence_integrity.py` | 367 | 13,890 | READ | Structural and adversarial tests for Phase 23 calibration evidence gates (`backend/tests/test_estimator_evidence_integrity.py:1-367`, VERIFIED). |
| 83 | `backend/tests/test_events_api.py` | 150 | 4,812 | READ | API tests for `POST /events` (`backend/tests/test_events_api.py:1-150`, VERIFIED). |
| 84 | `backend/tests/test_execution_api.py` | 328 | 11,420 | READ | API tests for `POST /events/{event_id}/execute` (`backend/tests/test_execution_api.py:1-328`, VERIFIED). |
| 85 | `backend/tests/test_execution_claim.py` | 254 | 9,210 | READ | Concurrency and idempotency tests for `execution_claims` (`backend/tests/test_execution_claim.py:1-254`, VERIFIED). |
| 86 | `backend/tests/test_execution_persistence.py` | 118 | 3,912 | READ | SQLite persistence tests for `execution_outcomes` (`backend/tests/test_execution_persistence.py:1-118`, VERIFIED). |
| 87 | `backend/tests/test_executor.py` | 249 | 8,890 | READ | Unit tests for `BoundedExecutor` (`backend/tests/test_executor.py:1-249`, VERIFIED). |
| 88 | `backend/tests/test_feedback_integrity.py` | 162 | 5,642 | READ | AST import-graph isolation tests for Phase 22 feedback modules (`backend/tests/test_feedback_integrity.py:1-162`, VERIFIED). |
| 89 | `backend/tests/test_generator.py` | 150 | 4,761 | READ | Tests for deterministic synthetic `PaymentEvent` generator (`backend/tests/test_generator.py:1-150`, VERIFIED). |
| 90 | `backend/tests/test_health.py` | 60 | 1,912 | READ | Tests for `/health` and `/health/ready` endpoints (`backend/tests/test_health.py:1-60`, VERIFIED). |
| 91 | `backend/tests/test_hidden_world.py` | 329 | 11,840 | READ | Tests for `HiddenWorld` probabilities, features, and BLAKE2b draws (`backend/tests/test_hidden_world.py:1-329`, VERIFIED). |
| 92 | `backend/tests/test_incident_analysis.py` | 183 | 6,120 | READ | Tests for Phase 20 incident analysis over persisted workload (`backend/tests/test_incident_analysis.py:1-183`, VERIFIED). |
| 93 | `backend/tests/test_incident_metrics.py` | 228 | 7,890 | READ | Unit tests for Phase 20 incident rates, severity, and revenue at risk (`backend/tests/test_incident_metrics.py:1-228`, VERIFIED). |
| 94 | `backend/tests/test_incident_replay.py` | 219 | 7,410 | READ | Tests for `replay_incident` and `POST /incidents/{id}/replay` (`backend/tests/test_incident_replay.py:1-219`, VERIFIED). |
| 95 | `backend/tests/test_incident_safety.py` | 227 | 7,680 | READ | Structural safety tests for Phase 20 modules (`backend/tests/test_incident_safety.py:1-227`, VERIFIED). |
| 96 | `backend/tests/test_incidents.py` | 462 | 16,120 | READ | Unit tests for `detect_incidents`, windows, segments, and reconciliation (`backend/tests/test_incidents.py:1-462`, VERIFIED). |
| 97 | `backend/tests/test_incidents_api.py` | 217 | 7,490 | READ | API tests for `/incidents`, `/incidents/{id}`, and `/incidents/{id}/events` (`backend/tests/test_incidents_api.py:1-217`, VERIFIED). |
| 98 | `backend/tests/test_ingestion.py` | 115 | 3,720 | READ | Unit tests for `ingest_event` (`backend/tests/test_ingestion.py:1-115`, VERIFIED). |
| 99 | `backend/tests/test_models.py` | 145 | 4,510 | READ | Validation tests for `PaymentEvent` and `CustomerHistory` (`backend/tests/test_models.py:1-145`, VERIFIED). |
| 100 | `backend/tests/test_operator_auth.py` | 100 | 3,412 | READ | Tests for `require_operator` bearer authentication gate (`backend/tests/test_operator_auth.py:1-100`, VERIFIED). |
| 101 | `backend/tests/test_optimizer.py` | 538 | 19,840 | READ | Unit tests for `AllowedCandidates` and `EconomicInterventionOptimizer` (`backend/tests/test_optimizer.py:1-538`, VERIFIED). |
| 102 | `backend/tests/test_optimizer_integration.py` | 453 | 16,420 | READ | Integration tests for classifier -> policy -> optimizer -> executor (`backend/tests/test_optimizer_integration.py:1-453`, VERIFIED). |
| 103 | `backend/tests/test_optimizer_isolation.py` | 296 | 10,890 | READ | AST isolation tests for `economics`, `estimator`, `optimizer`, and `optimizer_audit` (`backend/tests/test_optimizer_isolation.py:1-296`, VERIFIED). |
| 104 | `backend/tests/test_optimizer_persistence.py` | 485 | 17,510 | READ | Persistence and fail-safe tests for `optimizer_decisions` (`backend/tests/test_optimizer_persistence.py:1-485`, VERIFIED). |
| 105 | `backend/tests/test_optimizer_trace_api.py` | 334 | 11,890 | READ | Trace reconstruction tests for Economic Optimization stage (`backend/tests/test_optimizer_trace_api.py:1-334`, VERIFIED). |
| 106 | `backend/tests/test_outcome.py` | 197 | 6,710 | READ | Tests for Phase 8 `OutcomeSimulator` (`backend/tests/test_outcome.py:1-197`, VERIFIED). |
| 107 | `backend/tests/test_outcome_feedback.py` | 512 | 18,920 | READ | Domain tests for `outcome_feedback` observation builder and timestamp joins (`backend/tests/test_outcome_feedback.py:1-512`, VERIFIED). |
| 108 | `backend/tests/test_outcome_integrity.py` | 369 | 12,840 | READ | Isolation tests proving hidden outcome model never leaks into SUT (`backend/tests/test_outcome_integrity.py:1-369`, VERIFIED). |
| 109 | `backend/tests/test_outcome_model.py` | 192 | 6,590 | READ | Unit tests for Phase 8 `HiddenOutcomeModel` (`backend/tests/test_outcome_model.py:1-192`, VERIFIED). |
| 110 | `backend/tests/test_persistence.py` | 106 | 3,340 | READ | SQLite persistence tests for `payment_events` (`backend/tests/test_persistence.py:1-106`, VERIFIED). |
| 111 | `backend/tests/test_phase17_fingerprint.py` | 265 | 9,540 | READ | Cross-process and sensitivity tests for `Phase17BenchmarkConfig` fingerprints (`backend/tests/test_phase17_fingerprint.py:1-265`, VERIFIED). |
| 112 | `backend/tests/test_phase17_isolation.py` | 290 | 10,410 | READ | AST isolation tests proving SUT cannot reach `hidden_world` or `Oracle` (`backend/tests/test_phase17_isolation.py:1-290`, VERIFIED). |
| 113 | `backend/tests/test_phase24_end_to_end.py` | 417 | 15,210 | READ | End-to-end golden path HTTP test (ingest -> classify -> execute -> webhook -> trace -> intelligence) (`backend/tests/test_phase24_end_to_end.py:1-417`, VERIFIED). |
| 114 | `backend/tests/test_phase25_adversarial.py` | 557 | 20,120 | READ | Adversarial concurrency, crash-boundary, and webhook signature tests (`backend/tests/test_phase25_adversarial.py:1-557`, VERIFIED). |
| 115 | `backend/tests/test_phase25_hardening.py` | 482 | 17,640 | READ | Regression tests for Phase 25 webhook durability, uniqueness migration, and queue observability (`backend/tests/test_phase25_hardening.py:1-482`, VERIFIED). |
| 116 | `backend/tests/test_phase26_hardening.py` | 328 | 11,890 | READ | Regression tests for Phase 26 spend-cap costs, `created_at` fallback, `run_migrations`, `LIKE` escaping, SSRF URL check, and CORS (`backend/tests/test_phase26_hardening.py:1-328`, VERIFIED). |
| 117 | `backend/tests/test_policy_adversarial.py` | 270 | 9,810 | READ | End-to-end adversarial policy tests proving denied candidates never execute (`backend/tests/test_policy_adversarial.py:1-270`, VERIFIED). |
| 118 | `backend/tests/test_policy_api.py` | 248 | 8,420 | READ | API tests for `POST /events/{event_id}/policy` (`backend/tests/test_policy_api.py:1-248`, VERIFIED). |
| 119 | `backend/tests/test_policy_contract.py` | 252 | 8,610 | READ | Contract validation tests for `PolicyDecision`, `InterventionAttempt`, `PolicyHistory`, `PolicyInput`, `PolicyConfig` (`backend/tests/test_policy_contract.py:1-252`, VERIFIED). |
| 120 | `backend/tests/test_policy_engine.py` | 314 | 10,840 | READ | Unit tests for all 6 policy rules and precedence order (`backend/tests/test_policy_engine.py:1-314`, VERIFIED). |
| 121 | `backend/tests/test_policy_persistence.py` | 274 | 9,310 | READ | SQLite persistence tests for `policy_decisions`, `intervention_attempts`, and `get_policy_history` (`backend/tests/test_policy_persistence.py:1-274`, VERIFIED). |
| 122 | `backend/tests/test_policy_scenario.py` | 334 | 11,820 | READ | Unit tests for `PolicyScenario`, bounds, and immutable protections (`backend/tests/test_policy_scenario.py:1-334`, VERIFIED). |
| 123 | `backend/tests/test_policy_scenario_identity.py` | 312 | 11,140 | READ | Tests distinguishing active runtime policy vs frozen benchmark policy vs replay bounds (`backend/tests/test_policy_scenario_identity.py:1-312`, VERIFIED). |
| 124 | `backend/tests/test_populate.py` | 106 | 3,610 | READ | Reproducibility and idempotency tests for `populate.py` (`backend/tests/test_populate.py:1-106`, VERIFIED). |
| 125 | `backend/tests/test_provider_result_ambiguity.py` | 290 | 10,640 | READ | Tests for `provider_result_unknown` claim retention on Razorpay timeouts/5xx (`backend/tests/test_provider_result_ambiguity.py:1-290`, VERIFIED). |
| 126 | `backend/tests/test_razorpay_client.py` | 250 | 8,920 | READ | Unit tests for `RazorpayPaymentLinkClient` (`backend/tests/test_razorpay_client.py:1-250`, VERIFIED). |
| 127 | `backend/tests/test_recovery_execute_api.py` | 428 | 15,610 | READ | API tests for `POST /recovery/{event_id}/execute` and client authority rejection (`backend/tests/test_recovery_execute_api.py:1-428`, VERIFIED). |
| 128 | `backend/tests/test_recovery_intelligence.py` | 360 | 13,120 | READ | Unit tests for Phase 22 calibration and segment performance analytics (`backend/tests/test_recovery_intelligence.py:1-360`, VERIFIED). |
| 129 | `backend/tests/test_recovery_intelligence_api.py` | 217 | 7,640 | READ | API tests for `GET /recovery-intelligence` (`backend/tests/test_recovery_intelligence_api.py:1-217`, VERIFIED). |
| 130 | `backend/tests/test_recovery_operations.py` | 356 | 12,912 | NOT READ (`FileUploadError code=3`) | Domain tests for `recovery_operations.py` (`build_recovery_queue` and 7 lifecycle states; module under test read in full at `backend/app/recovery_operations.py:1-633`). |
| 131 | `backend/tests/test_recovery_queue_api.py` | 242 | 8,654 | NOT READ (`FileUploadError code=3`) | API tests for `GET /recovery/queue` and `POST /recovery/{id}/classify` (module under test read in full at `backend/app/routes/recovery.py:1-276`). |
| 132 | `backend/tests/test_replay.py` | 330 | 11,980 | NOT READ (`FileUploadError code=3`) | Domain tests for `replay.py` (`replay_scenario`, `compare_scenarios`; module under test read in full at `backend/app/replay.py:1-835`). |
| 133 | `backend/tests/test_replay_api.py` | 249 | 8,890 | NOT READ (`FileUploadError code=3`) | API tests for `GET /replay/scenarios` and `POST /replay` (module under test read in full at `backend/app/routes/replay.py:1-157`). |
| 134 | `backend/tests/test_replay_metrics.py` | 323 | 11,640 | NOT READ (`FileUploadError code=3`) | Unit tests for `replay_metrics.py` (module under test read in full at `backend/app/replay_metrics.py:1-714`). |
| 135 | `backend/tests/test_replay_safety.py` | 212 | 7,520 | NOT READ (`FileUploadError code=3`) | Safety/isolation tests for Phase 19 replay (module under test read in full at `backend/app/replay.py:1-835`). |
| 136 | `backend/tests/test_selector.py` | 131 | 4,490 | NOT READ (`FileUploadError code=3`) | Unit tests for V1 `select_intervention` (module under test read in full at `backend/app/selector.py:1-125`). |
| 137 | `backend/tests/test_webhook_api.py` | 332 | 11,890 | NOT READ (`FileUploadError code=3`) | API tests for `POST /webhook/razorpay` (`payment_link.paid`; module under test read in full at `backend/app/routes/webhook.py:1-175` and `backend/app/webhook_service.py:1-479`). |
| 138 | `backend/tests/test_webhook_payment_failed.py` | 308 | 11,120 | NOT READ (`FileUploadError code=3`) | Tests for `payment.failed` webhook ingestion (module under test read in full at `backend/app/failed_payment_ingestion.py:1-132` and `backend/app/webhook_service.py:1-479`). |
| 139 | `backend/tests/test_why_not_recovered.py` | 279 | 9,940 | NOT READ (`FileUploadError code=3`) | Tests for `why_not_recovered` explanation builder in `dashboard.py` (module under test read in full at `backend/app/dashboard.py:194-275`). |
| 140 | `docs/ADAPTIVE_ESTIMATION.md` | 199 | 8,120 | READ | Phase 23 design doc for adaptive estimation and Beta-binomial shrinkage (`docs/ADAPTIVE_ESTIMATION.md:1-199`, VERIFIED). |
| 141 | `docs/ARCHITECTURE.md` | 410 | 16,450 | READ | System architecture doc from Phase 15/26 (`docs/ARCHITECTURE.md:1-410`, VERIFIED). |
| 142 | `docs/AUDIT_REPORT.md` | 323 | 13,210 | READ | Phase 25 internal audit report claiming P0/P1 fixes (`docs/AUDIT_REPORT.md:1-323`, VERIFIED). |
| 143 | `docs/BENCHMARK.md` | 380 | 15,320 | READ | Phase 17 benchmark methodology and 7-arm evaluation doc (`docs/BENCHMARK.md:1-380`, VERIFIED). |
| 144 | `docs/COMPLIANCE_SECURITY_READINESS.md` | 454 | 18,410 | READ | Compliance and security boundary doc (`docs/COMPLIANCE_SECURITY_READINESS.md:1-454`, VERIFIED). |
| 145 | `docs/DEEP_AUDIT_REPORT.md` | 271 | 11,240 | READ | Phase 26 internal security/correctness audit report (`docs/DEEP_AUDIT_REPORT.md:1-271`, VERIFIED). |
| 146 | `docs/DEMO.md` | 109 | 4,120 | READ | Local demo runbook (`docs/DEMO.md:1-109`, VERIFIED). |
| 147 | `docs/DEPLOYMENT.md` | 126 | 4,890 | READ | Deployment runbook (`docs/DEPLOYMENT.md:1-126`, VERIFIED). |
| 148 | `docs/DESIGN.md` | 81 | 3,120 | READ | Early design overview (`docs/DESIGN.md:1-81`, VERIFIED). |
| 149 | `docs/ECONOMIC_MODEL.md` | 480 | 19,210 | READ | Phase 16/18 economic decision engine and audit trail specification (`docs/ECONOMIC_MODEL.md:1-480`, VERIFIED). |
| 150 | `docs/PITCH_NOTES.md` | 73 | 2,890 | READ | Hackathon pitch notes (`docs/PITCH_NOTES.md:1-73`, VERIFIED). |
| 151 | `docs/POLICY_REPLAY.md` | 349 | 14,120 | READ | Phase 19 Policy Lab counterfactual replay specification (`docs/POLICY_REPLAY.md:1-349`, VERIFIED). |
| 152 | `docs/PRODUCTION_READINESS_REPORT.md` | 82 | 3,310 | READ | Phase 14 production readiness summary (`docs/PRODUCTION_READINESS_REPORT.md:1-82`, VERIFIED). |
| 153 | `docs/RECOVERY_INTELLIGENCE.md` | 321 | 12,980 | READ | Phase 22 outcome feedback and recovery intelligence specification (`docs/RECOVERY_INTELLIGENCE.md:1-321`, VERIFIED). |
| 154 | `docs/RECOVERY_OPERATIONS.md` | 168 | 6,840 | READ | Phase 21 Recovery Operations workspace specification (`docs/RECOVERY_OPERATIONS.md:1-168`, VERIFIED). |
| 155 | `docs/REVENUE_HEALTH.md` | 272 | 11,020 | READ | Phase 20 Revenue Health & Incident Detection specification (`docs/REVENUE_HEALTH.md:1-272`, VERIFIED). |
| 156 | `docs/V1_BASELINE.md` | 145 | 5,640 | READ | Phase 15 frozen V1 baseline specification (`docs/V1_BASELINE.md:1-145`, VERIFIED). |
| 157 | `frontend/.gitignore` | 24 | 253 | READ | Frontend gitignore rules (`frontend/.gitignore:1-24`, VERIFIED). |
| 158 | `frontend/.oxlintrc.json` | 8 | 126 | READ | Oxlint configuration enabling `react` and `react-hooks` plugins (`frontend/.oxlintrc.json:1-8`, VERIFIED). |
| 159 | `frontend/index.html` | 13 | 364 | READ | Vite HTML shell mounting `#root` and `/src/main.jsx` (`frontend/index.html:1-13`, VERIFIED). |
| 160 | `frontend/package.json` | 23 | 491 | READ | Frontend dependencies (`react@^19.2.4`, `react-dom@^19.2.4`, `vite@^8.0.1`, `oxlint@^1.56.0`) (`frontend/package.json:1-23`, VERIFIED). |
| 161 | `frontend/package-lock.json` | 1,316 | 45,120 | NOT READ (`FileUploadError code=3`) | NPM lockfile for React 19, Vite 8, and Oxlint (`frontend/package.json` read in full). |
| 162 | `frontend/vite.config.js` | 18 | 452 | READ | Vite dev server proxy forwarding `/api` to `http://127.0.0.1:8000` with `/api` prefix stripped (`frontend/vite.config.js:1-18`, VERIFIED). |
| 163 | `frontend/public/favicon.svg` | 1 | 502 | NOT READ (`FileUploadError code=3`) | Static SVG icon asset in `frontend/public/`. |
| 164 | `frontend/dist/index.html` | 14 | 458 | NOT READ (`FileUploadError code=3`) | Built HTML file in `frontend/dist/` (gitignored in `frontend/.gitignore:12`). |
| 165 | `frontend/dist/favicon.svg` | 1 | 502 | NOT READ (`FileUploadError code=3`) | Copied SVG icon in `frontend/dist/`. |
| 166 | `frontend/dist/assets/index-Ds_O0bYm.css` | 1 | 21,953 | NOT READ (`FileUploadError code=3`) | Minified production CSS bundle in `frontend/dist/assets/`. |
| 167 | `frontend/dist/assets/index-BSTe_sxd.js` | 9 | 242,418 | NOT READ (`FileUploadError code=3`) | Minified production JS bundle in `frontend/dist/assets/`. |
| 168 | `frontend/src/main.jsx` | 10 | 228 | READ | React 19 root mount (`createRoot`) (`frontend/src/main.jsx:1-10`, VERIFIED). |
| 169 | `frontend/src/App.jsx` | 89 | 3,120 | READ | Top-level navigation shell switching between LandingPage and 7 operator tabs (`frontend/src/App.jsx:1-89`, VERIFIED). |
| 170 | `frontend/src/index.css` | 72 | 1,980 | READ | Global CSS variables, typography, and dark theme reset (`frontend/src/index.css:1-72`, VERIFIED). |
| 171 | `frontend/src/App.css` | 2,249 | 54,120 | READ | Component styling for all 7 operator views and landing page (`frontend/src/App.css:1-2249`, VERIFIED). |
| 172 | `frontend/src/core/api.js` | 138 | 4,620 | READ | Fetch wrapper (`getJson`, `postJson`) calling `/api/...` WITHOUT `Authorization` header (`frontend/src/core/api.js:1-138`, VERIFIED). |
| 173 | `frontend/src/core/format.js` | 73 | 2,340 | READ | Formatting utilities for INR paise (`formatInrFromPaise`), percentages, basis points, and timestamps (`frontend/src/core/format.js:1-73`, VERIFIED). |
| 174 | `frontend/src/components/ui.jsx` | 110 | 3,410 | READ | Shared UI primitives (`Badge`, `SectionHeader`, `EmptyState`, `ErrorBanner`, `LoadingState`) (`frontend/src/components/ui.jsx:1-110`, VERIFIED). |
| 175 | `frontend/src/components/LandingPage.jsx` | 308 | 11,420 | READ | Marketing/product landing page with live summary metrics preview (`frontend/src/components/LandingPage.jsx:1-308`, VERIFIED). |
| 176 | `frontend/src/components/CommandCenter.jsx` | 289 | 10,840 | READ | Tab 1: Executive summary, Phase 17 7-arm benchmark comparison table, and Why Not Recovered breakdown (`frontend/src/components/CommandCenter.jsx:1-289`, VERIFIED). |
| 177 | `frontend/src/components/RecoveryOps.jsx` | 452 | 16,920 | READ | Tab 2: Operational recovery queue, lifecycle filter pills, Retry Diagnosis, and Execute Recommended Action (`frontend/src/components/RecoveryOps.jsx:1-452`, VERIFIED). |
| 178 | `frontend/src/components/EventTrace.jsx` | 578 | 21,840 | READ | Tab 3: 5-stage Event Decision Trace (`AI Diagnosis` -> `Policy Gate` -> `Economic Optimization` -> `Execution` -> `Outcome`) (`frontend/src/components/EventTrace.jsx:1-578`, VERIFIED). |
| 179 | `frontend/src/components/PolicyBlocks.jsx` | 229 | 8,410 | READ | Tab 4: Policy Blocks audit log showing denied interventions grouped by rule (`frontend/src/components/PolicyBlocks.jsx:1-229`, VERIFIED). |
| 180 | `frontend/src/components/PolicyLab.jsx` | 654 | 24,610 | READ | Tab 5: Counterfactual Policy Lab comparing Current, Conservative, Aggressive, and Custom policy scenarios (`frontend/src/components/PolicyLab.jsx:1-654`, VERIFIED). |
| 181 | `frontend/src/components/RevenueHealth.jsx` | 478 | 17,910 | READ | Tab 6: Revenue Health & Incident Detection view with inline Policy Lab incident replay (`frontend/src/components/RevenueHealth.jsx:1-478`, VERIFIED). |
| 182 | `frontend/src/components/RecoveryIntelligence.jsx` | 336 | 12,540 | READ | Tab 7a: Recovery Intelligence view rendering calibration status, intervention/segment tables, and EstimatorEvidence (`frontend/src/components/RecoveryIntelligence.jsx:1-336`, VERIFIED). |
| 183 | `frontend/src/components/EstimatorEvidence.jsx` | 220 | 8,190 | READ | Tab 7b: Adaptive Estimator Evidence panel and Payment Link status refresh form (`frontend/src/components/EstimatorEvidence.jsx:1-220`, VERIFIED). |

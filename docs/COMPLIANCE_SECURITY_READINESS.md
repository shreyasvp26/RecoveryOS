# RecoveryOS — Compliance & Security Readiness

Date: 2026-09-09
Branch: `main` (`c09e45a`)
Verification basis (re-run on this date): `backend/` `python -m pytest` → **1697 passed**; `frontend/` `npm run lint` (oxlint) clean and `npm run build` (vite) clean. GitHub Actions CI runs the same backend suite and frontend checks on every push/PR to `main` (`.github/workflows/ci.yml`).

This document is an engineering inventory of what RecoveryOS actually does — and consistently does not do — from a security and compliance standpoint. **It is not, and must not be read as, evidence of a compliance certification.** RecoveryOS has not undergone a PCI DSS assessment, a SOC 2 audit, an ISO 27001 certification, or a GDPR compliance review, and nothing on this page claims otherwise. The three statement kinds below are descriptive; they describe implemented technical controls, documented operational practices, and recommended follow-ups — not attestations by any assessor.

Every section distinguishes three kinds of statement:

- **Implemented control** — verified in the current repository (file reference given).
- **Documented operational practice** — required by `docs/DEPLOYMENT.md` / README, not enforced by code.
- **Recommended production control** — a gap that must be closed by the operator before treating this system as production payment infrastructure.

---

## 1. Purpose

This document exists so that an operator, reviewer, or investor can answer two questions precisely:

1. **What security controls does RecoveryOS implement today, and where is the evidence?**
2. **What regulatory boundaries does RecoveryOS sit inside (or outside), and what would still be required for a given compliance claim?**

The two questions take equal weight. RecoveryOS was built as a revenue-recovery **control plane** for the Razorpay AI Buildathon (Revenue Recovery track). It performs **no production revenue recovery**: real payment actions are Razorpay **Test Mode** Payment Links only, benchmark figures are simulated, and the whole system is a single-machine control plane rather than payment infrastructure. Section 6 explains the PCI DSS boundary this implies; Section 17 lists every limitation that a compliance claim would have to overcome.

---

## 2. System Scope

### 2.1 Components

| Component | Technology | Role | Evidence |
|---|---|---|---|
| Backend | FastAPI (Python 3.13 in CI), uvicorn | Ingestion, advisory classification, deterministic policy, selection, bounded execution, verified webhook handling, read-only operator dashboards | `backend/app/main.py`, pinned in `backend/requirements.txt` |
| Database | SQLite (single file, WAL) | All persistence | `backend/app/db.py` |
| Frontend | React + Vite | Read-only operator console; Policy Lab; execution trigger | `frontend/src`, `frontend/vite.config.js` |
| CI | GitHub Actions | Backend pytest + frontend lint/build on push/PR to `main` | `.github/workflows/ci.yml` |
| Payment provider | Razorpay (Test Mode only) | Payment Links (execution) + webhooks (verified outcomes) | `backend/app/razorpay_client.py`, `backend/app/razorpay_webhook.py` |
| AI provider | OmniRoute (OpenAI-compatible) | Advisory classification only; no authority | `backend/app/classifier.py` |

### 2.2 Architectural stance

- Single machine, single SQLite file, **no** message broker, cache, cluster, or background worker (`docs/DEPLOYMENT.md`). Execution occurs only on an explicit operator `POST`; there is no scheduler.
- No RecoveryOS business logic performs payment capture, card storage, or money movement. The only "money-moving" action is creating a Razorpay **Test Mode** Payment Link.
- Inbound data channels: one signature-verified webhook endpoint; seven operator routers gated by a bearer credential; two public health endpoints (`backend/app/main.py:71-90, 95-101`).

---

## 3. Data Classification

### 3.1 Data at rest (SQLite schema)

| Table | Stored fields (excerpt) | Classification |
|---|---|---|
| `payment_events` | event_id, order_id, payment_id, customer_id, amount_paise, currency, payment_method, failure_reason, bank, risk_flag, customer_history, timestamp | Transaction metadata + Razorpay identifiers; amounts in paise |
| `classification_results` | root_cause_category, confidence, reasoning, candidate_interventions | Advisory model output |
| `classification_failures` | attempt_count, last_failed_at, last_error | Operational diagnostics |
| `policy_decisions` | proposed_intervention, allowed, denial_reason, policy_rules_applied, evaluated_at | Deterministic audit trail |
| `intervention_attempts` | event_id, intervention, customer_id, cost_paise, attempted_at, status | Spend-cap/history ledger |
| `execution_outcomes` | intervention, execution_mode, status, external_reference, payment_link_id, reported_at | Execution audit trail |
| `execution_claims` | (event_id, intervention) claim, status, claimed_at, resolved_at | Concurrency guard |
| `optimizer_decisions` | decision, expected_value, estimator provenance (version/mode/reason) | Economic ranking audit trail |
| `webhook_deliveries` | delivery_id (PK), body_sha256, event_type, payment_link_id, status, received_at | Verified webhook idempotency/audit store |
| `webhook_recovery_outcomes` | delivery_id (PK), payment_link_id (UNIQUE), amount_paid_paise, recovered_at | Verified recovery evidence |
| `provider_payment_link_outcomes` | per-link provider-observed status | Provider polling evidence (not counted as recovery) |
| `estimator_calibration_snapshots` | version (PK, immutable), posterior, evidence | Calibration audit |
| `benchmark_runs` | seed, event_count, summary_json | Persisted benchmark summary (simulated) |

DDL: `backend/app/db.py:34-253`.

### 3.2 What RecoveryOS does **not** store

- **No cardholder data.** A repository-wide search for `PAN`, `CVV`, `card_number`, `expiry_*`, BIN, or bank-account numbers returns no application code or schema that captures them (verified 2026-09-09). Cards are charged on Razorpay-hosted pages; RecoveryOS sees only the payment method label (e.g. `card`) and a Razorpay `payment_id`.
- **No credentials or secrets.** Secrets exist only in the process environment, never in SQLite (see Section 5).
- **No free-form PII** such as names, email addresses, phone numbers, or postal addresses. The identifiers stored (`customer_id`, `order_id`, `payment_id`) are Razorpay-issued references.

### 3.3 Data in transit

- Inbound: webhook over HTTPS (TLS terminated at reverse proxy per `docs/DEPLOYMENT.md`); operator UI calls proxied through the same origin.
- Outbound: Razorpay SDK and OmniRoute HTTP client over HTTPS/validated base URL (`backend/app/classifier.py:130-158`). The OmniRoute base URL requires `https://` (or loopback `http://`) with a host; the single outbound sink is validated at the adapter boundary.

---

## 4. Security Architecture

### 4.1 Trust model

```
Razorpay Test Mode
  → Webhook / operator API (authenticated)        [INBOUND BOUNDARY]
  → Ingestion (idempotent, contract-validated)
  → Advisory AI classification (OmniRoute)         [NO authority]
  → Deterministic six-rule policy gate             [AUTHORITATIVE]
  → Economic optimizer / selector (allowed only)   [RANKING ONLY]
  → Bounded executor (Test Mode Payment Link)      [EXECUTION]
  → Verified payment_link.paid webhook             [OUTCOME ONLY]
  → Append-only audit trail + dashboard
```

### 4.2 Trust boundaries (implemented)

| Boundary | Enforcement | Evidence |
|---|---|---|
| Operator → API | Bearer credential, request-time lookup, constant-time compare, fail-closed 503/401 | `backend/app/auth.py:28-63`; applied to every operator router (seven of eight — the webhook router authenticates via its own signature gate) `backend/app/main.py:83-90` |
| Razorpay → webhook | HMAC-SHA256 over exact raw body, constant-time compare, fail-closed on missing secret/signature, verified **before** parsing | `backend/app/razorpay_webhook.py` (`verify_signature`); `backend/tests/test_webhook_api.py` |
| Model → system | AI output is advisory; executor re-derives policy on every execution; forged/denied decisions rejected; webhook path never executes | `backend/app/execution_service.py`, `backend/app/executor.py`; `backend/tests/test_policy_adversarial.py` |
| Webhook → recovery evidence | Recovery recorded only from a verified, correlated link; trusted `amount_paid` from provider | `backend/app/webhook_service.py`; `backend/tests/test_webhook_api.py` |
| Client → execution | Client cannot supply intervention/authorization/mode/evaluation-time; server derives all | `backend/app/routes/recovery.py`; 422 on forged bodies (`backend/tests/test_recovery_execute_api.py`) |

### 4.3 Fail-closed defaults (implemented)

- Authentication unconfigured → every operator endpoint returns **503**, refusing to serve (`backend/app/auth.py:33-40`).
- CORS allow-list empty by default → only same-origin proxying is allowed (`backend/app/config.py:83-93`, `backend/app/main.py:71-77`; `RECOVERYOS_CORS_ORIGINS` opt-in).
- Webhook secret unconfigured → webhooks fail verification rather than being accepted (`backend/tests/test_webhook_api.py::test_unconfigured_webhook_secret_fails_closed`).
- Razorpay credentials unconfigured → execution reports an explicit `configuration_missing`, never a fabricated link (`backend/app/routes/events.py`; Test Mode enforced in `backend/app/razorpay_client.py`).
- Malformed/malicious model output → explicit classification failure; at most one repair retry; never coerced (`backend/app/classifier.py:111-127`).
- Concurrent duplicate execution → SQLite primary-key claim ensures **at most one** provider side effect; a lost provider result is parked `PROVIDER_RESULT_UNKNOWN`, never blindly retried (`backend/tests/test_execution_claim.py`).

---

## 5. Secrets Management

### 5.1 Implemented controls

| Control | Detail | Evidence |
|---|---|---|
| Env-only secrets | `RECOVERYOS_OPERATOR_API_KEY`, `RAZORPAY_KEY_ID/SECRET`, `RAZORPAY_WEBHOOK_SECRET`, `OMNIROUTE_API_KEY` read from environment; `load_dotenv()` never overrides existing env | `backend/app/config.py:22, 58-81, 130-150` |
| Not committed | `.env`, `.env.*`, `*.db`, `*.sqlite*`, `.venv/`, `node_modules/` gitignored; only `.env.example` is tracked | `.gitignore:1-50`; `backend/.env.example` |
| No live keys allowed | `rzp_live_` key ids are rejected at the client boundary before any SDK call; only `rzp_test_` accepted | `backend/app/razorpay_client.py:149-186`; `backend/tests/test_razorpay_client.py` |
| Request-time, rotatable | Operator key read on every request (never cached); rotation takes effect immediately | `backend/app/auth.py:32`; `backend/tests/test_operator_auth.py` |
| Never stored in DB / responses | Secrets are not written to SQLite; `/health` and `/health/ready` report configuration as **booleans only**; `test_replay_api.py` asserts no secret substrings in API payloads | `backend/app/main.py:127-186`; `backend/tests/test_health.py`; `backend/tests/test_replay_api.py::test_the_api_never_exposes_a_secret` |
| No exception echo | Unexpected classification failures return a stable detail and log the real error server-side (`exc_info`) | `backend/app/routes/events.py`; `backend/tests/test_phase26_hardening.py::test_classify_failure_detail_is_stable_and_never_echoes_the_exception` |
| Outbound-sink validation | `OMNIROUTE_BASE_URL` must be HTTPS (or loopback HTTP) with a host; invalid values fail at adapter construction | `backend/app/classifier.py:130-158`; `backend/tests/test_phase26_hardening.py` (4 tests) |

### 5.2 Recommended production controls (gaps)

- **No secrets manager** (no Vault / cloud KMS / SSM). Environment variables with OS-level protections are the current mechanism. A production instance should inject secrets from a managed store and rotate them on a schedule.
- **No per-credential hashing.** The operator key is compared in constant time, but it is a shared secret rather than a per-user credential; there is no hashed-user credential store by design (see Section 11).
- **No secret-scanning gate in CI.** `.env` is gitignored, but the pipeline does not run a secret scanner (e.g. gitleaks/trufflehog) to fail a commit that accidentally includes material.

---

## 6. Payment Security & PCI DSS Boundary

### 6.1 The honest position

**RecoveryOS is not PCI DSS compliant, not PCI DSS validated, and has no SAQ.** This section explains the *structural* position of the product relative to cardholder data — it does not assert compliance with any PCI DSS requirement scope for the operator's environment.

### 6.2 Where cardholder data flows (and where it does not)

RecoveryOS **never captures, stores, processes, or transmits cardholder data (CHD) or sensitive authentication data (SAD)** — no PAN, no cardholder name from the card, no expiry, no CVV, no PIN block. A repo-wide search for `PAN`, `CVV`, `card_number`, `expiry`, and BIN fields returns nothing in application code or schema (verified 2026-09-09; Section 3.2).

Payments are conducted through **Razorpay Payment Links**: the payer completes the transaction on Razorpay-hosted pages (hosted payment page / checkout), and Razorpay — the payment processor — handles CHD within its own PCI DSS-compliant environment. RecoveryOS receives, after the fact:

- `payment.failed` webhook events containing the **payment entity metadata** (payment_id, order_id, amount, method label, failure reason) — never CHD.
- `payment_link.paid` webhook events containing link id and `amount_paid`.
- Provider `get_payment_link` status reads during operator-triggered recalibration — again metadata only.

### 6.3 Practical consequence

Because the design **completely outsources cardholder-data handling** to the processor (Razorpay), it is structurally consistent with the PCI DSS concept of a minimized scope for the system owner — **provided** the operator: (a) uses Razorpay's hosted payment experience (no iframe embedding of own scripts over card fields, no custom card forms, no storage of any CHD), and (b) does not add any card-capture/storage feature. The moment RecoveryOS (or code in this repo) captures or stores CHD, this whole analysis is void and full PCI DSS scope applies.

### 6.4 Confirmed disclaimers

- **Test Mode only.** All real execution uses Razorpay Test Mode keys (`rzp_test_`); live credentials are structurally rejected (`backend/app/razorpay_client.py:149-186`). No production payment processing is performed or implied (`docs/DEPLOYMENT.md` "Important honesty note").
- Razorpay's own PCI DSS attestation status is the responsibility of Razorpay; nothing here relies on or asserts it.
- Security of the payment experience depends on Razorpay's hosted checkout, which is outside this repository's control.

---

## 7. Privacy & Personal Data

### 7.1 What personal data may exist

RecoveryOS stores transaction metadata keyed by Razorpay identifiers. Whether `customer_id`, `order_id`, or `payment_id` are "personal data" depends on the operator's context: if those identifiers can be linked to natural persons (e.g. via the operator's CRM), they are personal data **by reference**, even though RecoveryOS holds no name/email/phone/address.

Fields stored that are plausibly personal-data-adjacent:
- `customer_id`, `order_id`, `payment_id` (referential identifiers; `payment_events`).
- `payment_method`, `bank`, `failure_reason`, `risk_flag`, `amount_paise` (payment behavior metadata).
- `customer_history` (prior-payment counts, subscription flag) — derived behavioral profile.

### 7.2 Processing characteristics

| Characteristic | Current state |
|---|---|
| Purpose limitation | Data used solely for revenue-recovery diagnostics, policy evaluation, and verified-outcome measurement; no advertising/analytics use |
| Minimization | Only decision-time event metadata; no free-form PII, no CHD |
| Third-party processing | Event metadata is sent to OmniRoute as the classifier prompt; payment data flows via Razorpay |
| Retention | No retention policy enforced in code; rows persist until the SQLite file is deleted (operational gap) |
| Erasure | No deletion/DSAR endpoints; data removal requires operator DB maintenance (operational gap) |
| Consent / lawful basis | Out of scope of this repo; an operator governed by GDPR/DPDPA must establish lawful basis and a DPA with each processor |

### 7.3 Recommended production controls (gaps)

- **Data Protection / Privacy Policy** and **DPA(s)** with Razorpay and OmniRoute.
- A **retention policy** (the schema currently keeps history indefinitely) and a **deletion/DSAR workflow**.
- A **DPIA / privacy review** before processing any data that can be linked to natural persons of customers.
- Consider whether sending event metadata to an external model gateway requires a data-processing agreement; the model gateway is a sub-processor of event data.

This section is an inventory, **not** a finding of GDPR / DPDPA compliance.

---

## 8. Auditability & Operational Controls

### 8.1 Durable, append-oriented audit trail

RecoveryOS persists a decision- and outcome-audit chain. Every stage of the pipeline leaves a durable, mostly-immutable record:

| Stage | Table | Why it is audit-grade |
|---|---|---|
| Event | `payment_events` | `event_id` PK; duplicates rejected; events are never overwritten or deleted by the app |
| Classification | `classification_results` (+ `classification_failures`) | Includes reasoning + candidate interventions; failures recorded durably and surfaced |
| Policy | `policy_decisions` | PK `(event_id, proposed_intervention, evaluated_at)`; denial reason persisted; deterministic rule order |
| Selection | `optimizer_decisions` | Records the estimator version/mode/reason with each decision (provenance, `backend/app/db.py:352-372`) |
| Execution | `execution_outcomes` + `intervention_attempts` | Execution mode, status, external reference (payment link id); spend costs persisted at real modelled values |
| Webhook | `webhook_deliveries` | Includes `body_sha256` to prove which exact body was delivered; delivery_id PK |
| Recovery | `webhook_recovery_outcomes` | Unique per `payment_link_id`; only written after signature verification + correlation |
| Calibration | `estimator_calibration_snapshots` | Versioned and **immutable** (version is PK, no update/delete path) |

Audit persistence is verified by tests, including: policy-persistence suites, optimizer-persistence (an audit write failure aborts the action — `backend/tests/test_optimizer_persistence.py`), and replay-safety (replay never mutates the DB byte-for-byte — `backend/tests/test_replay_safety.py::test_replay_does_not_write_to_any_database_table`).

### 8.2 Operational controls

- **Migrations at deploy time, never per request** (`backend/app/main.py:51-62`; `backend/app/db.py:318-332`). `init_db` is non-destructive and index/column-only.
- **Deterministic reproducibility** for the benchmark and demo dataset (`--seed`/`--count`; idempotent `populate`), so a persisted chain can be rebuilt identically.
- **Reconciliation is manual** (documented): provider polling runs only on explicit operator recalibrate; there is no background poll (`docs/AUDIT_REPORT.md` §9.3).

### 8.3 Recommended production controls (gaps)

- Append-oriented tables are not an **immutable/append-only ledger**: they use SQLite and can be modified by anyone with filesystem write access to the DB file.
- No audit of **who** issued an operator request (only the key is checked; no logged operator identity — see Section 11).
- No **tamper-evidence** (e.g. hash-chaining, signed log entries) beyond the per-body webhook hash.
- No formal **change management / approval workflow**.

---

## 9. Reliability & Resilience

### 9.1 Implemented controls

| Concern | Control | Evidence |
|---|---|---|
| Duplicate payments reaching execution | SQLite claim on `(event_id, intervention)` guarantees at most one provider side effect even under concurrent double-sends | `backend/app/execution_service.py`; `backend/tests/test_execution_claim.py` (2- and 6-thread races) |
| Duplicate webhook deliveries | `delivery_id` PK + `body_sha256`; same id+body → 2xx dedup; same id+different body → 409 conflict; crash-safe `claimed`→terminal retry | `backend/app/db.py:138-147`; `backend/tests/test_webhook_api.py`; `backend/tests/test_phase25_adversarial.py` |
| Duplicate recovery evidence | DB UNIQUE index on `payment_link_id` + `INSERT OR IGNORE` | `backend/app/db.py:442-472`; `backend/tests/test_phase25_hardening.py` |
| Concurrent duplicate ingestion | Race resolved as benign `DUPLICATE`, never a spurious 500 | `backend/app/ingestion.py:57-77`; `backend/tests/test_phase26_hardening.py` |
| Crash mid-execution | Claim parked as `PROVIDER_RESULT_UNKNOWN`, never auto-retried into a second real link | `backend/tests/test_execution_claim.py::test_a_lost_provider_result_is_reported_as_unknown_not_failed` |
| Crash in webhook processing | In-flight delivery reprocessed to completion; transient SQLite failures return 500 so the provider redelivers | `backend/app/routes/webhook.py`; `backend/tests/test_phase25_hardening.py` |
| Lock contention / readers-with-writer | `PRAGMA busy_timeout = 10000` and WAL journal mode per connection | `backend/app/db.py:254-277` |
| Indexed historical queries | 24h policy window bound by `attempted_at >= ?` + index; literal LIKE search (no wildcard probing) | `backend/app/db.py:475-487, 915-985, 996-1038` |

### 9.2 Documented / recommended production controls (gaps)

- **SQLite is a single-writer database.** This is a deliberate constraint for a control plane, documented in `docs/DEPLOYMENT.md`; it is not high-scale payment infrastructure and the repo does not claim otherwise.
- **No backup automation** — the DB is a single file and the operator must back it up; there is no scripted snapshot/restore in the repo.
- **No horizontal scale, no HA / failover**, no replication.
- **No load testing / capacity evidence** — 1697 tests cover behavior, not throughput.
- **Single point of failure** for the single-machine topology.

---

## 10. Logging & Monitoring

### 10.1 What exists today

- **uvicorn access logs** for the HTTP surface; webhook route logs each delivery id and processing disposition (`backend/app/routes/webhook.py`).
- **Durable diagnostics**: classification failures are recorded in `classification_failures` and surfaced in the operations queue (attempt count, last-failed time, reason) — a swallowed failure cannot silently stall the pipeline (`backend/app/db.py:61-74`; `backend/tests/test_phase25_hardening.py`).
- **Server-side error detail**: unexpected classification failures are logged with `exc_info=True` and returned to the client as a stable, non-echoing detail (`backend/app/routes/events.py`).
- **Readiness monitoring**: `GET /health` (liveness) and `GET /health/ready` (DB usability + per-integration configured booleans, no secrets) (`backend/app/main.py:95-186`).
- **Operational projection**: `/recovery/queue` surfaces held execution claims, provider-observed outcomes (clearly not webhook-verified), and diagnosis errors so anomalies are visible to the operator (`backend/app/recovery_operations.py`).

### 10.2 Recommended production controls (gaps)

- **No metrics/alerting** (no Prometheus/StatsD/Metrics API, no pager/notification path).
- **No centralized/structured log shipping**; logs are process stdout to the operator's host.
- **No trace correlation IDs** across the pipeline beyond `delivery_id`/`event_id` (which are usable correlation keys, but not a distributed tracing system).
- **No audit log of operator actions** (who executed, when, from which IP).
- **No log retention / rotation policy** enforced by the app.

---

## 11. Access Control

### 11.1 Implemented controls

| Control | Detail | Evidence |
|---|---|---|
| Operator authentication | Single shared bearer key (`Authorization: Bearer <key>`) on every operator/data router | `backend/app/auth.py`; `backend/app/main.py:83-90` |
| Fail-closed | Unconfigured key → 503; missing/invalid/non-Bearer/malformed → 401 with `WWW-Authenticate: Bearer` | `backend/app/auth.py:28-63`; `backend/tests/test_operator_auth.py` (9 tests) |
| Live rotation | Expected key read from env at request time (never cached) | `backend/app/auth.py:32`; `backend/tests/test_operator_auth.py` |
| Constant-time compare | `hmac.compare_digest` | `backend/app/auth.py:58` |
| Public boundaries only | `/health`, `/health/ready`, and the signature-verified webhook are the only unauthenticated endpoints | `backend/app/main.py:83-90, 95-101` |
| Cross-origin restriction | CORS allow-list empty by default (fail-closed); explicit opt-in via `RECOVERYOS_CORS_ORIGINS` | `backend/app/config.py:83-93`; `backend/tests/test_phase26_hardening.py` (S9) |

The same gate is exercised adversely in CI: missing/wrong/non-Bearer/malformed credentials → 401; valid → 200; rotation immediate; unconfigured → 503; health public; webhook bypasses the operator gate but is stopped by its own signature gate.

### 11.2 Documented deployment requirement

`docs/DEPLOYMENT.md` requires `RECOVERYOS_OPERATOR_API_KEY` for any operator deployment and states operator endpoints fail closed without it.

### 11.3 Important gap to record (verified)

**The shipped frontend does not present the operator credential.** `frontend/src/core/api.js` issues `fetch()` calls with no `Authorization` header, and a search of `frontend/src` finds no token handling, storage, or injection of the key. Consequences, verified against current code:

- With the key set, browser console calls to any operator endpoint receive `401` until a mechanism adds the bearer.
- Without the key set, operator endpoints answer `503` (fail-closed).
- Therefore an auth-gated deployment requires the operator console traffic to gain the credential **outside the app** — e.g. an authenticated reverse proxy / API gateway that injects `Authorization: Bearer <key>` for the `/api` upstream, or a future frontend change. The backend gate is fully tested; the frontend is not.

### 11.4 Recommended production controls (gaps)

- **No per-user accounts, roles, or least privilege** — one shared key for every operator action.
- **No MFA**, no session/identity provider, no user access review.
- **No per-operator audit identity** — all calls authenticate as "the key".
- **No network-level ACL enforcement in the app** (firewall/allow-list is deployment responsibility).
- A production deployment should also add HTTPS at the edge, rate limiting/IP allow-listing at the proxy, and server-side session or API-key-per-user if more than one operator exists.

---

## 12. Secure Development & Testing

### 12.1 Implemented controls

| Control | Detail | Evidence |
|---|---|---|
| Full automated test gate | Backend pytest suite + frontend oxlint + vite build in CI on push/PR to `main` | `.github/workflows/ci.yml` |
| Verified current pass rate | **1697 backend tests passed** (re-run 2026-09-09); frontend lint + build clean | `backend/` run output; this document's header |
| Pinned dependencies | `fastapi==0.136.1`, `uvicorn[standard]==0.43.0`, `pytest==9.1.1`, `httpx==0.27.0`, `python-dotenv==1.0.1`, `razorpay==2.0.1`; enforced by a test | `backend/requirements.txt`; `backend/tests/test_phase26_hardening.py::test_requirements_are_pinned_exactly` |
| Adversarial security suites | Operator auth, webhook signature/conflict/tamper, replay structural isolation, incident-safety structural checks, execution-claim races, Phase-25 attacker's pass | `backend/tests/test_operator_auth.py`, `test_webhook_api.py`, `test_webhook_payment_failed.py`, `test_replay_safety.py`, `test_incident_safety.py`, `test_execution_claim.py`, `test_policy_adversarial.py`, `test_phase25_adversarial.py`, `test_phase25_hardening.py`, `test_phase26_hardening.py` |
| Structural isolation of safety-critical paths | Replay/incident modules proven (by import/code scanning) unable to reach the provider, DB, or hidden ground truth | `backend/tests/test_replay_safety.py`, `backend/tests/test_incident_safety.py` |
| Input hardening | Literal LIKE search (no wildcard probing), bounded history queries, validated outbound URL, stable error details, no html-injection sinks in the React UI (no `dangerouslySetInnerHTML`, no `innerHTML`, no `eval`) | `backend/app/db.py:996-1038`; `backend/app/classifier.py:130-158`; `frontend/src` (grep) |
| Audit-driven regressions | Each `DEEP_AUDIT_REPORT.md` finding has a fix + regression test (`PRODUCTION_READINESS_REPORT.md`) | `backend/tests/test_phase26_hardening.py` |

### 12.2 Recommended production controls (gaps)

- **No SAST/DAST** in CI (no Semgrep/Bandit/Brakeman/OWASP ZAP etc.); only the handwritten adversarial suites exist.
- **No dependency/vulnerability scanning** (no `pip-audit`, OSV, Dependabot/Renovate, npm audit gate).
- **No SBOM generation**, no signed artifacts, no reproducible-build proof.
- **No security hardening tooling for the container/image** (no Dockerfile hardening, no network policy) — there is no container image defined in-repo.
- **No strict CSP or other browser security headers** are configured (headers are the reverse-proxy's job, not enforced here).

---

## 13. Compliance Framework Mapping

The matrix below maps RecoveryOS controls to common frameworks **descriptively**. It is informational only and is **not** a claim of compliance with, nor certification under, any framework.

| Framework | Area | Where RecoveryOS has substance | Where RecoveryOS falls short |
|---|---|---|---|
| PCI DSS v4 | Scope minimization / CDE | No CHD stored/processed; card flows fully outsourced to the processor's hosted CDE (Section 6) | No SAQ, no attestation, Test Mode only, no formal assessment |
| PCI DSS v4 | Authentication / access | Operator bearer auth, fail-closed | Single shared key; no per-user/MFA; web UI lacks key injection |
| SOC 2 (Trust Services) | Security (CC6, CC7) | Access control, fail-closed defaults, immutable-ish audit trail | No formal audit, no change management, no per-operator identity |
| SOC 2 | Availability (A1) | Crash-safe idempotency | Single machine, no HA, manual backups |
| SOC 2 | Confidentiality (C1) | Secrets env-only, no CHD, booleans-only health | Unencrypted SQLite file at rest; no DLP |
| ISO/IEC 27001 | Annex A.9 (access), A.12 (ops) | Request-time auth, deterministic operations | No ISMS, no documented policies/incident process |
| ISO/IEC 27001 | A.10 (crypto) | TLS at edge, validated outbound HTTPS | No encryption at rest; key management is manual env |
| GDPR | Minimization / purpose limitation | Minimal metadata, no free-form PII, no CHD | No retention policy, no DSAR path, no DPA artifacts |
| GDPR | Processor transparency | Event metadata goes to OmniRoute/Razorpay | No consent/legal-basis documentation, no DPIA |
| DPDPA (India, applicable domain) | Same minimization principles | As GDPR row | As GDPR row; no formal compliance review |

---

## 14. Production Compliance Gap Analysis

If an operator wanted to move from "readiness" toward an assessed claim, each row below would need to be closed. **None are closed today.**

| # | Gap | Severity for production claims | What would be required |
|---|---|---|---|
| 1 | Frontend cannot authenticate against the operator gate | High | Reverse-proxy/gateway credential injection, or a frontend auth mechanism (Section 11.3) |
| 2 | Single shared operator key; no per-user identity | High | Per-operator credentials, MFA, and an operator audit log |
| 3 | No encryption at rest (plain SQLite file) | High | Full-disk/volume encryption (e.g. LUKS, APFS FileVault, EBS encryption) or DB-field encryption; document key custody |
| 4 | No automated backup/recovery | High | Scheduled snapshot + restore test; RPO/RTO defined and documented |
| 5 | No centralized logging/alerting | High | Log shipping + SIEM/retention + alerting on auth failures, webhook failures, claim parking |
| 6 | No vulnerability/dependency scanning or SAST in CI | Medium-High | Add scanners; triage SLA; SBOM + signed release |
| 7 | No secrets manager / rotation | Medium | Managed secret store; rotation schedule; least-privilege access to env |
| 8 | Only Test Mode payment execution exists | High (for any payment claim) | No production-live mode exists by design; production claims are out of scope until a production integration is built and assessed |
| 9 | No retention or DSAR/deletion paths | Medium | Retention policy + deletion tooling |
| 10 | No DPA / privacy artifacts | Medium | DPAs with Razorpay and OmniRoute; AI-processing disclosure; DPIA if personal data |
| 11 | SQLite single-writer, single machine | Medium | If scale needed, move persistence boundary; document HA/RPO decisions |
| 12 | Manual reconciliation only | Medium | Operator-driven; needs a documented runbook and coverage of the `scan_limit` window |
| 13 | No formal assessment/certificate | N/A (decisive) | Engage a qualified assessor for SAQ/AOC/CAIQ (PCI/SOC 2/ISO 27001) relevant to the operator's obligations |

---

## 15. Deployment Security Checklist

Required by the repository's own docs plus the gaps above — the operator executes this before treating any environment as beyond-demo:

1. **Set `RECOVERYOS_OPERATOR_API_KEY`** to a long random value (fail-closed requirement). `docs/DEPLOYMENT.md`.
2. **Terminate TLS at the edge** (reverse proxy) and route `/api` to FastAPI; keep CORS unset unless the frontend is intentionally cross-origin (`RECOVERYOS_CORS_ORIGINS`). `backend/.env.example`.
3. **Inject the operator bearer for the UI** (reverse proxy/API gateway) so the shipped frontend can read gated endpoints (Section 11.3).
4. **Serve secrets from a managed store**, not flat env, for any sustained deployment; rotate on a schedule.
5. **Restrict network exposure** (firewall/security group to the proxy, no public DB port — SQLite is a local file, keep it so).
6. **Store the SQLite file on persistent, encrypted storage**; configure automated backups and test a restore.
7. **Use Razorpay Test Mode keys only**; verify `rzp_test_` / `rzp_live_` values in the runtime env (the client rejects live keys by design).
8. **Register the webhook HTTPS URL** and set `RAZORPAY_WEBHOOK_SECRET`; note public-tunnel hostnames are ephemeral for demos (`docs/DEPLOYMENT.md`).
9. **Keep `OMNIROUTE_BASE_URL` at the default** or an operator-owned HTTPS endpoint; loopback-HTTP is only for local dev.
10. **Verify** `GET /health/ready` returns DB usable + desired configuration booleans **before** operator use.
11. **Add monitoring/alerting** on: auth failures, webhook processing failures, `PROVIDER_RESULT_UNKNOWN` claim parking, and deployment health.
12. **Keep dependencies pinned** (already enforced in-repo) and run CI on every change (already enforced).

---

## 16. Evidence Index

| Claim in this document | Primary evidence |
|---|---|
| Operator auth, fail-closed, constant-time, request-time | `backend/app/auth.py:28-63`; `backend/tests/test_operator_auth.py` |
| CORS fail-closed default | `backend/app/config.py:83-93`; `backend/app/main.py:71-77`; `backend/tests/test_phase26_hardening.py::test_cors_allow_list_is_empty_by_default` |
| Webhook replay protection + signature | `backend/app/razorpay_webhook.py`; `backend/app/db.py:138-147`; `backend/tests/test_webhook_api.py` |
| Recovery only from verified webhook, unique per link | `backend/app/webhook_service.py`; `backend/app/db.py:442-472`; `backend/tests/test_phase25_hardening.py` |
| At-most-once execution under concurrency | `backend/app/execution_service.py` (claims); `backend/tests/test_execution_claim.py` |
| Spend cap wired to real economic costs | `backend/app/config.py:170-210`; `backend/tests/test_phase26_hardening.py::test_spend_cap_uses_the_economic_model_costs` |
| `payment.failed` observation-time handling | `backend/app/failed_payment_ingestion.py`; `backend/tests/test_phase26_hardening.py` (S2) |
| Migrations at deploy time, non-destructive init | `backend/app/db.py:285-332`; `backend/app/main.py:51-62` |
| LIKE search is literal | `backend/app/db.py:996-1038`; `backend/tests/test_phase26_hardening.py::test_like_search_is_literal_not_wildcard` |
| Bounded policy history queries | `backend/app/db.py:915-985`; index `idx_intervention_attempts_attempted_at` (`backend/app/db.py:475-487`) |
| No exception echo | `backend/app/routes/events.py`; `backend/tests/test_phase26_hardening.py::test_classify_failure_detail_is_stable_and_never_echoes_the_exception` |
| Outbound URL validation (SSRF surface) | `backend/app/classifier.py:130-158`; `backend/tests/test_phase26_hardening.py` (S5) |
| WAL + busy_timeout | `backend/app/db.py:254-277` |
| Live keys rejected | `backend/app/razorpay_client.py:149-186`; `backend/tests/test_razorpay_client.py` |
| Secrets not committed / not in DB / booleans-only health | `.gitignore:1-50`; `backend/.env.example`; `backend/app/main.py:127-186`; `backend/tests/test_health.py` |
| No CHD stored | Repo-wide grep for PAN/CVV/card-number/expiry fields: zero application/schema hits (verified 2026-09-09) |
| Replay/incident safety (simulation-only, structural) | `backend/tests/test_replay_safety.py`, `backend/tests/test_incident_safety.py` |
| 1697 backend tests / frontend clean | Re-run 2026-09-09 (`backend/` pytest; `frontend/` oxlint + vite build); `.github/workflows/ci.yml` |
| Audit-chain tables and immutability (calibration snapshots) | `backend/app/db.py:34-253, 245-253`; `backend/tests/test_optimizer_persistence.py` |
| Frontend lacks operator-credential injection (gap) | `frontend/src/core/api.js:10-61` (no Authorization header); grep of `frontend/src` for token/storage: zero hits |

---

## 17. Security & Compliance Limitations

RecoveryOS, as of 2026-09-09, explicitly does **not** provide or claim any of the following:

1. **PCI DSS compliance, SAQ, or attestation** for RecoveryOS or its operator.
2. **SOC 2 report, ISO 27001 certification**, or any other third-party security attestation.
3. **GDPR / DPDPA / data-protection compliance** or a DPA/DPIA artifact set.
4. **Production payment processing** — Razorpay Test Mode only; live keys are structurally rejected; benchmark figures are simulated.
5. **Per-user identity, MFA, or role-based access control** — one shared bearer key; no audit of which operator acted.
6. **A frontend that presents the operator credential** — the console cannot read gated endpoints without an external credential-injection path.
7. **Encryption at rest** for the SQLite file; **automated backup/recovery**; **HA/failover**; or **scale beyond single-writer SQLite**.
8. **Centralized logging, metrics, alerting, or audit retention**.
9. **Vulnerability/SAST/DAST scanning or an SBOM**; supply-chain hardening beyond pinned exact versions.
10. **Retention, erasure, or DSAR tooling**; **access reviews**; **formal change management**.
11. **A production-capable monitoring/tamper-evident audit ledger** — the audit trail is engineering-grade, not forensic-grade.
12. Any evidence drawn from the repository may serve as an input to, but is not a substitute for, an assessment performed by a qualified auditor.

---

## 18. Conclusion

RecoveryOS is a **security-conscious control plane, not assessed payment infrastructure**. Its real strengths are structural and verifiable: an inbound webhook boundary verified by HMAC over the raw body before parsing; fail-closed operator authentication and CORS; a deterministic policy gate that makes the AI advisory by construction; durable idempotency that provably prevents duplicate execution, duplicate recovery, and spun-up provider calls under concurrency; a Test-Mode-only execution boundary that rejects live keys; an env-only, never-committed, never-echoed secrets model; and an append-oriented audit chain — all held green by **1697 tests** and CI on every change.

Its honest boundaries are equally structural: single-machine SQLite with no HA or automated backup, no encryption at rest, no centralized monitoring, no per-user identity, a frontend that does not (yet) carry the operator credential, and **no formal compliance assessment of any kind**. Every "recommended production control" and gap row in this document can be satisfied, but only by the operator — and none of them are satisfied by reading this document, which is an inventory and a map, not a certificate.
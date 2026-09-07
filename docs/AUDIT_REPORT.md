# RecoveryOS — Phase 25 Reliability & Security Audit Report

**Audit date:** 2026-09-07 · **Branch:** `main` · **Head:** `67cebfb`
**Verification:** backend `python -m pytest` → **1670 passed** (14 fix regression tests + 16 adversarial tests) · frontend `npm run lint` + `npm run build` → clean.

---

## 1. Executive summary

RecoveryOS is a deliberately **operator-driven** revenue-recovery control plane:
event ingestion → advisory AI classification → deterministic six-rule policy
gate → V2 economic optimizer → bounded execution (Test Mode Payment Link) →
verified `payment_link.paid` webhook (OUTCOME) → out-of-band adaptive
feedback. There is **no background worker, scheduler, or queue**; execution
only ever happens on an explicit operator `POST`, and the AI never holds
authority. The architecture is sound and its hard invariants hold: recovery is
claimed **only** from a signature-verified webhook, monetary execution is
Test-Mode-only, and the operator chooses *whether* to act, never *what* to do.

The audit found **two P0 correctness defects**, one **P1 reliability gap**, and
one **P1 observability gap**. All four were reproduced, fixed, and locked with
regression tests:

1. **[P0] `payment.failed` ingestion could silently drop a failed payment**
   (transient DB error → terminal 2xx → lost forever).
2. **[P0] A payment link's recovery could be double-recorded** (no uniqueness
   on `payment_link_id`).
3. **[P1] A failed auto-diagnosis silently stalled the pipeline**
   (event stuck at `NOT_CLASSIFIED`, no retry path, failure only in a log).
4. **[P1] The operations queue hid provider-observed terminal outcomes and
   in-flight/stuck execution claims.**

No new frameworks, workers, or queues were introduced. Fixes are root-cause,
minimal, and additive; every behavioral change is covered by a regression test.

---

## 2. Critical findings

### 2.1 [P0 — FIXED] INGESTION channel drops events on transient DB failure

- **Component:** `backend/app/webhook_service.py::process_payment_failed`
- **Root cause:** when `ingest_event` returned `IngestionStatus.ERROR` (a
  transient SQLite failure) or when `sqlite3.Error` escaped the mapping/
  persistence step, the delivery row was marked terminal (`ingested`) **and** a
  **2xx** was returned for the ERROR case. Razorpay therefore never redelivered,
  and the failed payment was permanently lost from the recovery pipeline. The
  OUTCOME channel (`_correlate_supported_event`) already returned an error
  without marking the delivery terminal — the INGESTION channel was
  inconsistent.
- **Impact:** a genuinely failed payment (real lost revenue) silently
  disappears from the queue; no operator can ever recover it.
- **Reproduction:** a monkeypatched `ingest_event` returning ERROR produced
  `ignored` + delivery `ingested` + retry claimed as `deduplicated` — the event
  was gone. Script preserved under
  `/var/folders/…/T/opencode/repro/repro_ingestion_error.py`.
- **Fix:** ERROR results and `sqlite3.Error`-raised mapping/persistence failures
  now **leave the delivery in-flight** (`claimed`) and return
  `persistence_failure` (HTTP 500). Everything remains crash-safe: the retry
  re-enters as `in_flight`, reprocesses, and completes. Non-recoverable
  outcomes are unchanged: `DUPLICATE` → terminal 2xx `duplicate_event`,
  `INVALID` → terminal 2xx. Docstring updated to document the new contract.
- **Regression tests:** `test_phase25_hardening.py`
  - `test_ingestion_error_result_leaves_delivery_in_flight_and_retry_completes`
  - `test_payment_failed_sqlite_error_on_map_leaves_delivery_recoverable`
  - `test_payment_failed_ingestion_error_is_http_500_then_redelivery_succeeds`
  - `test_non_recoverable_ingestion_results_stay_terminal`
- **Verification:** 1670 passed; existing `test_webhook_payment_failed.py`
  contract tests still green (including `test_ingestion_survives_classifier_failure`).

### 2.2 [P0 — FIXED] Recovery double-count for one Payment Link

- **Component:** `backend/app/db.py` (`webhook_recovery_outcomes`)
- **Root cause:** the row was keyed only by `delivery_id`
  (X-Razorpay-Event-Id). A `payment_link.paid` redelivered under a *second*
  delivery id inserted a second recovery row for the same `payment_link_id`,
  which could double-count recovered revenue in recovery-intelligence /
  calibration evidence. The sibling table `provider_payment_link_outcomes`
  already used `payment_link_id` as PK.
- **Impact:** inflated recovery evidence can poison the adaptive estimator's
  calibration and mislead the Recovery Intelligence screen.
- **Fix:** a `UNIQUE` index on `payment_link_id`, created by the idempotent
  migration `_init_webhook_recovery_outcome_uniqueness` (runs inside
  `init_db`). The migration first **collapses any historical duplicate rows** to
  the newest per link (window-function delete with a legacy-SQLite fallback
  self-join), then installs the index so the database — not application state —
  rejects double-counting. `INSERT OR IGNORE` makes a duplicate write a safe
  no-op.
- **Regression tests:**
  - `test_two_deliveries_for_the_same_link_record_one_recovery`
  - `test_uniqueness_migration_collapses_historical_duplicates`

---

## 3. Security findings

- **[Note — not a new vulnerability]** The operator endpoints
  (`POST /events/{id}/execute`, `/recovery/{id}/execute`, recalibrate) carry
  **no authentication/authorization**. Anyone reachable on the network can
  trigger an execution. Money impact is bounded to Razorpay **Test Mode** (live
  keys are rejected), and the webhook gate is separate, but this is the correct
  thing to harden before any real deployment. **Not auto-fixed** because the
  frontend calls these endpoints with no auth today — silently adding auth
  would break the shipped UI contract. Recommended: terminate the backend
  behind an authenticated reverse proxy / API-gateway layer out of scope of the
  application itself.
- **[Verified — sound]** Webhook authenticity is fail-closed: HMAC-SHA256 over
  the exact raw body, verified **before** parsing, constant-time compare,
  missing signature → 400, bad signature → 401; verified deliveries are never
  re-played (PRIMARY KEY) and a replayed id with a different body is an explicit
  409 `conflict`. The `payment.failed` INGESTION channel shares this gate and
  never authorizes or executes anything.
- **[Verified — sound]** No secrets are committed: `backend/.env`
  (Test-Mode-only credentials) is gitignored; only `.env.example` is tracked.
  No `rzp_live_` / `sk_live_` / AWS-style secrets anywhere in tracked files.
- **[Know your model]** The advisory classifier is advisory by construction:
  the executor re-derives the policy decision on every execute and only ever
  runs a candidate that carries an authoritative ALLOW. A malicious or
  hallucinating model output cannot select, authorize, or execute anything.

---

## 4. Reliability findings

### 4.1 [P1 — FIXED] Swallowed auto-diagnosis failures stalled the pipeline

- **Component:** `webhook_service._auto_classify_best_effort` +
  `routes/events.py::classify_event_endpoint` + `recovery_operations` +
  frontend `RecoveryOps.jsx`
- **Root cause:** a classifier/model/persistence failure during the automatic
  post-ingestion diagnosis was logged and dropped. The event remained ingested
  but `NOT_CLASSIFIED`, with `execution` returning `missing_classification`
  (422). The queue showed an inert "Not diagnosed" row with **no action** and no
  way to re-run the diagnosis. Descriptionally, a payment executed nothing and
  sat stuck forever.
- **Impact:** the pipeline terminated midway with no alert, no retry, and no
  operator affordance — exactly the "failures swallowed / states stuck forever"
  failure class.
- **Fix (additive, contract-preserving):**
  - New durable table `classification_failures` (`event_id` PK,
    `attempt_count`, `last_failed_at`, `last_error`).
  - `_auto_classify_best_effort` and the manual classify endpoint now record a
    failure row (`record_classification_failure`) and clear it on success
    (`clear_classification_failure`). Recording a failure can never fail the
    webhook ingestion.
  - The ops queue row gains `diagnosis_error` (only when a classification is
    absent) — attempt count, last-failed time, and reason.
  - Frontend: a NOT_CLASSIFIED row with `diagnosis_error` shows "Diagnosis
    failed · N attempts · <reason>" and a **Retry diagnosis** button wired to
    `POST /events/{id}/classify` (advisory only — never selects/executes).
  - Existing contract preserved: `test_ingestion_survives_classifier_failure`
    still asserts the event is ingested and webhook returns 2xx during an
    outage.
- **Regression tests:** `test_auto_classify_failure_is_recorded_not_just_logged`,
  `test_successful_classification_clears_the_failure_record`,
  `test_queue_surfaces_diagnosis_error`,
  `test_queue_surfaces_no_diagnosis_error_once_classified`.

### 4.2 [P1 — FIXED] Ops queue hid provider-observed terminal link outcomes

- **Component:** `executor.py` / `razorpay_client.py` / `calibration_service.py`
  boundaries → `recovery_operations._outcome_view`
- **Root cause:** calibration already persists provider-polled terminal outcomes
  (`provider_payment_link_outcomes`, one row per link). But the operations queue
  consulted only executions + webhook recoveries, so a link that the provider
  had already settled (paid/expired) while the webhook never arrived still read
  "waiting for payment" forever.
- **Fix:** the queue row now carries an additive `provider_outcome` field
  (`status`, `outcome`, `observed_at`, plus a note that it is provider-observed
  and **not** webhook-verified). The authoritative `outcome` (recovered amounts)
  is unchanged — a link is never declared recovered without a verified webhook.
- **Regression tests:** `test_queue_surfaces_provider_outcome_without_claiming_recovery`,
  `test_queue_wiring_surfaces_provider_outcome_and_claim_end_to_end`.

### 4.3 [P1 — FIXED] Held execution claims were invisible

- **Component:** `execution_service.py` / `db.claim_execution`
- **Root cause:** a crash between claiming an execution and durably writing its
  outcome leaves a `claimed` row; the duplicate-execution guard then returns
  `execution_in_progress` (409) forever, with no queue indication of *why*.
- **Fix:** the queue row surfaces an additive `claim` field (status
  "claimed", intervention, mode, claimed_at) so the operator can distinguish
  "executing now" from "stuck/crashed and must be reconciled". Completed claims
  are not surfaced.
- **Regression tests:** `test_queue_surfaces_held_execution_claim`,
  `test_queue_hides_resolved_and_absent_claims`,
  `test_queue_wiring_surfaces_provider_outcome_and_claim_end_to_end`.

---

## 5. Payment pipeline (verified lifecycle)

1. `payment.failed` webhook → **OUTCOME/INGESTION split**, HMAC fail-closed.
2. **Claim** (delivery id PK) → map → **ingest** → mark ingested → best-effort
   **auto-diagnose** (failures now durable & re-drivable).
3. Operator `POST /events/{id}/execute`: server re-derives policy from persisted
   history, runs the **six-rule deterministic gate**, ranks with the V2
   optimizer, **executes** via the correct mode, resolves the claim, persists
   the outcome.
4. `payment_link.paid` webhook → correlated recovery (INSERT OR IGNORE,
   now unique per link) → recovery-intelligence feedback + evidence-calibrated
   adaptive estimator.
5. Ops queue projects the whole story and now surfaces diagnosis failures,
   provider-observed settlements, and held claims.

Verified invariants that remain true after this audit: SIMULATED never shows a
recovered amount; a forgotten webhook reads as waiting (honest), now with
provider-side evidence when available; a provider-result-unknown execution is
never retried blindly and never reported as recovered.

## 6. Failure recovery (verified)

- **Provider failure:** deliverable noted; no auto-retry (safe, not flaky).
- **Webhook redelivery:** crash-safe claim semantics — `claimed` rows are
  reprocessed (`in_flight`); terminal rows are deduplicated; conflicting bodies
  are 409.
- **Worker/crash:** there is no worker; a crash mid-execution leaves a claim,
  now surfaced to the operator (4.3).
- **DB failure:** the ingestion channel now surfaces transient SQLite failures
  as 500 so Razorpay redelivers (2.1); recovery writes are idempotent.
- **Analysis/API timeout:** classifier timeouts are recorded durably and are
  retryable from the queue (4.1).
- **Microservice/LLM outage:** ingestion never depends on classification
  success; the effect is a visible, re-drivable `diagnosis_error`.

## 7. Adversarial verification (attacker's pass)

After the four fixes shipped, an adversarial pass attempted to break **each
one** from outside the happy path, with no changes to architecture and no new
features: duplicate webhook deliveries (sequential *and* concurrent from two
processes), concurrent recovery outcomes for a single Payment Link, a
classifier crashing at **every** point, DB failures at **every** transaction
boundary, worker/process crashes between commits, retries after partial
success, and unauthorized access to **every** operator endpoint. New file
`backend/tests/test_phase25_adversarial.py` (16 tests).

The four fixes **survived**; three related defects were exposed and fixed:

1. **Manual-classify partial-success retry loop (Fixed).** A prior attempt that
   persisted a classification but crashed before its 200 left the retry hitting
   a UNIQUE `IntegrityError` → `classification_persistence_failure` (500)
   **forever**. The endpoint now confirms the durably persisted classification
   and answers 200 `classification_success`; a genuinely unpersisted write still
   answers 500 `classification_persistence_failure`.
   Regression: `test_manual_classify_partial_success_retry_recovers_not_500_forever`;
   `test_classify_api.py` split into the recovery case and the genuine-failure case.
2. **Auto-classify clear-crash false failure (Fixed).** A crash *after*
   persisting a classification but *before* clearing its failure record
   fabricated a `persistence_failed` row even though the classification is
   durable. The clear crash is now logged and never invents a failure.
   Regression: `test_auto_classify_clear_crash_does_not_fabricate_a_failure`.
3. **Opaque 500 at the crash boundary (Fixed).** A SQLite error between the
   committed event insert and the delivery's terminal status write surfaced an
   unmodeled FastAPI 500 instead of the canonical `persistence_failure`. The
   webhook route now maps any `sqlite3.Error` during processing to
   `persistence_failure` (500) so Razorpay's redelivery is driven by the retry
   contract. Regression:
   `test_crash_at_status_write_is_a_canonical_http_500_then_redelivery_succeeds`.

Invariants that survived **untouched** (verified, no change needed):

- Duplicate delivery (sequential) → exactly one event, 2xx `deduplicated`;
  same delivery id with a **different** body → 409 `conflict`, never overwritten.
- Concurrent same-delivery ingestion (two connections) → exactly one event; the
  loser legitimately resolves via `in_flight`→`duplicate_event`, or
  `deduplicated` if the winner already finished; a still-`claimed` delivery
  completes on redelivery.
- Concurrent recovery outcomes for one Link under two delivery ids → exactly
  one row (the unique index).
- Classifier crashes at adapter/model/persist/failure-recording → ingestion
  always 200 `ingested`; failure records stay durable **and honest**.
- Crash between event insert and status update, and between recovery insert
  and status update → redelivery converges to exactly-once.
- Unauthorized probes: missing/bad/wrong-secret webhook signatures rejected
  **before parsing** (400/401, zero rows written); `execute` ignores forged
  `intervention`/`authorized`/`execution_mode` bodies (state-determined 422
  until classified; nothing executes); classify/policy/execute on ghost events
  → 404; a live `rzp_live_` key is rejected at the client boundary and surfaces
  a controlled 500 at HTTP — nothing executes.

## 8. Testing

- Backend full suite: **1670 passed** (was 1653; +16 adversarial tests, +1
  classify-persistence test split).
- Frontend: `npm run lint` and `npm run build` clean.
- CI (`.github/workflows/ci.yml`) runs backend pytest + frontend lint/build on
  push/PR to `main` — includes the new tests automatically.

## 9. Remaining risks (honest)

1. **No auth on operator endpoints** (Section 3) — the most important remaining
   item before any non-demo deployment. Out of scope here by design.
2. **`scan_limit=500`** on the queue scan is the scan window; a very old stuck
   event can fall outside the default window. Workaround: narrower filters;
   consider raising or paginating when the operator outgrows it.
3. **Reconciliation is still manual.** Provider polling runs only during an
   explicit operator recalibrate; there is no automatic poll. The queue now
   surfaces *already-persisted* provider outcomes but does not by itself poll.
   Do not add a background poll without an explicit operator decision — it
   would be the one new "background worker" the system deliberately lacks.
4. **Webhook indiscrepancy edge:** if a link's `payment_link.paid` arrives
   under two different delivery ids, the second is now a silent no-op (2.2);
   the operator notices only if they diff webhook deliveries. Low risk.
5. **Test Mode is not production payment processing** — the repo's own standing
   claim, reproduced here for the record.

---

## Appendix A — Files changed

| File | Change |
|---|---|
| `backend/app/webhook_service.py` | Fix 1 (ingestion persistence-failure contract) + Fix 3 auto-classify failure recording; adversarial fix: clear-crash never fabricates a failure |
| `backend/app/db.py` | `classification_failures` table + helpers; `webhook_recovery_outcomes` UNIQUE migration (Fix 2); bulk claim reader |
| `backend/app/recovery_operations.py` | `diagnosis_error`, `provider_outcome`, `claim` row fields (Fixes 3/4) |
| `backend/app/routes/events.py` | classify endpoint records/clears durable failures; adversarial fix: partial-success retry recovers with the persisted classification |
| `backend/app/routes/webhook.py` | adversarial fix: any `sqlite3.Error` during processing surfaces canonical `persistence_failure` (500) |
| `backend/tests/test_phase25_hardening.py` | 14 new regression tests (Fixes 1–4) |
| `backend/tests/test_phase25_adversarial.py` | 16 new adversarial tests (attacker's pass) |
| `backend/tests/test_classify_api.py` | persistence-failure test split: partial-success recovery vs genuine-failure 500 |
| `frontend/src/core/api.js` | `classifyEvent` API helper |
| `frontend/src/components/RecoveryOps.jsx` | "Diagnosis failed" cell + **Retry diagnosis** action |
| `frontend/src/App.css` | `.ops-failed` style |
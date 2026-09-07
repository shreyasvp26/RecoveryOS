# RecoveryOS — Deep Full-Codebase Audit Report

Date: 2026-09-07
Scope: every file under `backend/app`, `backend/tests`, `frontend/src`, `docs`, `.github/workflows`.
Method: six parallel domain audits (security surface, webhook/ingestion integrity, data layer, policy/executor/optimizer, classifier/calibration/feedback, frontend/deployment/CI) plus **independent re-verification of every high-impact claim by direct code reading and a live-server probe**. No code was changed in this audit.

Verification evidence: `backend: 1670 passed` (pytest), `frontend: lint + build clean`, plus a live `uvicorn` probe against the real app (see §4.1).

---

## 1. Executive summary

The core safety architecture holds: no client can choose an intervention/mode/amount/evaluation-time on any execution path; every selected candidate is backed by an authoritative `allowed==True` policy decision; the executor independently rejects forged decisions; REAL execution is Test-Mode-only through a genuine Razorpay client; concurrent duplicates are blocked by a DB primary-key claim; recovery is only recorded from a signature-verified webhook.

But a full read of the surface reveals **one critical production-blocking gap, four high/medium integrity gaps, and a set of medium/low hardening items** — plus two sub-agent findings that are **refuted** (a claimed frontend XSS, and an overstated "live-key client leak").

| # | Severity | Finding | Proof |
|---|----------|---------|-------|
| 1 | 🔴 CRITICAL | **No authentication on any operator/data endpoint** | main.py:39-47 (no middleware), routes use only `Depends(get_db)`; live probe confirmed 200/201/202 with zero credentials |
| 2 | 🔴 HIGH | **Spend-cap policy rule is structurally inert** | config.py:145-149, policy.py:124-128,485-486, execution_service.py:444 |
| 3 | 🟠 HIGH | **Verified `payment.failed` without `created_at` → permanent 500 loop, event never ingested** | failed_payment_ingestion.py:113-114 + models.py:118-122, routes/webhook.py:154-175 |
| 4 | 🟠 MEDIUM | **Per-request full-table DELETE migration** (`init_db` on every connection) | routes/webhook.py:57-64, db.py:356-397 |
| 5 | 🟠 MEDIUM | **CORS deployment doc contradicts code** | docs/DEPLOYMENT.md:56-57 vs. zero CORSMiddleware anywhere |
| 6 | 🟠 MEDIUM | **Unpinned dependencies / non-reproducible builds** | backend/requirements.txt:1-6 |
| 7 | 🟡 LOW-MED | **LIKE wildcard injection** in dashboard search | db.py:905-911 |
| 8 | 🟡 LOW-MED | **`get_policy_history` full-table scan per decision** | db.py:844-850 |
| 9 | 🟡 LOW-MED | **Broad `except Exception` echoes internal error text** to clients | routes/events.py:204-213 |
| 10 | 🟡 LOW | **`OMNIROUTE_BASE_URL` env-driven SSRF surface** (config-only) | config.py:62-64, classifier.py:144,170-172 |
| 11 | 🟡 LOW | **No busy_timeout/WAL; multi-commit execution, no outer transaction** | db.py:263, execution_service.py:436-451 |
| 12 | 🟢 INFO/COSMETIC | Claim-loser mislabeled `ALREADY_EXECUTED` in a narrow window | execution_service.py:241-247 |
| 13 | ⚪ REFUTED | Frontend XSS via classifier reasoning | EventTrace.jsx:401, RecoveryOps.jsx:199 — React auto-escapes; no `dangerouslySetInnerHTML`/`innerHTML` in the repo |
| 14 | ⚪ REFUTED | Live/Test-mode key leakage risk overstated | razorpay_client.py:178-187 actually rejects `rzp_live_` |

---

## 2. CRITICAL findings

### 2.1 🔴 No authentication on any operator/data endpoint

`app/main.py` mounts eight routers with zero security middleware, and no route declares any auth dependency (the only `Depends(...)` in the whole app are `get_db`, `get_classifier`, `get_policy_config`, `get_now`, `get_razorpay_client`, `get_provider` — none authenticate):

```python
# main.py:39-47
app = FastAPI(title="RecoveryOS API", version="0.1.0")
app.include_router(events_router)
app.include_router(dashboard_router)
app.include_router(webhook_router)
...
# no middleware, no auth dependency anywhere
```

Confirmed by grep: the app contains `HTTPBearer|HTTPBasic|OAuth2|get_current_user|Authorization` only as *outbound* artifact in `classifier.py:148` (the OmniRoute bearer) and never as an inbound gate.

**Live proof (unauthenticated requests, all 2xx):**

```
GET  /events               → 200   (event list, customer/order/payment ids)
POST /events               → 201   (anyone can ingest arbitrary events)
POST /events/e1/execute    → 422   (reached validation: no auth gate before it)
GET  /recovery/queue       → 200   (operator queue: customer ids, diagnosis, policy, links)
GET  /estimator-evidence   → 200   (calibration snapshots + evidence provenance)
GET  /health/ready         → 200
```

Impact: any internet-reachable instance exposes financial-intelligence data, lets anyone poison the ingestion pipeline and the calibration dataset, and lets anyone drive `REAL_RAZORPAY` payment-link execution (test mode) or read/recover the operating history. REPLACING this must be deliberate (e.g. an operator API key/bearer), but per the task this audit lists it, it does not fix it.

---

## 3. HIGH findings

### 3.1 🔴 Spend-cap policy rule is structurally disabled

`RULE_SPEND_CAP` can never trigger in production configuration:

```python
# config.py:145-149  — build_policy_config never sets intervention_cost_paise
return PolicyConfig(
    max_interventions_per_customer_24h=...,
    event_cooldown_minutes=...,
    daily_spend_cap_paise=...,
    # intervention_cost_paise NOT provided → default all-zero mapping below
)
```

```python
# policy.py:124-128  — default maps every intervention to cost 0
intervention_cost_paise: Mapping[str, int] = field(
    default_factory=lambda: {
        intervention: 0 for intervention in CANDIDATE_INTERVENTIONS
    })
```

Every spend-cap input therefore evaluates to zero:

```python
# policy.py:485-486
proposed_cost = config.intervention_cost(input.proposed_intervention)   # always 0
if history.existing_daily_spend_paise + proposed_cost > config.daily_spend_cap_paise:
    return denied(RULE_SPEND_CAP)
```

and the persisted cost written for each attempt is also `config.intervention_cost(selected) == 0` (execution_service.py:443-444), so `existing_daily_spend_paise` (db.py:860) is always 0 too. The real economic costs (`₹0.20` reminder, `₹1.00` payment_link — economics.py:216-226) live in `DEFAULT_ECONOMIC_MODEL`, which is separate from the policy gate and never wired in. **Effective consequence: five of the six "authoritative" rules are live; the spend-cap claim in the design docs is not enforced.** Even if the costs were wired, the default cap of ₹50,000/day vs. 0–100 paise per attempt would be practically unreachable.

### 3.2 🔴 `payment.failed` ingestion permanent 500 loop when `created_at` missing

The ingestion mapper's timestamp fallback violates its own validated contract:

```python
# failed_payment_ingestion.py:113-114
timestamp=failed.failed_at
or "1970-01-01T00:00:00+00:00",
```

```python
# models.py:118-122  — a midnight timestamp is rejected
parsed = datetime.fromisoformat(self.timestamp)
if not (parsed.hour or parsed.minute or parsed.second or parsed.microsecond):
    raise ValueError("timestamp must include a time component (ISO8601 date-time)")
```

`"1970-01-01T00:00:00+00:00"` has hour=minute=second=microsecond=0 → `PaymentEvent(...)` raises `ValueError`. In `webhook_service.process_payment_failed` the surrounding `try` catches **only `sqlite3.Error`** (webhook_service.py:333-348); in the route the handlers catch only `WebhookPayloadError` and `sqlite3.Error` (routes/webhook.py:135-175). The `ValueError` escapes as an unhandled 500 **before** the delivery is marked terminal, so the delivery stays `claimed` (in-flight) and Razorpay's retry re-enters the exact same path → **permanent 500, and the failed payment never reaches the recovery pipeline**.

Reachability: `razorpay_webhook.py:367-371` sets `failed_at = None` when the signed payload's `created_at` is absent or not an `int` (the parser only *requires* a payment id + status `failed`, _validate_failed_shape, razorpay_webhook.py:300-321). A real provider payload missing/oddly-typed `created_at` hits this. Because it fails closed it won't fabricate an event, but it also never recovers and retries forever. (Note the placeholder is impossible *by construction* — the fallback string can never satisfy the model's validation, so this branch is dead-on-arrival.)

---

## 4. MEDIUM findings

### 4.1 Per-request full-table DELETE migration (runs `init_db` on every connection)

Every request opens a fresh connection through `get_db` which calls `init_db`:

```python
# routes/webhook.py:57-64
def get_db() -> Iterator[sqlite3.Connection]:
    conn = connect_database()
    init_db(conn)          # called per request across all routers
    ...
```

`init_db` unconditionally executes the Phase-23 recovery-dedupe migration on **every** invocation:

```python
# db.py:356-397
DELETE FROM webhook_recovery_outcomes
WHERE delivery_id NOT IN ( ... ROW_NUMBER() OVER (PARTITION BY payment_link_id ...) )
... CREATE UNIQUE INDEX IF NOT EXISTS ux_webhook_recovery_outcomes_link ...
```

A per-request `DELETE` on a growing table (plus `PRAGMA table_info` probes, db.py:400-407) is a scaling hazard and takes a write lock at the start of every webhook/read request, serializing concurrent traffic and increasing "database is locked" odds. The DELETE statement itself is guarded by `except sqlite3.OperationalError` (window-fn fallback) — fine — but the de-duplication scan should happen once at migration time, not per connection.

### 4.2 CORS documentation contradicts code

`docs/DEPLOYMENT.md:56-57` instructs: *"frontend must reach the backend: either serve both behind one origin with a reverse proxy, or set `VITE_API_BASE` to the backend's public base URL (with CORS enabled on the backend for that origin)."* Grep of the entire backend for `CORSMiddleware|allow_origins|cors` returns **zero matches** — there is no CORS configuration anywhere. A cross-origin frontend configured exactly as documented will be blocked by the browser. Either the doc's CORS claim must be removed, or the middleware added.

### 4.3 Unpinned dependencies / non-reproducible builds

```text
# requirements.txt (all 6 lines unpinned)
fastapi
uvicorn[standard]
pytest
httpx
python-dotenv
razorpay
```

CI runs `pip install -r backend/requirements.txt` (`.github/workflows/ci.yml`), so builds are non-reproducible and depend on whatever is latest on PyPI; a future breaking `razorpay`/`httpx`/`fastapi` release can silently change behavior (and `httpx2` is already flagged by Starlette's deprecation warning). Pin exact versions (or at least compatible ranges) with hashes or a lockfile.

---

## 5. LOW / hardening findings (verified)

### 5.1 LIKE wildcard injection in dashboard search — `db.py:905-911`

```python
if query:
    like = f"%{query}%"          # user `%` and `_` are NOT escaped
    sql += " AND (event_id LIKE ? OR customer_id LIKE ? OR order_id LIKE ? OR payment_id LIKE ?)"
    params.extend([like, like, like, like])
```

Parameterized (no SQL injection) but a client-supplied `%`/`_` turns the search into a wildcard: `query=%` returns every row up to `LIMIT`, and a crafted pattern can probe for id structures. Escape `%`/`_` in `query` or search literal substrings only.

### 5.2 `get_policy_history` full table scan per decision — `db.py:844-850`

```python
rows = conn.execute(
    """SELECT event_id, intervention, customer_id, cost_paise,
              attempted_at, status
       FROM intervention_attempts
    """                        # <- no WHERE at all
).fetchall()
```

Every policy evaluation loads **all** `intervention_attempts` rows into Python and filters the 24h window + per-event cooldown in application code. O(n) per decision as the table grows. Add a `WHERE attempted_at >= ?` window bound and per-event index.

### 5.3 Broad `except Exception` echoes internal error text — `routes/events.py:204-213`

```python
except Exception as exc:
    ...
    "detail": f"unexpected classification failure: {exc}",
```

Raw exception text (which may include provider URLs, internal identifiers, or stack-junk from the classification adapter) is returned verbatim to the client. Sub-agent claimed this leaks the API key — that specific claim is **unproven** (httpx/OmniRoute error strings sampled do not include the bearer header), but echoing `str(exc)` is an information-exposure smell. Return a stable error title; log the detail server-side.

### 5.4 `OMNIROUTE_BASE_URL` SSRF surface — config-only — `config.py:62-64`, `classifier.py:144,170-172`

`OMNIROUTE_BASE_URL` is read unchecked from the environment and concatenated into `f"{self._base_url}/chat/completions"` (classifier.py:170-172). It is operator-controlled, not attacker-controlled through the API, so the agent's MEDIUM rating is overstated; realistically LOW. Still worth a note: there is no scheme/allowed-host validation, and this URL is the one place the outbound HTTP sink can point somewhere unexpected. Validate `https://` + allow-list.

### 5.5 No busy_timeout / WAL; multi-commit execution — `db.py:263`

```python
conn = sqlite3.connect(path, check_same_thread=False)   # default busy timeout 5s, no WAL
```

`execute_event` performs 5+ separate `commit()` calls (`_persist_decision`, claim, outcome, attempt, claim-resolution) with no outer transaction (execution_service.py:392-479). Crash between them is handled by the claim-parking machinery (park-as-unknown), so no double execution; but concurrent `execute` + webhook on one SQLite file will occasionally surface `database is locked` (5s timeout) under real load. DEPLOYMENT.md already honestly disclaims this is a demo/single-writer database.

### 5.6 Claim-loser cosmetic mislabel — `execution_service.py:241-247`

If the concurrent winner completes and *releases* (known failure) before the loser reads the claim, `get_execution_claim` returns `None` → loser reports `ALREADY_EXECUTED` even though nothing executed. Status-accuracy only; no safety impact.

### 5.7 Webhook `in_flight` claim double-processing (rare) — race within `claim_webhook_delivery`

Two simultaneous deliveries of the *same* `delivery_id` can both pass `claim_webhook_delivery` (one fresh INSERT, one treating the just-written `claimed` row as `in_flight`) and both proceed to `ingest_event`. The check-then-insert in `ingest_event` (ingestion.py:57-64) is non-atomic, so on a true race the loser can hit the event-id `IntegrityError` → `IngestionStatus.ERROR` → surfaced as a 500 `persistence_failure` even though the event was actually persisted by the winner (ingestion.py:65-70; webhook_service.py:350-363). Self-heals on Razorpay retry (delivery then terminal), and the adversarial test suite already accepts persisted-outcome statuses here, so severity is LOW/informational.

### 5.8 Deps/CI

- `.env.example` is absent (`cat` returned nothing) — no committed reference template for `VITE_API_BASE`, `OMNIROUTE_BASE_URL`, etc.
- `frontend/src/core/api.js:4` defaults `API_BASE` to `/api` (Vite proxy assumption) with no cross-origin credentials — combined with §4.2, the only supported production topology today is same-origin proxying.

---

## 6. Areas audited CLEAN (affirmed, not merely asserted)

- **Policy completeness**: all non-`NO_ACTION` candidates from the advisory classification are evaluated; both selectors (V1 fixed-priority, V2 optimizer) only ever select `allowed==True` candidates; the executor independently rejects forged/denied/stale decisions (executor.py:262-284). Denied decisions cannot reach the executor.
- **Cooldown & customer-limit bypass**: execution uses server `now` via `Depends(get_now)` (events.py:99-105, 388; recovery.py:161); no client-supplied `evaluation_time` reaches an execution path. The advisory `/events/{id}/policy` endpoint accepts a client `evaluation_time` (events.py:310-323) but it never feeds execution — advisory-only, LOW note.
- **Optimizer math**: integer-only paise/bps, no division-by-zero, no float, no NaN, no overflow (economics.py:229-317); ties deterministic via `(-EV, priority, name)` (optimizer.py:275-285).
- **Mode enforcement**: mode is structurally coupled to intervention (executor.py:169-179, 107-115); REAL runs only with a genuine Test-Mode client that rejects `rzp_live_` keys (razorpay_client.py:178-187); `None` client → explicit `configuration_missing` failure, never a fabricated call; `provider_result_unknown` claims are held, never released.
- **Webhook signature gate**: HMAC-SHA256 over the exact raw body, constant-time compare, fail-closed on missing secret/signature, signature verified *before* any parsing (razorpay_webhook.py:64-94; routes/webhook.py:113-131).
- **Recovery uniqueness**: DB UNIQUE index on `payment_link_id` + `INSERT OR IGNORE` — double-counting a paid link is structurally impossible (db.py:343-397).
- **Estimate fallback**: corrupt/NaN/out-of-range calibration snapshot falls back to baseline via `except Exception`, never affects authorization (calibration_service.py:395-409; adaptive_estimation.py:89-97).
- **Frontend output safety**: **no** `dangerouslySetInnerHTML`, no `innerHTML`, no `eval`/`document.write` anywhere (grep across `frontend/src`); all model/classifier/UI text rendered through React's auto-escaping JSX. All `href` values are static `#anchor` links (LandingPage.jsx:136-168). **The two sub-agent XSS findings are refuted.**
- Full backend suite: **1670 passed**; frontend `oxlint` + `vite build` clean.

---

## 7. Refuted sub-agent findings

| Claimed | Source | Refutation |
|---------|--------|------------|
| HIGH XSS: classifier `reasoning` unescaped at EventTrace.jsx:401, RecoveryOps.jsx:199 | frontend agent | React auto-escapes `{...}` text; repo has zero `dangerouslySetInnerHTML`/`innerHTML` sinks (grep: no files found). Not exploitable. |
| Live/Test-mode key leakage risk | security agent (implied) | `RazorpayPaymentLinkClient.__init__` rejects any key id not starting `rzp_test_` (razorpay_client.py:178-187); live keys can never construct the client. |
| "API key leaks into 500 detail strings" | AI/calibration agent | Unproven: sampled OmniRoute/httpx error strings carry no bearer header; the actual issue is generic `str(exc)` echo (§5.3), downgraded. |
| `OMNIROUTE_BASE_URL` SSRF = MEDIUM | AI/calibration agent | Environment-controlled, not request-controlled → downgraded to LOW (§5.4). |

---

## 8. Honest bottom line

- **Blocking before any operator deployment**: the total absence of inbound authentication (§2.1). Nothing else stops deployment of the *demo* — the rest are integrity hardening.
- **Correctness bug worth fixing soonest**: the `payment.failed` midnight-timestamp fallback (§3.2) that turns a verified failure without `created_at` into an unrecoverable 500 loop.
- **Inert control**: spend-cap rule (§3.1) needs economic costs wired into `PolicyConfig`, or its documentation corrected.
- The architecture's core invariants (advisory AI, deterministic gate, authorized-only execution, test-mode-only REAL, verified-only recovery) are real and hold under audit.

---

## Appendix — Files cited

main.py, config.py:62-64,145-149 · policy.py:124-128,476-486 · execution_service.py:230-253,379-492 · db.py:254-266,343-397,400-407,844-918 · routes/webhook.py:57-64,113-175 · routes/events.py:99-105,167-249,275-323,383-390 · routes/recovery.py:161 · routes/estimation.py:63-136 · webhook_service.py:96-139,203-274,277-393 · failed_payment_ingestion.py:77-115 · models.py:83-122 · razorpay_webhook.py:202-246,249-396 · razorpay_client.py · ingestion.py:40-77 · classifier.py:110-189 · calibration_service.py:225-274,395-409 · adaptive_estimation.py:89-97 · optimizer.py:275-285 · selector.py · economics.py · executor.py:107-179,262-284 · benchmark_simulation.py · frontend/src/core/api.js · frontend/src/components/{EventTrace,RecoveryOps,LandingPage,PolicyBlocks,CommandCenter,RevenueHealth,PolicyLab}.jsx · frontend/vite.config.js · docs/DEPLOYMENT.md:46-79 · .github/workflows/ci.yml · backend/requirements.txt
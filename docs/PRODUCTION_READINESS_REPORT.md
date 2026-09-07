# RecoveryOS — Production-Boundary Closure Report & Final Verdict

Date: 2026-09-07
Scope: closes every verified finding in `docs/DEEP_AUDIT_REPORT.md` (2026-09-07) with a fix plus a regression test, then independently re-verifies the entire surface before issuing a verdict.

Verification evidence for this closure:
- `backend: 1697 passed` (`python -m pytest`, from `backend/`) — the 1670-test baseline plus 27 new regression tests.
- `frontend: oxlint clean, vite build clean`.
- Reported per-finding, below.

---

## 1. Verdict

**READY** for operator deployment *when configured* — meaning `RECOVERYOS_OPERATOR_API_KEY` is set at deploy time (the fail-closed contract mandates it; without it every operator endpoint returns 503 and refuses to serve, by design).

Residual limitations are documented and non-blocking:
- SQLite is a single-writer database; `DEPLOYMENT.md` already disclaims it is a demo/single-writer store.
- The default daily spend-cap of ₹50,000 against ~₹0.20–₹1.00 modelled intervention costs is practically unreachable; the cap is now *genuinely wired* to real economic costs, so raising/lowering it actually binds.

No feature work was added; no safety invariant was relaxed. The changes are fixes and regression tests.

---

## 2. Finding-by-finding closure

| # | Audit finding | Fix | Regression coverage |
|---|---------------|-----|---------------------|
| 1 | CRITICAL — no authentication on any operator/data endpoint | New `app/auth.py` `require_operator` dependency: `Authorization: Bearer <key>`, constant-time `hmac.compare_digest`, key read at request time (`app/config.py:get_operator_api_key`), fail-closed (unset → **503**, missing/invalid → **401** with `WWW-Authenticate`). Applied centrally in `app/main.py` via `app.include_router(..., dependencies=[Depends(require_operator)])` to all eight routers. Public boundaries preserved: `GET /health`, `GET /health/ready`, and the signature-verified `POST /webhook/razorpay`. | `tests/test_operator_auth.py` (9 tests): missing/wrong/non-bearer/malformed credentials → 401; valid → 200; rotation takes effect immediately; unconfigured → 503; health public; webhook passes the operator gate to its own signature gate. |
| 2 | HIGH — spend-cap rule structurally disabled (all costs 0) | `app/config.py` now resolves intervention costs once from `DEFAULT_ECONOMIC_MODEL` (`default_intervention_cost_paise()`) and wires them into `build_policy_config()` → the runtime policy and every persisted attempt carry the real modelled cost, so `RULE_SPEND_CAP` genuinely evaluates non-zero inputs. | `tests/test_phase26_hardening.py::test_spend_cap_uses_the_economic_model_costs`; existing `test_policy_api.py::test_policy_spend_cap_uses_configured_value` reaffirmed. |
| 2a | (emergent) replay scenario costs could drift from the runtime policy | `app/policy_scenario.py::_scenario_config` now uses the same `default_intervention_cost_paise()` wiring, so a custom scenario that echoes the active policy's three knobs replays identically (the "identical policy under a different name" invariant held by `test_replay.py` again). | `tests/test_phase26_hardening.py::test_replay_scenario_costs_cannot_drift_from_the_economic_model`; `tests/test_replay.py` (full file re-passed). |
| 3 | HIGH — `payment.failed` without `created_at` → permanent 500 retry loop | `map_failed_payment_to_event` takes `observed_at` (the webhook boundary's real observation time), uses `failed_at or observed_at`, and raises an explicit `ValueError` when neither exists — the fabricated `1970-01-01T00:00:00+00:00` fallback is gone. Route passes `received_at` (`app/webhook_service.py`). | `tests/test_phase26_hardening.py`: mapping without `created_at` uses the observation time and never `1970`; mapping with neither timestamp fails explicitly. |
| 4 | MEDIUM — per-request full-table DELETE migration | `init_db` is now non-destructive (index/column-only; idempotent). The dedupe `DELETE` moved into `run_migrations(conn)` (`app/db.py`), called once from the FastAPI lifespan (`app/main.py`) and from the seeding entrypoints (`populate.py`, `benchmark_store.py`). `_ensure_webhook_recovery_outcome_uniqueness` tolerates a legacy duplicate table via a savepoint (skips the index build, leaving the collapse+index to deploy-time migration). | `tests/test_phase26_hardening.py`: `init_db` leaves historical duplicates untouched; `run_migrations` collapses them (newest kept) and installs the unique index. Existing migration test (`test_phase25_hardening.py::test_uniqueness_migration_collapses_historical_duplicates`) still green. |
| 5 | MEDIUM — CORS doc contradicts code | `CORSMiddleware` added fail-closed (`get_cors_origins()`: empty default = same-origin proxying only; comma-separated `RECOVERYOS_CORS_ORIGINS` opt-in). `docs/DEPLOYMENT.md` cross-origin section rewritten to match. `backend/.env.example` documents both new env vars. | `tests/test_phase26_hardening.py`: allow-list empty by default; comma-separated parsing. |
| 6 | MEDIUM — unpinned dependencies | `backend/requirements.txt` pinned exactly to the tested versions (fastapi 0.136.1, uvicorn[standard] 0.43.0, pytest 9.1.1, httpx 0.27.0, python-dotenv 1.0.1, razorpay 2.0.1). | `tests/test_phase26_hardening.py::test_requirements_are_pinned_exactly`. |
| 7 | LOW-MED — LIKE wildcard injection in dashboard search | `list_payment_events` escapes `%`, `_`, `\` (`_escape_like_literal`) and adds `ESCAPE '\'` to the query, so search is literal substring only. | `tests/test_phase26_hardening.py::test_like_search_is_literal_not_wildcard`. |
| 8 | LOW-MED — `get_policy_history` full-table scan | SQL window bound (`attempted_at >= ?`) + per-event `event_id = ?` filter backed by the new `idx_intervention_attempts_attempted_at` index; the Python datetime parse/cooldown filter is retained as a correctness back-stop. | `tests/test_policy_persistence.py` (full history suite re-passed): window, exact-24h boundary, per-customer, per-event, spend accumulation. |
| 9 | LOW-MED — broad `except Exception` echoes internal text | `app/routes/events.py` now logs `str(exc)` server-side (`exc_info=True`) and returns a stable `classification_error` detail; no exception text is echoed to the client. | `tests/test_phase26_hardening.py::test_classify_failure_detail_is_stable_and_never_echoes_the_exception`. |
| 10 | LOW — `OMNIROUTE_BASE_URL` SSRF surface | `classifier.py` `_validate_base_url`: HTTPS required (http tolerated only for `localhost`/`127.0.0.1`/`::1`), host required, trailing slash normalized; validated at the adapter boundary (`OmniRouteClassifier.__init__`, `build_omniroute_adapter`). | `tests/test_phase26_hardening.py` (4 tests): scheme/host/loopback/normalization + adapter construction. |
| 11 | LOW — no busy_timeout/WAL | `db.connect()` sets `PRAGMA busy_timeout = 10000` and `PRAGMA journal_mode = WAL` (WAL tolerated if unsupported). | Existing DB tests re-passed; connection PRAGMAs validated indirectly via full suite. |
| 12 | INFO — claim-loser mislabeled `ALREADY_EXECUTED` | New `STATUS_EXECUTION_CLAIM_RELEASED` returned when the claim row is absent; `app/routes/recovery.py` maps it to a 200 block not in the conflict set, with an accurate "was not executed and may be retried" detail. | `tests/test_phase26_hardening.py`: no claim → `execution_claim_released`; held claim → `execution_in_progress`. |
| 13 | INFO — webhook concurrent double-processing (rare) | `ingest_event` maps a concurrent-insert `sqlite3.IntegrityError` to a benign `DUPLICATE` after confirming the row (was previously an `ERROR` → 500). | `tests/test_phase26_hardening.py::test_concurrent_duplicate_insert_reports_duplicate`. |
| 14 | REFUTED × 2 — frontend XSS, live-key leak | No change needed; refutations in the source audit stand. | — |

---

## 3. What was left alone (deliberately)

- **Advisory-only `evaluation_time`** on `/events/{id}/policy` — never reaches an execution path; unchanged.
- **Frontend** — no changes required: same-origin `/api` proxy, no dangerous sinks, build clean.
- **The core invariants** the audit affirmed as CLEAN were re-verified green by the existing suite (policy completeness, authorized-only execution, Mode enforcement, webhook signature gate, recovery uniqueness, estimator fallback).

---

## 4. Verification

Backend:
```
1670 (pre-change baseline) + 9 (test_operator_auth.py) + 18 (test_phase26_hardening.py)
= 1697 passed
```
Public-boundary spot checks re-run live against the app object: `/health`, `/health/ready`, and an unsigned webhook deliver `200/200/400 (missing_signature)` — none blocked by the operator gate; every operator route 401s without a bearer and 503s on an unconfigured key.

Frontend: `npm run lint` (oxlint) clean; `vite build` clean.

---

## 5. Operational checklist (deployment)

1. Set `RECOVERYOS_OPERATOR_API_KEY` (long random value) — required; fail-closed.
2. Set `DATABASE_URL`, and the Razorpay/OmniRoute secrets as before.
3. Leave `RECOVERYOS_CORS_ORIGINS` unset unless the frontend is served cross-origin.
4. All client calls to operator endpoints must send `Authorization: Bearer <key>`.
5. `uvicorn app.main:app` — startup lifespan now applies migrations once.

---

## Appendix — Files changed

Added: `backend/app/auth.py`, `backend/tests/test_operator_auth.py`, `backend/tests/test_phase26_hardening.py`.

Modified: `backend/app/{main,config,db,classifier,ingestion,webhook_service,failed_payment_ingestion,execution_service,policy_scenario,populate,benchmark_store}.py` · `backend/app/routes/{events,recovery}.py` · `backend/requirements.txt` · `backend/.env.example` · `backend/tests/conftest.py` and 21 existing API test files (test clients now authenticate via `TEST_OPERATOR_HEADERS`) · `docs/DEPLOYMENT.md`.
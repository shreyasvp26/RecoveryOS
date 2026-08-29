# RecoveryOS V1 Release Baseline

This document records the frozen V1 submission baseline. It is the reference point for any future V2 work: it states what V1 actually does, what was verified and how, and where the honest limits are. Nothing here is aspirational.

**Release tag:** `v1-submission` (annotated). The tag points at the V1 release commit on `main`; resolve it with `git rev-parse v1-submission`.

**Branch:** `main`

## Verification summary

All results below were produced by running the repository, not by inference.

| Area | Result |
| --- | --- |
| Test suite (`python -m pytest` from `backend/`) | 492 passed, 0 failed, 0 skipped, 0 xfail, 1 warning (third-party Starlette deprecation) |
| Backend startup | All 10 routes registered and responding; controlled errors verified (404 unknown event, 422 invalid body, 400 unsigned webhook) |
| Frontend | `npm run build` and `npm run lint` both clean |
| Clean-state reproducibility | Full chain verified from an empty database: init (8 tables, all zero) → populate → benchmark → persist → dashboard read. No manual rows, no stale state, no credentials required |
| Benchmark reproducibility | Two identical invocations produced byte-identical output; a different seed produced different output |
| Unauthorized execution | 0 across 8 adversarial vectors |
| Hidden ground-truth isolation | Verified by import tracing plus source-level, behavioural, and API-level tests |

## Architecture

```
Event → Context → AI reasoning (advisory) → Deterministic policy gate → Intervention selection
      → Execution → Outcome → Audit → Benchmark → Dashboard
```

The governing principle: **the AI recommends, the policy engine decides, the executor acts, the benchmark proves value.** The LLM never directly controls a money-moving action. Classification output is advisory input to a deterministic gate; it cannot authorize anything.

Locked stack: Python 3.11, FastAPI, SQLite, React + Vite, OmniRoute (LLM), Razorpay Python SDK (Test Mode), pytest, `.env` configuration.

## V1 fixed-priority selector

Selection is **fixed-priority, not economic**. `app/selector.py` intersects the classifier's candidates with the set carrying an authoritative ALLOW decision, then picks the highest-priority survivor:

```
retry_delayed > payment_link > reminder > alternate_method_prompt > retry_immediate
```

`no_action` is never selected as an action and is never executed. There is no expected-value calculation, no cost model, no ranking by predicted recovery, and no randomness.

## Safety guarantees

Six locked rules in `app/policy.py`, evaluated in this authoritative order (`DETERMINISTIC_RULE_ORDER`), where the **first blocker determines the denial reason**:

1. Fraud protection — `fraud_suspect` events are always denied
2. Terminal failure block
3. Duplicate successful-intervention protection
4. Maximum 2 interventions per customer per rolling 24h
5. 30-minute event cooldown
6. Configurable daily spend cap (rolling 24h, global)

Verified at exact boundaries: the cooldown blocks at 29.99 minutes and allows at 30.0; the customer cap allows the 1st and 2nd intervention and blocks the 3rd; the spend cap allows spend exactly at the cap and blocks cap + 1; fraud takes precedence over every other simultaneously-tripped rule. 200 identical evaluations produced exactly 1 distinct output.

The gate is **fail-closed**: timezone-naive timestamps, unknown interventions, malformed input, and unexplained denials all raise explicit controlled errors. `PolicyDecision` rejects a non-boolean `allowed` and rejects a denial with no reason. Policy imports only stdlib, the intervention taxonomy, and the domain models — it contains no LLM call, no HTTP client, and no execution.

**Unauthorized execution = 0.** `BoundedExecutor` independently re-verifies authorization and blocked all of: a policy-denied intervention, an ALLOW bound to a different event, an ALLOW for a different intervention, `no_action`, a forged non-`PolicyDecision` object, and a denied simulated intervention. The check is an `is not True` identity comparison, so truthy non-`True` values cannot pass.

## Razorpay integration behaviour

**Test Mode only, enforced structurally.** `app/razorpay_client.py` rejects `rzp_live_` keys, empty credentials, and any key without the `rzp_test_` prefix — all before the SDK is constructed. The executor therefore cannot reach production Razorpay even if live credentials are supplied by mistake.

Every failure path yields an explicit `FAILED` outcome and never a fabricated success:

| Condition | Outcome |
| --- | --- |
| No client configured | `REAL_RAZORPAY` / `FAILED` / `configuration_missing` |
| Controlled `RazorpayError` | `REAL_RAZORPAY` / `FAILED` / provider detail |
| Unexpected exception | `REAL_RAZORPAY` / `FAILED` / `razorpay_api_error` |

The closed loop was verified end to end once against real Razorpay Test Mode infrastructure during Phase 14: a real classification, a deterministic ALLOW and selection, a real Test Mode Payment Link, a real manual browser payment, and a genuine `payment_link.paid` webhook that was HMAC-verified over the exact raw body, correlated to the persisted `payment_link_id`, and recorded as a single trusted recovery. See the README for the evidence.

## Real vs simulated distinction

Every execution records an explicit `execution_mode`. `payment_link` runs as `REAL_RAZORPAY`; `retry_immediate`, `retry_delayed`, `reminder`, and `alternate_method_prompt` run as `SIMULATED`. All benchmark recovery figures are simulated and carry `evaluation_mode = "SIMULATED"`.

The UI labels simulated figures at every render site ("SIMULATED BENCHMARK — evaluation, not production revenue") and never displays a per-event recovered amount. The only real recovered rupee figure in the interface is the closed-loop panel, which is conditioned on a verified webhook and shows "awaiting verified `payment_link.paid` webhook" until one arrives. No frontend component contains a hardcoded financial or benchmark number; all figures come from `/dashboard/summary`, `/events`, `/events/{id}/trace`, and `/decisions/blocked`.

**Three states that stay distinct.** Execution success means the operation ran. Payment success means a payer completed checkout. Verified recovery means RecoveryOS independently authenticated, correlated, and persisted the outcome. Only the third marks money recovered, and execution success is never treated as evidence of recovery.

## Reproducibility procedure

```bash
cd backend
python3.11 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env                                  # .env is gitignored; never commit it

python -m pytest                                      # 492 tests
python -m app.populate --seed 42 --count 60           # deterministic demo dataset
python -m app.benchmark_store --seed 42 --count 500   # canonical benchmark, persisted
uvicorn app.main:app                                  # API on :8000

cd ../frontend && npm install && npm run dev          # proxies /api -> :8000
```

To reset, stop the API, delete the SQLite file, and re-run the same two data commands. `app.populate` is deterministic (fixed reference evaluation time) and idempotent (re-running skips already-processed events). No credentials are needed for the offline demo; the live Razorpay loop additionally requires Test Mode credentials, a webhook secret, a public tunnel registered in the Razorpay Dashboard, and a manual browser payment.

## Benchmark result and limitations

Canonical run: seed 42, 500 synthetic events, one shared event set and one shared hidden outcome model across all three strategies.

| Strategy | Recovered (simulated paise) | Recovered events | Interventions |
| --- | --- | --- | --- |
| No Action | 266,939,600 | 242 / 500 | 0 |
| Naive Retry | 271,854,300 | 246 / 500 | 240 |
| RecoveryOS | 264,715,100 | 241 / 500 | 159 |

RecoveryOS recovers **less** simulated revenue than both baselines on this seed (−2,224,500 vs No Action; −7,139,200 vs Naive Retry). That result is reported, not suppressed, and the seed was not tuned.

**The hidden outcome model carries no signal.** `generate_hidden_outcome_model` draws independent uniform probabilities via `rng.random()` per (event, intervention) pair. They are uncorrelated with every event feature — failure reason, payment method, amount, risk flag, customer history — and uncorrelated across interventions. Expected recovery is therefore ≈0.5 for every pair, which is exactly what the flat 242/246/241 spread shows.

Consequently **the benchmark cannot reward intelligent targeting**. The only lever that moves simulated recovered revenue is how many events a strategy acts on, so Naive Retry's small edge comes from attempting 240 interventions rather than choosing better ones, and RecoveryOS sits slightly lower because policy correctly refuses fraud and terminal events. What the benchmark *does* establish is harness integrity: order-invariant determinism, a shared event set and shared model, ground-truth isolation, zero exceptions, a fraud intervention rate of 0.0 for RecoveryOS, and the accounting invariant `processed + skipped + exceptions == event_count`. What it *cannot* establish is that RecoveryOS's targeting beats a blanket retry.

Metric H (false-intervention rate) is deliberately **not computed**: the repository defines no canonical threshold, so any value would be invented rather than measured.

Benchmark figures are simulated evaluation results over seeded synthetic data. **They are not real, production, or customer revenue**, and they are unrelated to the live Test Mode verification. The two must never be combined.

## Known limitations

- **Test Mode is not production payment processing.** Different method availability (international cards unsupported), no settlement, no real fraud or risk decisioning, no production rate limits or failure modes. No production readiness is claimed.
- **LLM classification is not deterministic.** Two semantically similar events at `temperature = 0.0` produced different candidate sets. The correct claim is that *the intervention-selection policy is deterministic once the classifier output is available, while the external LLM classification itself can vary between equivalent events.* RecoveryOS never coerces the model toward a particular intervention, so a live demo may need more than one event before `payment_link` is selected.
- **The live loop cannot run unattended.** The hosted payment requires a human in a browser, quick-tunnel hostnames are ephemeral, and Razorpay does not re-target queued webhook retries to a newly configured URL.
- **Recovery verification depends on an external webhook.** The system is fail-closed and will not mark anything recovered on its own; if the webhook does not arrive the trace honestly stays at `waiting`.
- **The live closed loop is a single verified instance**, not a reliability or load measurement.
- **Default intervention costs are all zero.** `PolicyConfig.intervention_cost_paise` defaults to 0 for every intervention, so the spend cap only triggers on pre-existing recorded spend unless costs are configured. The rule itself is correct and enforces properly once costs are set; V1 simply has no per-intervention cost model.
- **Per-event recovery is not recorded.** Only execution outcomes and webhook-verified recoveries are persisted, so the dashboard substantiates non-recovery as `policy_blocked`, `no classification`, or `no execution recorded` rather than from a hidden outcome.
- **Recoverable Revenue has no canonical definition** in the repository and is displayed as unavailable rather than guessed.

## V1/V2 boundary

V1 is frozen at `v1-submission`. The following are **not implemented** and were verified absent from `backend/app/`: economic intervention optimizer, expected-value selection, cost or friction modelling, policy replay, incident or degradation intelligence, model-adapter redesign, multi-agent architecture, LangGraph, Kafka, Redis, and Kubernetes. Dependencies remain `fastapi`, `uvicorn[standard]`, `pytest`, `httpx`, `python-dotenv`, `razorpay`.

The planned V2 direction is expected-value intervention selection along the lines of

```
EV = P(recovery | event, intervention) × amount − intervention_cost − friction_cost
```

which is **deliberately not implemented in V1**. Two constraints any V2 work must respect:

1. **Hidden benchmark probabilities must never reach the system under test.** They are evaluation ground truth. A V2 optimizer needs its own estimator learned from decision-time information, not the hidden model.
2. **A signal-bearing outcome model is a prerequisite for measuring V2.** Under the current no-signal model, no targeting strategy can outperform a blanket one, so the existing benchmark cannot demonstrate an optimizer's value either way.

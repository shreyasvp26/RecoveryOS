# RecoveryOS

An **AI Revenue Recovery Control Plane** for the Razorpay AI Buildathon 2026 (Revenue Recovery track).

## Core Principle

> **AI recommends. Deterministic policy decides. Executor acts. Benchmark proves value.**

The LLM never has direct authority over a money-moving action. AI output is advisory; a deterministic policy gate is authoritative; an executor performs the action; a benchmark proves value against baselines.

> **Important:** This repository is at **Phase 14 — V1 verification and submission readiness**. RecoveryOS performs **no production revenue recovery**. It can select one intervention deterministically and run it either as an explicit simulation or as a real **Razorpay Test Mode** Payment Link; the closed loop has been demonstrated end to end against real Razorpay Test Mode infrastructure (see [Live Razorpay verification (Trace C)](#live-razorpay-verification-trace-c)). **Test Mode is not production payment processing**, and no claim of production readiness is made. The benchmark proves value only by comparison — over ONE shared 500-event **synthetic** set and ONE shared hidden outcome model, it measures **No Action**, **Naive Retry**, and the real **RecoveryOS** pipeline (classifier → policy → selector → executor) on simulated, labeled recovery amounts; read the [benchmark honesty disclosure](#benchmark-honesty-and-the-no-signal-limitation) before quoting any figure. A **read-only operator dashboard** (Recovery Command Center, Event Decision Trace, Policy & Blocked Actions) reports persisted state with honest labeling of simulated figures, and the verified, outcome-only `payment_link.paid` webhook marks each real link `waiting` → `recovered`. The V2 optimizer remains future work.

## Locked Architecture

```
Razorpay Test Mode
  → Event Ingestion
  → Event Context + Customer History
  → AI Reasoning
  → Deterministic Policy Gate
  → Intervention Selection
  → Real Razorpay Test Action OR Controlled Simulation
  → Evaluation Boundary (Hidden Outcome Model + Deterministic Simulation)
  → Outcome Engine
  → Append-only Audit Trail
  → Benchmark
  → Dashboard
```

See `docs/ARCHITECTURE.md` for the detailed design, including the distinction between `REAL_RAZORPAY` and `SIMULATED`, and the rule that benchmark recovery amounts are simulated evaluation results (not production Razorpay revenue). The Phase 8 evaluation boundary is described there too: deterministic, order-independent, hidden from the decision path.

## Repository Structure

```
recoveryos/
├── backend/       FastAPI application + pytest test suite
│   ├── app/       Application package (models, persistence, generator, ingestion, classifier, policy, selector, executor, razorpay client)
│   ├── tests/     pytest tests
│   ├── requirements.txt
│   └── .env.example
├── frontend/      React + Vite application
├── docs/          ARCHITECTURE.md, BENCHMARK.md, DESIGN.md, ECONOMIC_MODEL.md, POLICY_REPLAY.md, RECOVERY_OPERATIONS.md, REVENUE_HEALTH.md, PITCH_NOTES.md, V1_BASELINE.md
├── README.md
└── .gitignore
```

## Local Setup

Prerequisites: Python 3.11, Node.js, npm.

### Backend

```bash
cd backend
python3.11 -m venv .venv
source .venv/bin/activate       # Windows: .venv\Scripts\activate
pip install -r requirements.txt

# Copy the environment template (edit as needed, never commit .env)
cp .env.example .env
```

### Backend Tests

```bash
cd backend
python -m pytest            # or: python -m pytest tests/ -v
```

### Frontend

```bash
cd frontend
npm install
npm run dev                 # Vite dev server
```

Open the printed local URL to view the RecoveryOS shell page.

## Run the Backend

```bash
cd backend
uvicorn app.main:app --reload
```

Health check: `http://127.0.0.1:8000/health` returns `{"status": "ok"}`.

## Environment & Configuration

All configuration is read from the environment. `backend/.env` is the local mechanism; copy `backend/.env.example` (which documents every variable and its default) and fill in real values.

```bash
cd backend
cp .env.example .env        # then edit; .env is gitignored and MUST NOT be committed
```

**Secrets must never be committed.** `.env` and `.env.*` (except `.env.example`) are gitignored, as are `*.db` SQLite files. No credential is ever hardcoded in source, written to SQLite, or echoed in API responses. If a secret is ever committed, treat it as compromised and rotate it in the provider dashboard.

| Variable | Purpose | Behaviour when unset |
| --- | --- | --- |
| `OMNIROUTE_API_KEY` | Auth for the advisory AI classifier's model gateway | Classification fails explicitly; no fabricated classification |
| `OMNIROUTE_MODEL` | Model identifier, externalized so no model name is hardcoded in business logic | Falls back to the `config.py` default |
| `OMNIROUTE_BASE_URL` | OpenAI-compatible endpoint base URL | Falls back to the `config.py` default |
| `RAZORPAY_KEY_ID` | Razorpay **Test Mode** key ID (`rzp_test_` prefix required) | `payment_link` execution reports an explicit `configuration_missing` failure |
| `RAZORPAY_KEY_SECRET` | Razorpay Test Mode key secret | Same as above |
| `RAZORPAY_WEBHOOK_SECRET` | HMAC-SHA256 secret for verifying webhook bodies — a **separate** secret from the API key secret, set in the Razorpay Dashboard webhook settings | Incoming webhooks fail verification (**fail-closed**) |
| `POLICY_MAX_INTERVENTIONS_PER_CUSTOMER_24H` | Rolling-24h per-customer intervention cap | Defaults to 2 |
| `POLICY_EVENT_COOLDOWN_MINUTES` | Minimum gap between interventions on one event | Defaults to 30 |
| `POLICY_DAILY_SPEND_CAP_PAISE` | Rolling-24h global spend cap | Defaults to 5000000 |
| `DATABASE_URL` | SQLite path | Defaults to `sqlite:///./recoveryos.db` |

**Razorpay Test Mode is enforced at the client boundary.** `razorpay_client.py` rejects any `rzp_live_` key, so the executor structurally cannot reach production Razorpay even if live credentials are supplied by mistake.

For webhook delivery during local development, the backend must be reachable from Razorpay's servers. The verification below used a Cloudflare quick tunnel to expose `http://127.0.0.1:8000`, with the tunnel hostname registered as `https://<tunnel-host>/webhook/razorpay` in the Razorpay Dashboard webhook settings.

## Running the demo

The offline demo needs no Razorpay credentials and is fully deterministic:

```bash
cd backend
python -m app.populate --seed 42 --count 500         # deterministic demo dataset (SIMULATED execution)
python -m app.benchmark_store --seed 42 --count 500  # persist the canonical benchmark run summary
uvicorn app.main:app                                 # start the API on :8000

cd ../frontend
npm run dev                                          # Vite dev server (proxies /api -> :8000)
```

To reset, stop the API, delete the SQLite file, and re-run the same two commands — `app.populate` is deterministic and idempotent, so the persisted chain reproduces exactly.

**Why `--count 500`.** The Phase 20 Revenue Health screen compares the last 28 days of the dataset against the preceding 28 days per segment, and only compares a segment with at least five evaluated outcomes in each window. A smaller workload is legitimate but will mostly fail that sample gate, and the screen will honestly report that no degradation was detected rather than lowering the bar.

The **live** Razorpay loop additionally requires Test Mode credentials, a webhook secret, a public tunnel whose hostname is registered in the Razorpay Dashboard, and a manual browser payment. See the limitations below before attempting it.

## Live Razorpay verification (Trace C)

The full closed loop has been demonstrated once, end to end, against **real Razorpay Test Mode** infrastructure with no fabricated events, classifications, payments, or webhooks, and no manual database edits. Each stage below is a distinct architectural boundary, and the evidence for each was captured separately.

| Stage | What actually happened |
| --- | --- |
| **AI diagnosis** (advisory) | A genuinely new failed-payment event was ingested and classified by the real OmniRoute-backed classifier. The classification is advisory: it proposes candidate interventions and cannot authorize anything. |
| **Deterministic policy** | The policy gate independently evaluated the candidates against the six locked rules and returned an authoritative `ALLOW`. |
| **Deterministic selection** | The selector intersected the allowed candidates with the locked V1 priority and selected `payment_link`. The model was never asked or steered to pick it. |
| **Real Test Mode execution** | The bounded executor created a real Razorpay **Test Mode** Payment Link (`REAL_RAZORPAY`, execution status `SUCCESS`). |
| **Real payment** | The hosted Razorpay checkout page was paid manually in a browser by netbanking for ₹4,999 (499900 paise). Razorpay's API independently reports the link `paid` with `amount_paid` 499900 and the payment `captured`. |
| **Real webhook** | Razorpay delivered a genuine `payment_link.paid` webhook to the tunnel from its own infrastructure. The endpoint was never called by hand and no delivery was synthesized. |
| **HMAC verification** | The signature was recomputed over the **exact raw request body** and compared constant-time *before* any parsing. |
| **Correlation** | The delivery was correlated to the persisted `execution_outcomes.payment_link_id` — never by amount, customer, or email — resolving to the originating event. |
| **Recovery persistence** | A single recovery outcome was persisted, with the trusted amount taken from the link's own `amount_paid` (499900 paise), not from any client-supplied figure. |
| **Idempotency / adversarial** | Verified by the repository's deterministic webhook suite: identical redelivery is a 2xx no-op with no second recovery, same delivery id + different body is a 409 conflict, and a tampered body is rejected fail-closed with 401. |

The dashboard trace for the event transitions from `waiting` to `recovered`, reporting the recovered amount, recovery timestamp, and payment id.

**Three states that are deliberately distinct.** All three occurred for this event, but the architecture never conflates them:

- **Execution success** — the Payment Link operation itself ran (`execution_status: SUCCESS`). It says nothing about whether anyone paid.
- **Payment success** — a real payer completed the Test Mode checkout and Razorpay captured the payment.
- **Verified recovery** — RecoveryOS independently received, authenticated, correlated, and persisted the outcome. Only this state marks money as recovered.

A successful execution is *not* evidence of recovery, and Razorpay reporting a payment as paid is *not* by itself accepted as recovery either. Recovery is recorded only after signature verification and correlation both succeed.

## Operational limitations of the live demo

These are genuine constraints on reproducing the verification above, stated plainly.

- **Test Mode only.** All live verification uses Razorpay **Test Mode**. Test Mode does not represent production payment behaviour: it has different method availability (for example international cards are unsupported), no real settlement, no real fraud/risk decisioning, and no production rate limits or failure modes. **Nothing here demonstrates production payment processing.**
- **Cloudflare quick tunnels are ephemeral.** The hostname changes every time the tunnel restarts, and an old hostname stops resolving. The Razorpay webhook URL must point at the **currently active** tunnel.
- **Razorpay does not re-target queued retries.** A delivery that failed against a stale hostname is not redelivered to a newly configured URL. Correcting the webhook URL after a payment does not recover that delivery; a fresh payment is required. This was observed directly during verification.
- **Recovery verification depends on an external webhook arriving.** RecoveryOS is fail-closed by design and will not mark anything recovered on its own. If the webhook does not arrive, the trace correctly stays at `waiting` — which is an honest report, not a bug.
- **The hosted payment step is manual.** Completing the Razorpay checkout requires a human in a browser; it is not automatable within this repository, so the live loop cannot run unattended in CI.
- **The live loop is a single verified instance**, not a load or reliability measurement.

## AI / model variability

During Phase 14, two semantically similar fresh events were classified by the real OmniRoute classifier at `temperature = 0.0` and produced **different candidate intervention sets** — one included `payment_link`, the other returned only `alternate_method_prompt`. The consequences are documented honestly:

- **The live AI classification is genuine.** The variation is itself evidence that a real external model is being consulted rather than a canned response.
- **`temperature = 0` does not make the classifier deterministic.** Determinism is not guaranteed across requests to a hosted, routed model endpoint, so the classification stage must be treated as non-reproducible.
- **Selection is deterministic *after* classification.** Given a fixed classifier output, the policy gate and selector are pure and reproducible — the same candidates and persisted state always yield the same authorization and the same selected intervention.
- **RecoveryOS does not force `payment_link`.** When the model omits it from the candidate set, `payment_link` simply cannot be selected. No prompt coercion, retry-until-desired-answer, or post-hoc candidate injection exists, and none was added to obtain the verified trace.
- **This is an honest limitation of the current external-model dependency,** not a defect in the deterministic core. It means a live demo may need more than one event before `payment_link` is selected.

The accurate overall claim is therefore: *the intervention-selection policy is deterministic once the classifier output is available, while the external LLM classification itself can vary between equivalent events.*

## Benchmark honesty and the "no-signal" limitation

Full methodology lives in `docs/BENCHMARK.md`. The essentials:

- **The dataset is seeded and synthetic.** Events come from `app/generator.py` under an explicit seed (canonically seed 42, 500 events). **No benchmark figure is derived from real customer payment data**, from real Razorpay transactions, or from the live Trace C verification.
- **All recovery amounts are simulated** and labeled `SIMULATED` (`evaluation_mode`).
- **Disclosed limitation — the hidden outcome model carries no signal.** Recovery probabilities are drawn as independent uniform values (`rng.random()`) per (event, intervention) pair. They are **not correlated with any event feature** — not the failure reason, payment method, amount, or risk flag — and not correlated across interventions. Every intervention on every event therefore has an expected recovery probability of ≈0.5, and no intervention is genuinely better suited to any event.

  Consequently **the benchmark cannot reward intelligent targeting**, and the canonical seed-42 result reflects exactly that: No Action recovers 242/500 events, Naive Retry 246/500, and RecoveryOS 241/500 — all statistically flat at the ~0.5 the model dictates. Naive Retry's small edge comes from attempting more interventions, not from choosing better ones, and RecoveryOS is slightly behind because policy correctly blocks fraud and terminal events.

  What the benchmark **does** establish is harness integrity: fairness across a shared event set and shared model, order-invariant determinism, ground-truth isolation from the decision path, and honest accounting. What it **cannot** establish is that RecoveryOS's targeting recovers more revenue than a blanket retry. A signal-bearing outcome model would be required for that, and inventing one would mean fabricating the very correlations the system claims to exploit.

The existing methodology, calculations, and results are preserved as-is. The unflattering seed-42 result is reported rather than suppressed, and nothing was tuned to improve presentation.

## Closed-loop webhook mechanism

A secure, durable, and audit-friendly channel that turns a real Razorpay `payment_link.paid` webhook into a verified, correlated, duplicate-safe recovery outcome. The webhook is an **OUTCOME channel only**: it never invokes the executor, policy engine, selector, or link creation. It verifies an HMAC-SHA256 signature over the exact raw request body (constant-time compare, fail-closed 4xx before any parsing), then (1) durably claims the delivery under the `X-Razorpay-Event-Id` PRIMARY KEY, (2) strictly validates the `payment_link.paid` shape (link id, `status: paid`, non-negative `amount_paid`), (3) correlates to the persisted Phase 11 `payment_link_id` (never amount/customer), and (4) records a trusted recovery outcome derived only from the actual `amount_paid` observed on the link. Crash-safe: an in-flight `claimed` delivery is reprocessed to completion on retry, and the recovery write is idempotent (`INSERT OR IGNORE`), so a crash never double-counts and never loses a recovery. Dashboard traces label each real link `waiting` → `recovered`.

### Webhook guarantees

```bash
cd backend
# .env: RAZORPAY_WEBHOOK_SECRET=<shared Test Mode secret>  (never committed)
uvicorn app.main:app                              # start the API
```

- **Signature-verified, fail-closed** — `POST /webhook/razorpay` recomputes HMAC-SHA256 over the exact raw request body and compares constant-time (`hmac.compare_digest`); a bad/missing signature is a 401/400 before any parsing. The note sent from a Payment Link is expected to carry the `X-Razorpay-Event-Id` idempotency key and the signed body.
- **OUTCOME-only, architecturally separate** — this path never executes, never selects, never re-classifies, and never creates a Payment Link. It only records verified recovery evidence and labels the dashboard. (`BoundedExecutor.execute` is patched in tests to prove a webhook can never run an intervention.)
- **Durable, explicit idempotency** — `X-Razorpay-Event-Id` is the SQLite PRIMARY KEY of `webhook_deliveries` (body SHA-256 stored). Same id + same body = 2xx `deduplicated` no-op; same id + **different** body = 409 `conflict` (never overwritten, never double-recovered); persistent failure = 500 so Razorpay retries.
- **Crash-safe claim/retry** — a delivery still `claimed` (crashed mid-flight) is reprocessed to completion on Razorpay's retry, never silently dropped as a duplicate; the recovery-outcome insert is `INSERT OR IGNORE`, so a crash between the recovery write and the status update completes cleanly without double-counting.
- **Trusted, correlated outcome** — correlation matches the persisted Phase 11 `execution_outcomes.payment_link_id` (REAL_RAZORPAY + SUCCESS + payment_link). The trusted recovery amount is the `amount_paid` observed on the link, never the original webhook event amount. Verified outcomes persist to `webhook_recovery_outcomes` (delivery-id PRIMARY KEY).
- **Strictly validated shape** — a `payment_link.paid` event must carry a link id, report `status: paid`, and give a non-negative integer `amount_paid`; a malformed paid event is a 400, never silently unmatched. Unsupported events are recorded-and-ignored (2xx), never executed.

## Phase history

The sections below are a **historical record** of how each capability was built, retained for provenance and audit. They describe the phase in which a capability landed, not the current status of the repository — for current status see the top of this file.

### Phase 21 — Recovery Operations Center

```bash
cd backend
python -m pytest                                          # full suite (Phase 21 tests included)
curl -s localhost:8000/recovery/queue | head              # the operational queue
curl -s -X POST localhost:8000/recovery/<event_id>/execute -d '{}' \
     -H 'Content-Type: application/json'                  # operator execution (server decides what)
```

- **The operational question, answered from persisted state** — which failed payments need attention, what RecoveryOS diagnosed, whether policy allowed it, what was selected, whether it executed, and whether the money came back. The queue is a **projection** over `payment_events`, `classification_results`, `policy_decisions`, `optimizer_decisions`, `execution_outcomes` and `webhook_recovery_outcomes`. No `recovery_queue` table and no second lifecycle store, so a row cannot disagree with the authoritative records.
- **Execution is not recovery** — a successfully created real Payment Link is `PENDING_OUTCOME` and reads "Waiting for payment". It becomes `RECOVERED` only when the Phase 12 webhook path verifies and correlates a payment to that exact link, and the amount shown is the provider's trusted `amount_paid`.
- **Simulated is not real** — `SIMULATED` interventions reach `EXECUTED` and stop. They never carry a recovered amount, and every actionable row states its execution mode so a demo viewer cannot mistake one for the other. `payment_link` remains the only real Razorpay action, Test Mode only.
- **The operator chooses whether, never what** — `POST /recovery/{id}/execute` reuses the existing execution service, which re-derives the classification, the deterministic policy decisions and the economic selection from server state. A request supplying an intervention, an authorization, an execution mode or an evaluation time is refused with 422; nothing executes.
- **Concurrent duplicates are impossible** — the policy gate blocks sequential duplicates from persisted history, but two simultaneous requests can both read that history before either writes. A durable claim on `(event_id, intervention)` makes SQLite decide which single attempt reaches the provider. It grants no authorization and cannot make a denied candidate executable.
- **Provider uncertainty is stated, not guessed** — if the provider was called and the result could not be confirmed, the claim is parked as `PROVIDER_RESULT_UNKNOWN` and never retried automatically, because a retry could create a second real Payment Link. See `docs/RECOVERY_OPERATIONS.md` for the state table, the safety invariants and the limitations.

### Phase 20 — Revenue Health: incident-level degradation detection

```bash
cd backend
python -m pytest                                     # full suite (Phase 20 tests included)
curl -s localhost:8000/incidents | head              # detected incidents, worst modelled impact first
```

- **The system-level question, answered deterministically** — where recovery performance itself is degrading, what the modelled impact is, and which payment decisions it covers. `app/incidents.py` compares the last 28 days of the persisted workload against the immediately preceding 28 days per segment (`bank`, `payment_method`, `failure_reason`, `bank + payment_method`), requires at least 5 evaluated outcomes in each window, and raises an incident at a **15 percentage-point** recovery-rate fall. No forecasting, no anomaly model, no LLM, and no alerting or incident-workflow system.
- **Pure, derived, reproducible** — detection is a pure function of (events, evaluated outcomes, configuration) with no clock and no randomness; incident ids are deterministic digests. Incidents are **derived on every request, never stored**: no incidents table, no duplicated events, no schema change. A current detection is `OPEN`; `RESOLVED` is the pure reconciliation of a previously observed set against a newer one, since Phase 20 keeps no incident history and does not pretend to track a lifecycle.
- **The classic failure rate, published and inert** — failed ÷ total payments is calculated and exposed, and is 100% in every window and segment because RecoveryOS only ingests payments that already failed. It is reported for completeness, documented as non-discriminating by construction, and is never an input to detection; the informative failure-side metric is the unrecovered share of that volume.
- **Honest money** — recovery rate reuses the canonical definition (recovered ÷ *scored* events) in integer basis points, and **simulated revenue at risk** is the observed gap applied to the current window's payment value in integer paise. It is a **modelled estimate** of simulated evaluation evidence — never merchant loss, provider loss, or production revenue, and labelled as such in the API and the UI.
- **Reuses the existing control plane** — recovery evidence comes from the Phase 19 replay of the *active* policy (the durable pipeline records execution, not per-event recovery); affected payments link into the existing Event Decision Trace; and `POST /incidents/{id}/replay` hands that exact subset to the frozen Phase 19 `replay_scenarios`. No second event view and no second replay engine.
- **Safe by construction** — no Phase 20 module imports the Razorpay client or an HTTP client, writes to the database, or mutates the active policy or benchmark records; no hidden ground truth reaches detection or any response. See `docs/REVENUE_HEALTH.md` for the methodology, the severity table and the limitations.

### Phase 10 — Recovery Command Center & Decision Trace

```bash
cd backend
python -m app.populate --seed 42 --count 60      # deterministic demo dataset (SIMULATED execution)
python -m app.benchmark_store --seed 42 --count 500  # persist a canonical benchmark run summary
uvicorn app.main:app                              # start the API

cd ../frontend
npm run dev                                       # Vite dev server (proxies /api -> :8000)
```

**Canonical clean reset/rebuild** — for a fresh or reset database, stop the API, remove the SQLite file, then re-run the same two commands. `app.populate` is **deterministic** (fixed reference evaluation time, matching the generator/benchmark) and **idempotent** (re-running on an existing DB is a safe no-op — already-processed events are skipped, never duplicated), so the same `--seed`/`--count` reproduces the exact same persisted chain across clean rebuilds.

- **Three screens, read-only** — Command Center (`/dashboard/summary`), Event Decision Trace (`/events` + `/events/{id}/trace`), and Policy & Blocked Actions (`/decisions/blocked`). No screen writes, and no frontend recomputes policy or benchmark metrics — the backend applies the locked Phase 9 metric readers.
- **Honest figures, never invented** — Recoverable Revenue is shown as **Definition unavailable** because the repository defines no canonical metric and the hidden outcome model is evaluation ground truth that is intentionally not exposed. Benchmark comparisons come only from a persisted run; without one the panel shows an empty state rather than a guessed number. Simulated evaluation figures carry a prominent `SIMULATED` label.
- **Real persisted state** — Revenue at Risk is the sum of ingested `payment_events.amount_paise`; blocked/fraud counts come from persisted `policy_decisions`; the trace reconstructs each event from `classification_results`, `policy_decisions`, `intervention_attempts`, and `execution_outcomes`. Per-event simulated recovery is NOT recorded (only execution outcomes are), so "Revenue Not Recovered" is substantiated as **policy_blocked** or **no classification** — never as a hidden outcome.
- **No changes to frozen phases** — the decision pipeline (`benchmark.py`, `outcome.py`, `policy.py`, `selector.py`, `executor.py`, etc.) is untouched. `db.py`/`main.py` gain only additive, read-only Phase 10 entries plus the `benchmark_runs` persistence table. The full test suite (incl. the honesty guarantees in `tests/test_dashboard_api.py`) passes.

### Phase 9 — Honest Three-Strategy Benchmark

```bash
cd backend
python -m pytest                            # full suite (Phase 9 benchmark tests included)

# Run the canonical benchmark (500 events, seed 42) and print the summary
python -m app.benchmark --seed 42 --count 500
```

- **Three strategies over ONE shared event set and ONE shared hidden outcome model** (`app/benchmark.py`) — `no_action` (the control, attempts nothing and values every event at its modeled natural baseline), `naive_retry` (`retry_immediate` on every eligible non-fraud event, no AI/policy/selector, so its retries are modeled directly by the simulator — never a fabricated authorization), and `recovery_os` (the REAL pipeline: advisory classification → deterministic policy gate → selection → bounded execution through the existing SQLite schema, with recovery simulated only after execution was decided). No `benchmark_recoveryos.py` reimplementation and no `benchmark_mode` branches inside the frozen modules.
- **Fair, deterministic outcomes** — recovery for (seed, event, intervention) is drawn from the Phase 8 per-triple `random.Random(f"{seed}:{event_id}:{intervention}")`, so the outcome is identical regardless of strategy order and prior simulations. Faithful strategies can never influence each other's draws.
- **Ground-truth isolation, enforced** — hidden recovery probabilities are consulted only to simulate the outcome of an already-selected intervention. They never enter classification, policy, selection, or execution; the controlled `DeterministicClassifier` reads decision-time event fields only.
- **Spelled-out rules with no mandate to win** — fraud events are never executed by RecoveryOS (fraud interventions are measured, target 0, never hardcoded) and terminal events are never executed either; exceptions are visible and are never double-counted as not-recovered or failed; skipped is distinguished from failed from exception; `processed + skipped + exceptions == event_count` on every run. The benchmark can — and on the canonical seed does — report that RecoveryOS recovers less simulated revenue than a baseline; that honest result is reported, not suppressed.
- **Metrics** (`app/benchmark_metrics.py`) — simulated recovered revenue (integer paise, labeled simulated), recovery rate (denominator = shared event count), intervention count (No Action = 0), recovery efficiency (recovered paise per intervention, `None` when zero interventions — never a division by zero), incremental revenue over No Action, and RecoveryOS vs Naive Retry. **False-intervention rate is reported as METRIC DEFINITION AMBIGUITY** — the repository defines no canonical false-intervention threshold, so a threshold would be invented rather than measured; the raw per-event attempted/recovered foundation it would need is carried on every record.
- **No new DB schema, no new endpoints, no real Razorpay calls** — RecoveryOS runs through the existing frozen modules against an in-memory SQLite database (a stub/absent client means no provider is ever contacted), and the benchmark exposes only the CLI. All results are simulated evaluation results, never production Razorpay revenue.

### Phase 8 — Deterministic Outcome Simulation + Honest Benchmark (Foundation)

```bash
cd backend
python -m pytest                            # full suite (Phase 8 integrity tests included)

# The evaluation harness regenerates events and the hidden model from a seed,
# then simulates each intervention's outcome — fully in memory, no records needed
python -c "
from app.generator import generate_events
from app.outcome_model import generate_hidden_outcome_model
from app.outcome import OutcomeSimulator
events = generate_events(seed=42, count=10)
model = generate_hidden_outcome_model(events, 42)
for event in events[:2]:
    print(OutcomeSimulator(model).simulate(event, 'retry_delayed').to_dict())
"
```

- **Hidden outcome model (`app/outcome_model.py`)** — each synthetic event gets its own recovery probability for **exactly the locked interventions** (including `no_action`, the natural baseline). The model is evaluation-owned: the classifier, policy gate, selector, executor, and Razorpay boundary never receive or refer to it.
- **Event-specific and deterministic** — probabilities are drawn from a per-event `random.Random(f"{seed}:{event_id}")`. A probability depends only on (seed, event identity), not on event-set size, evaluation order, or any shared RNG. No `random.seed()`, no module-global mutable RNG, explicit integer seed only.
- **Explicit bounds, never clamped** — every probability must satisfy `0 <= p <= 1`; anything else (including a missing event/intervention) fails with an explicit error (`InvalidSeedError`, `InvalidOutcomeProbabilityError`, `MissingGroundTruthError`). No guessed defaults, no `except: pass`.
- **Deterministic recovery simulation (`app/outcome.py`)** — one Bernoulli draw per (event, intervention) from a private `random.Random(f"{seed}:{event_id}:{intervention}")`; the outcome for a triple is identical no matter how many simulations happen before it or in what order. `RecoveryOutcome` is minimal: `event_id`, `intervention`, `recovered`, `recovered_amount_paise` (derived: `event.amount_paise` when recovered, else 0). Hidden probabilities never appear in the record.
- **Execution success ≠ recovery success** — an executed intervention can have `ExecutionOutcome.status == "SUCCESS"` and yet simulate `recovered == false` (and vice versa). Recovery is decided only at the evaluation boundary, independently of execution.
- **Hard isolation** — hidden ground truth never reaches the LLM input/prompt, policy decisions, selection, execution, API responses, or normal logs (enforced by source-level, behavioral, and API-level integrity tests). **Possible recovery ≠ authorized recovery**: a `fraud_suspect` event stays denied even when the hidden model says recovery is certain.
- **No persistence, no new endpoints** — the model and outcomes are harness-side state, regenerated deterministically; nothing is written to the database and no `/benchmark` or `/ground-truth` API exists. Deleting the DB and re-running the pipeline requires no manual benchmark records.

### Phase 4 — Spinning Up Development Data

```bash
cd backend
python -m pytest                              # full suite
python -m app.generator --seed 42 --count 10  # deterministic dev dataset (JSON)
```

- **What an ingested PaymentEvent represents** — a single failed, declined, or abandoned payment attempt from the locked Phase 2 domain contract, validated on entry and stored as one row in `payment_events`. Ingestion never alters, deletes, or silently overwrites an existing event.
- **How synthetic events are generated** — `app/generator.py` draws from the locked domain value sets (payment methods, risk flags), realistic INR amounts in paise, deterministic ISO8601 timestamps, and unique `evt_`/`order_`/`pay_` identifiers. Customer IDs may repeat across events for the same synthetic customer.
- **Seeded generation is deterministic** — the generator uses `random.Random(seed)`, never the global random source. The same seed and generation parameters reproduce the exact same dataset (and the same identifiers), on any machine.
- **Decision-time information only** — generated events contain only information available at decision time. No recovery probability, true outcome, benchmark score, simulated revenue, or any other future-knowledge field exists. **Benchmark ground truth is intentionally absent.**

### Phase 5 — Advisory AI Classification

```bash
cd backend
source .venv/bin/activate
# configure the model layer, then run the API
export OMNIROUTE_API_KEY=...                    # never commit this
export OMNIROUTE_MODEL=...                      # see .env.example
uvicorn app.main:app

# classify an ingested event (advisory output, no execution)
curl -X POST http://127.0.0.1:8000/events/<event_id>/classify
```

- **AI is advisory** — the classifier (via OmniRoute) diagnoses the likely root cause and recommends candidate interventions only. It cannot authorize, select, or execute an action, and it never calls the payment provider.
- **Structured classification contract** — every model response is validated against the locked Phase 5 contract: `root_cause_category` must be one of `transient`, `customer_action_needed`, `fraud_suspect`, `terminal`; `confidence` must be between 0 and 1; every `candidate_interventions` entry must be one of `retry_immediate`, `retry_delayed`, `payment_link`, `reminder`, `alternate_method_prompt`, `no_action`. Malformed or invalid output triggers at most one retry, then an explicit failure — never a fabricated classification.
- **Decision-time information only** — the model receives only the locked PaymentEvent fields. Benchmark ground truth, true outcomes, and recovery probabilities are structurally absent.
- **Policy remains authoritative** — the deterministic policy gate (Phase 6) decides whether a proposed intervention is permitted. This phase ends at classification and persistence.

### Phase 6 — Deterministic Policy Safety Gate

```bash
cd backend
source .venv/bin/activate
uvicorn app.main:app

# evaluate one proposed intervention (no execution, no provider calls)
curl -X POST http://127.0.0.1:8000/events/<event_id>/policy \
  -H "Content-Type: application/json" \
  -d '{"proposed_intervention": "retry_delayed", "evaluation_time": "2026-08-27T13:00:00+00:00"}'
```

- **Authority path** — the LLM only recommends; the deterministic policy engine authorizes (ALLOW/DENY); the future executor acts. There is never an `execute=true` supplied by the LLM.
- **Six locked rules, evaluated in a fixed order** — (1) fraud protection (`fraud_suspect` events are always denied), (2) terminal failure block, (3) duplicate successful-intervention protection, (4) max 2 interventions per customer per rolling 24h, (5) 30-minute event cooldown, (6) configurable daily spend cap (rolling 24h, global). The first blocker determines the denial reason; the same inputs always produce the same decision. This order is the authoritative `DETERMINISTIC_RULE_ORDER` in `app/policy.py` and matches `docs/ARCHITECTURE.md`.
- **Fail-closed by construction** — malformed input, unknown interventions, timezone-naive timestamps, and history lookups that cannot be determined safely produce explicit controlled errors; policy never fabricates history, spend, or duplicates and never fails open.
- **Historical facts come from persisted state** — the customer 24h count, most-recent event intervention, successful-duplicate flag, and daily spend are computed by the persistence boundary (`intervention_attempts`) with actual datetime arithmetic against the explicit evaluation timestamp, never from the LLM and never from shadow in-memory state.
- **Persistence** — every evaluation is persisted to `policy_decisions` (correlated by `event_id`, preserving the decision contract). No execution exists: nothing in Phase 6 records a successful intervention.

### Phase 7 — Deterministic Selection + Bounded Execution

```bash
cd backend
source .venv/bin/activate
uvicorn app.main:app

# select + execute the authorized intervention for an event (server decides everything)
curl -X POST http://127.0.0.1:8000/events/<event_id>/execute     # no body, no client authorization
```

- **Never an LLM → executor path** — the LLM only proposes candidates. The deterministic policy gate independently authorizes each candidate; the selector picks exactly one among those with `allowed == true`; the executor independently requires an authoritative ALLOW decision before acting.
- **Locked V1 priority** — `retry_delayed > payment_link > reminder > alternate_method_prompt > retry_immediate`. When every actionable candidate is denied (or none exists), the explicit result is `no_action` — which is never executed and never simulated.
- **Deterministic selection** — the selector (Phase 7) uses no LLM reasoning, no randomness, no recovery predictions, and no economic optimization; it only intersects candidates with authoritative ALLOW decisions and applies the fixed priority.
- **Bounded executor** — the executor author ≠ policy engine: it rejects any `PolicyDecision.allowed == false`, rejects mismatched event/intervention bindings, refuses `no_action`, never calls the LLM, and never decides recoverability.
- **Explicit execution modes** — `retry_immediate`, `retry_delayed`, `reminder`, and `alternate_method_prompt` run as `SIMULATED` (the operation is recorded as executed; there is **no outcome/recovery model**, simulated or real); `payment_link` runs as `REAL_RAZORPAY` (Razorpay **Test Mode** only — `rzp_live_` keys are rejected at the client boundary, via the isolated `razorpay_client` boundary). Provider failures and configuration gaps produce explicit `FAILED` outcomes — never fabricated success, never a guessed Payment Link URL.
- **No client bypass** — `POST /events/{event_id}/execute` accepts no intervention and no `allowed` flag; the authoritative chain (classification → policy → selection → executor) fully determines what executes, evaluated against server-side time.
- **Persistence** — outcomes are appended to `execution_outcomes` (correlated by `event_id`); each execution also records an `intervention_attempts` row so Phase 6 policy facts (customer limits, cooldown, spend, duplicates) stay derived from persisted state. Historical decisions and outcomes are never overwritten.
- **Execution success ≠ revenue recovery outcome** — `SUCCESS` means only that the operation itself ran. Phase 7 contains no outcome model: whether revenue was recovered is answered by the later benchmark/outcome layer, never by selection or execution.

What Phase 9 can and cannot do: it can measure RecoveryOS against No Action and Naive Retry over a shared event set and shared hidden model, report simulated metrics honestly, and note metric-definition ambiguity where the repo defines no canonical threshold. It cannot recover real revenue, rank interventions by expected value, or add a dashboard or V2 optimizer.

What Phase 8 can and cannot do: it can deterministically simulate whether an executed intervention recovered an event, and it keeps that ground truth strictly out of the decision path. It does not itself run the final benchmark strategies/metrics on that simulation (that is now Phase 9), cannot rank interventions by expected value, and does not add any dashboard or V2 optimizer.

What Phase 7 can and cannot do: it can select and execute (simulate or create a Test Mode Payment Link) a single authorized intervention per event. It cannot benchmark, cannot estimate recovery, cannot rank by expected value, and there is no audit dashboard or V2 optimizer.

What Phase 6 can and cannot do: it can evaluate and persist advisory policy decisions. It cannot select the best intervention, rank candidates, or execute anything. Selection, executor, Razorpay integration, benchmark, and dashboard were delivered by the later phases recorded above.

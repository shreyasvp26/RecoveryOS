# RecoveryOS — Five-Minute Demo

A deterministic, rehearsable walkthrough of the closed recovery loop. The demo
**never relies on hardcoded fake values** — every number comes from the
persisted backend state that the documented bootstrap commands produce.

Two paths exist. Run the **full live path** when Razorpay Test Mode
credentials, a webhook tunnel, and Test Mode Payment Link capacity are
available; otherwise run the **offline evaluation path**, which needs no
credentials and still demonstrates the whole decision, policy, safety, and
recovery-intelligence story.

## Setup (both paths)

```bash
cd backend
python -m app.populate --seed 42 --count 500         # initialize + seed synthetic data
python -m app.benchmark_store --seed 42 --count 500  # persist the Phase 17 benchmark summary
uvicorn app.main:app                                 # backend on :8000

cd ../frontend
npm install && npm run dev                           # frontend on :5173
```

To reset for a clean demo: stop the API, delete the SQLite file, and re-run
the two `python -m` commands above. `app.populate` is deterministic and
idempotent, so the persisted chain reproduces exactly.

Verify you are looking at real persisted data: the Command Center's Revenue at
Risk and the benchmark panel should show actual (seeded) numbers — never a
placeholder.

## Full live path (requires Razorpay Test Mode)

Requires, and only works with:

- `RAZORPAY_KEY_ID` / `RAZORPAY_KEY_SECRET` (Test Mode, `rzp_test_` prefix).
- `RAZORPAY_WEBHOOK_SECRET` + a public tunnel registered as
  `https://<tunnel>/webhook/razorpay` in the Razorpay Dashboard.
- Available Razorpay **Test Mode** Payment Link creation allowance.

### Timed walkthrough

- **0:00–0:30 — Revenue at Risk.** Open Command Center. Explain the problem:
  businesses lose real revenue to failed payments; the panel shows the sum of
  ingested failed payments at risk.
- **0:30–1:15 — Architecture.** Point to the layered flow: AI → Economics →
  Policy → Execution → Verification → Feedback, and the rule that the LLM is
  advisory while policy is authoritative.
- **1:15–2:15 — Benchmark.** Open this tab. Show the three arms — No Action,
  Naive Retry, RecoveryOS (V2) — clearly labeled **SIMULATED EVALUATION**.
- **2:15–3:15 — Live Recovery Operations decision.** Open Recovery Operations,
  pick a policy-allowed event, expand it, and walk: failure → diagnosis →
  estimate → optimizer → policy → allowed action.
- **3:15–3:50 — Real Razorpay Test Mode Payment Link.** Execute the event.
  The row shows a `REAL_RAZORPAY` Payment Link. Open the link and complete the
  Test Mode checkout in a browser.
- **3:50–4:20 — Verified outcome.** The trace transitions `waiting` →
  `recovered` once the HMAC-verified `payment_link.paid` webhook arrives,
  reporting the provider's trusted amount.
- **4:20–4:45 — Blocked event.** Show an event blocked by the deterministic
  gate (fraud / cooldown / retry limit). Emphasize: the AI can recommend, but
  it cannot override policy.
- **4:45–5:00 — Recovery Intelligence.** Show verified outcomes → evidence →
  estimator, and the versioned calibration snapshot.

## Offline evaluation path (no Razorpay credentials)

Works without any live provider. The synthetic seed, benchmark, decision
traces, policy blocks, and Recovery Intelligence all render from persisted
backend state.

```bash
cd backend
python -m app.populate --seed 42 --count 500
python -m app.benchmark_store --seed 42 --count 500
uvicorn app.main:app
cd ../frontend && npm run dev
```

Walk: Command Center → Revenue Health → Recovery Operations (expand an event
and show the decision chain) → an event blocked by policy → Policy Lab
(simulation) → Recovery Intelligence → Estimator Evidence. All SIMULATED and
benchmark labels are explicit.

**Never fake the live provider result.** If credentials are unavailable, say
so plainly and run the offline path. A screenshot or narration must not claim
a real Razorpay payment or a verified recovery that did not happen.

## Fallback decision table

| Component | Live path | Offline path |
| --- | --- | --- |
| Synthetic seed + benchmark | Yes | Yes |
| Decision trace / economic reasoning | Yes | Yes |
| Policy allow / block | Yes | Yes |
| Policy Lab (simulation) | Yes | Yes |
| Recovery Intelligence / estimator | Yes | Yes |
| REAL_RAZORPAY Payment Link | Yes | Not available |
| payment_link.paid webhook → verified recovery | Yes | Not available |

## Razorpay Test Mode limitation

Razorpay **Test Mode** has a limited Payment Link creation allowance.
RecoveryOS is therefore **not** designed around mass Payment Link creation —
the real Razorpay integration is the authentic, single-loop demonstration
path, and the batch benchmark is synthetic/simulated. Never blur these: the
benchmark's revenue is simulated evaluation output, not a load or reliability
measurement of the Payment Link path.

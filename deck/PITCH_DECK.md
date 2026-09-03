# RecoveryOS — 5-Minute Pitch Deck · Deliverable

**Event:** Razorpay AI Buildathon 2026 · Revenue Recovery track
**Deck:** `recoveryos_pitch.html` (present) · `recoveryos_pitch.pdf` (16:9, 10 pages)
**Render:** `assets/final_01.png … final_10.png` (1920×1080 previews)

---

## 1. Final deck files

| File | Purpose |
| --- | --- |
| `recoveryos_pitch.html` | The presentation. Open in any browser. Arrow keys / Space / N navigate; press `N` for the speaker-notes bar; progress bar on top; hash `#s{n}` deep-links. |
| `recoveryos_pitch.pdf` | 10-page 16:9 static export (1920×1080 pages) with backgrounds preserved. |
| `assets/final_01.png … final_10.png` | Per-slide 1920×1080 renders for review. |

No external dependencies beyond Google Fonts (Inter / IBM Plex Mono) which fall back to system fonts offline.

---

## 2. Slide-by-slide structure

**Narrative arc:** Problem → Product → Product in action → Intelligence & control → Safe denial → Verified recovery → Measured value → Engineering trust → Honest scope → Close.

### Slide 1 — The Hook ("Why should I care?")
- Headline: *"Failed payments aren't always lost revenue."*
- Chain: `PAYMENT FAILED → RECOVERYOS → RECOVERED`
- One line: revenue at risk is revenue that simply hasn't been recovered yet.
- Dark editorial cover. No architecture. ~25–30s.

### Slide 2 — What is RecoveryOS ("What is this?")
- Headline: *"RecoveryOS closes the loop on lost revenue."*
- Lifecycle strip (the most important product diagram): `PAYMENT EVENT → AI DIAGNOSIS → POLICY → RECOVERY ACTION → EXECUTION → VERIFICATION → RECOVERY`.
- Thesis line (the heart of the deck): **AI proposes. Policy decides. Recovery is verified.**
- ~35–40s.

### Slide 3 — Product in action ("Does it work as a product?")
- Operator console representation built from **real persisted backend state** (verified against the running instance; SIMULATED figures are badged).
- KPI strip: Revenue at Risk ₹53,40,990 · Simulated Recovered ₹9,20,207 (SIMULATED) · Recovery Rate 17.0% (SIMULATED) · Blocked Interventions 1,685.
- Recovery queue table: a real event recovered via a **signed webhook** (`evt_p14traceC…50Z_02`, ₹4,999), plus two policy **DENY** categories (fraud 1,285 / cooldown 30m).
- Batch, not a one-off. ~35–40s.

### Slide 4 — Intelligence + Control ("How does AI participate?")
- Headline: *"AI handles ambiguity. Policy retains authority."*
- Boundary diagram: **AI Diagnosis → AI Proposal | Deterministic boundary | Policy Authorize → ALLOW/DENY → Executor**.
- Callouts: "LLM output is advisory" · "Authorization is deterministic."
- The most important technical idea, made visual. ~30s.

### Slide 5 — The system can say NO ("How does it stay safe?")
- Headline: *"A good recovery system knows when NOT to act."*
- One concrete denial: AI proposes retry → policy checks → **DENY** (cooldown active, 30 min) → NO ACTION.
- Supporting safety line: fraud interventions **0** · unauthorized executions **0** · every benchmark seed.
- A safe denial is correct behavior. ~30s.

### Slide 6 — Action ≠ Recovery ("How do you know it worked?")
- Headline: *"Sending the action isn't the same as recovering the money."*
- Closed loop: `RECOVERY ACTION → RAZORPAY → CUSTOMER PAYS → SIGNED WEBHOOK → VERIFICATION → RECOVERED`.
- Tech proof strip: OUTCOME channel · HMAC-SHA256 over raw body · provider-trusted amount.
- ~35–40s.

### Slide 7 — Measured value ("Does it create value?")
- Same 500 payments, three strategies, labelled **SIMULATED EVALUATION**.
- Bar chart + KPIs: **+₹6,82,130 vs No Action · 93.1% Oracle value capture · 0 fraud/unauthorized** (V2 uses 180 interventions vs Naive's 240).
- Honest framing: proves *targeting*, not production revenue. ~30s.

### Slide 8 — Engineering trust ("Is there serious engineering underneath?")
- Six guarantees: Deterministic authority · Append-only audit trail · Idempotency · Bounded spend · Fail-closed verification · Stateful recovery lifecycle.
- Footer: 1,600+ tests · CI on every change. ~30s.

### Slide 9 — Real product, honest scope ("What is real today?")
- Two columns: **REAL** (running control plane, Razorpay Test Mode integration, verified webhook recovery, operator console, audit trail, policy engine) vs **SIMULATED** (batch economics, 500-event evaluation, hidden world).
- Closing line: *"We know exactly what we built, and we know exactly what remains."*
- ~25–30s.

### Slide 10 — Close
- Wordmark: **RecoveryOS** · thesis line · **BOUNDED · LOGGED · PROVABLE**.
- Future line: *"From Test Mode validation → real merchant recovery."*
- Final line: *"It's not just an LLM connected to a payment API."* ~10–15s.

---

## 3. Speaker script + timing (~4:55 total)

> **N = toggle speaker notes** on any slide. Full script below; ~60–75 wpm pace.

**Slide 1 (25s):** A payment fails. Revenue becomes at risk. Most systems stop at "payment failed." RecoveryOS continues the journey — diagnose, decide, act, verify, recover. A failed payment is not always lost revenue. It is revenue that simply hasn't been recovered yet.

**Slide 2 (35s):** RecoveryOS closes the loop on lost revenue. This is the whole product in one glance — one lifecycle from a failed payment event to a verified recovery. Payment event → AI diagnosis → deterministic policy → recovery action → execution → verification → recovery. The one line to remember: AI proposes. Policy decides. Recovery is verified.

**Slide 3 (35s):** This isn't a concept — it's a running product. One operator console over a batch of 500 evaluated payment events. Every value here is real persisted state, not a mock: over ₹53 lakh of revenue at risk, a simulated recovered figure, a 17% recovery rate, and more than 1,600 interventions blocked by policy. Two stories in the queue: an event allowed and recovered through a verified webhook — and events denied by policy. Same product, working across a batch, not one hand-picked payment.

**Slide 4 (30s):** This is the most important idea in the system — the split between intelligence and authority. AI handles the ambiguity: it diagnoses why a payment failed and proposes what might work. But the model is advisory. It cannot authorize anything. Authority lives in a deterministic policy gate. LLM output is advisory. Authorization is deterministic. That boundary is what makes an AI system safe to put in front of money.

**Slide 5 (30s):** A good recovery system knows when not to act. Here the AI recommends retrying a payment. The policy gate checks it and denies it — an intervention just happened within the 30-minute cooldown. No action executes. The reason is recorded. A safe denial is not a failure; it is the system working correctly. The model is bounded, authority stays deterministic — and in the benchmark that meant zero fraud interventions and zero unauthorized executions.

**Slide 6 (35s):** Sending the action isn't the same as recovering the money. The loop is only closed when Razorpay confirms the payment. RecoveryOS verifies the resulting state — it never assumes success. A signed webhook, HMAC-verified over the raw body, correlated to the exact payment link, reports the amount actually paid. The provider's number, not ours. Action is not recovery — recovery is only success when it is verified.

**Slide 7 (35s):** We measured it. Same 500 failed payments, three strategies, one hidden outcome model. Do nothing: ₹238k. Retry everything: ₹246k. RecoveryOS with economic targeting: ₹920k — nearly four times the control, capturing 93% of what the policy-bounded oracle says is theoretically possible, and doing it with fewer interventions than naive retry. To be clear: these are simulated evaluation results, not production revenue. The benchmark proves targeting — choosing the right intervention for the right failure — which is exactly what matters.

**Slide 8 (30s):** There's serious engineering underneath the UI — exactly the engineering that matters when you move money. Deterministic authority: the optimizer can never widen the set of allowed actions. Append-only audit trail: decisions are written before they execute, and the whole loop is reconstructable. Idempotency: a durable claim ensures only one attempt crosses the money-moving boundary. Bounded spend: a daily cap, a cooldown, per-customer limits. Fail-closed verification and a stateful lifecycle. These are the guarantees that make a financial product deployable.

**Slide 9 (25s):** Let me be precise about what is real and what is evaluated. Real and running: the control plane, the Razorpay Test Mode integration — a genuine payment recovered through a real signed webhook — the operator console, the audit trail, the policy engine. Simulated: the large-scale batch economics — the 9-lakh figure is a benchmark over a synthetic world. We know exactly what we built, and exactly what remains. In a product that handles money, that honesty is a feature, not a limitation.

**Slide 10 (15s):** RecoveryOS. AI proposes. Policy decides. Recovery is verified. Bounded. Logged. Provable. Built to move from Test Mode validation into real merchant recovery. It's not just an LLM connected to a payment API — it's a recovery control plane: intelligent, controlled, verifiable, measurable.

---

## 4. Timing summary

| Slide | Topic | Time | Cumulative |
| --- | --- | --- | --- |
| 1 | The hook | 0:25 | 0:25 |
| 2 | What is RecoveryOS | 0:35 | 1:00 |
| 3 | Product in action | 0:35 | 1:35 |
| 4 | Intelligence + control | 0:30 | 2:05 |
| 5 | System can say NO | 0:30 | 2:35 |
| 6 | Action ≠ Recovery | 0:35 | 3:10 |
| 7 | Measured value | 0:35 | 3:45 |
| 8 | Engineering trust | 0:30 | 4:15 |
| 9 | Honest scope | 0:25 | 4:40 |
| 10 | Close | 0:15 | **4:55** |

---

## 5. Facts and numbers used (all verified against the repository)

**Real, persisted operational state (backend `recoveryos.db`, dashboard endpoints):**
- Revenue at Risk **₹53,40,990** ("sum of ingested failed payments")
- Interventions executed **165** (63 succeeded) · **Blocked interventions 1,685** · **Fraud actions blocked 1,285**
- Recovery queue state: Recovered 1 · Pending outcome 1 · Executed 60 · Blocked 335 · Failed 102
- Real recovered event: `evt_p14traceC_20260828T1950Z_02`, ₹4,999, expired card / renewal failed, `customer_action_needed`, RECOVERED via signed `payment_link.paid` webhook; `evt_p14live_20260828T1655Z_01` also recovered
- Razorpay **Test Mode only**, Payment Link = the only `REAL_RAZORPAY` action

**Phase 17 canonical benchmark (SIMULATED, seed 42, 500 events, `phase17_signal_bearing_v1`):**
- No Action ₹238,077 · Naive Retry ₹245,603 · RecoveryOS V1 ₹843,352 · **RecoveryOS V2 ₹920,207** · Oracle ₹1,101,885
- V2 vs No Action: **+₹6,82,130** · V2 vs V1: +₹76,855 realized / +₹1,00,006.72 true EV
- Incremental Oracle value capture: **V2 93.1%** · V1 79.4% · Naive 4.3%
- Interventions: V2 180 vs Naive 240 · V1/V2 disagree on 129/500 events
- Safety (V1 & V2, all seeds): **fraud intervention 0%, unauthorized executions 0, exceptions 0**; Naive 29.6% false-intervention rate
- Phase 9 (frozen, no-signal) honestly reported as flat ~0.5 — the benchmark ran and lost when there was no signal to find

**Policy engine (defaults in `policy.py`):**
- Six rules, fixed order, fail-closed: fraud protection · terminal failure · duplicate intervention · customer limit (2/customer/24h) · cooldown (30 min/event) · spend cap (₹50,000/rolling 24h)

**Webhook verification (`razorpay_webhook.py`):**
- HMAC-SHA256 over the exact raw body · constant-time compare · fail-closed 4xx before parsing · `payment_link.paid` is the only consumed event · outcome channel only (never executes) · recovery amount = provider-trusted `amount_paid`

**AI layer:** OmniRoute-backed advisory classifier → root_cause_category ∈ {transient, customer_action_needed, fraud_suspect, terminal}, confidence ∈ [0,1], candidate interventions. Never authorizes. Selection deterministic after classification.

**Quality:** 1,623 pytest tests (README/docs + `tests/`) · CI on every push · append-only audit trail (decisions written before execution).

---

## 6. Placeholders / points that require verification before presenting

Nothing on the slides is invented, but confirm these before going live:

1. **Live numbers freshness** — Slide 3 KPIs (₹53,40,990, ₹9,20,207, 17.0%, 1,685) are from the current `recoveryos.db`. Re-run `python -m app.populate --seed 42 --count 500` + `python -m app.benchmark_store --seed 42 --count 500` to reproduce byte-identical state; if the DB is re-seeded with different counts the numbers must be re-read from `/dashboard/summary`.
2. **Slide 3 "500 recovered-and-evaluated payment events"** — accurate for the benchmark; the recovered count in the live queue is 1 (plus 1 pending). The slide's batch framing refers to the 500-event evaluation; ensure the spoken framing matches (Slide 3 script already does).
3. **"1,600+ tests"** — derived from 1,623 tests; if the suite changes, update.
4. **Real webhook verification claim (Slide 3, 6, 9)** — refers to Trace C, demonstrated once end-to-end against real Razorpay Test Mode (real ₹4,999 netbanking payment, genuine HMAC-verified `payment_link.paid` webhook). If the panel asks for a live rerun, follow `docs/DEMO.md` full live path.
5. **Google Fonts `@import`/`<link>`** — requires network to fetch Inter & IBM Plex Mono; the deck falls back to system fonts offline (still clean).
6. **Screen-shots vs faithful representation** — authentic headless UI captures proved unreliable in this environment, so Slide 3 uses a pixel-faithful, data-verified representation of the real Command Center built from the exact persisted values. If authentic screenshots are preferred for delivery, re-capture from the running app (standard, non-headless capture) and swap into the panel area.

---

## 7. Anti-checklist (self-audit result)

- ✅ Fits 5 minutes (4:55 script)
- ✅ Product understandable in < 30s (Slide 1–2)
- ✅ Revenue problem obvious
- ✅ Product lifecycle visual (Slide 2)
- ✅ AI role + deterministic policy boundary (Slide 4)
- ✅ A DENY case with real reason (Slide 5)
- ✅ Closed-loop + verification (Slide 6)
- ✅ Batch represented (Slide 3 + 7)
- ✅ Benchmark evidence with accurate, labelled-SIMULATED numbers (Slide 7)
- ✅ Real vs simulated honest (Slide 9)
- ✅ Engineering depth visible, not overwhelming (Slide 8)
- ✅ Safety + auditability visible
- ✅ No phase history, no feature dump, no fabricated claims
- ✅ Intentional whitespace, editorial fintech aesthetic, no AI-generic visuals
- ✅ Final slide memorable (thesis + bounded/logged/provable)
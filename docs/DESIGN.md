# RecoveryOS Phase 10 Dashboard Design

The Recovery Command Center is a **read-only operator surface** over the
persisted decision chain. It deliberately holds no policy, benchmark, or
decision logic — every number is assembled by the backend from persisted
state. This document records the visual system and the honesty rules the UI
enforces.

## Design Principle

Purpose-first and system-status driven: the dashboard answers "what happened,
what did we do, what did we stop, and what is simulated?" — never "here is a
winning number".

Three honesty rules are enforced in the UI:

1. **Simulated is always labelled.** Any figure derived from the Phase 9
   simulated benchmark carries a `SIMULATED` badge; it is never presented as
   production/recovered revenue.
2. **Unavailable is not guessed.** Recoverable Revenue has no canonical
   definition in the repository, so it renders as **Definition unavailable**
   with the backing note, rather than an invented metric.
3. **Empty is not failure.** "No events match", "no blocked interventions",
   and "benchmark not run" are distinct empty states; a failed fetch is a
   distinct error state with a retry action. API failures are never masked as
   zeros or a fabricated value.

## Design Tokens

Defined in `frontend/src/index.css` as CSS custom properties, using a dark
"control-plane" theme:

- Surfaces: `--bg` (canvas), `--bg-elev` (sidebars/panels), `--bg-card`
  (cards), `--bg-inset`, `--bg-card-hover`.
- Borders: `--border`, `--border-strong`.
- Text: `--text-hi` (headings/ids), `--text` (body), `--text-dim`,
  `--text-faint`.
- Semantics: `--brand` (RecoveryOS accent), `--success` (allowed/executed),
  `--warn` (blocked/simulated), `--danger` (fraud/denied), `--info`.
- Typography: `--sans` for UI, `--mono` for ids/amounts (monospace keeps
  numeric data scannable).

## Screens

### Recovery Command Center (`components/CommandCenter.jsx`)
- KPI band: Revenue at Risk, Interventions Executed (success/total), Blocked
  Interventions, Fraud Actions Blocked, Events Ingested, Policy Decisions.
- **Recoverable Revenue** panel — honest unavailable state.
- **Simulated Benchmark Comparison** — the three strategies, each with
  recovered amount, recovery rate, and efficiency, plus RecoveryOS-vs-baseline
  deltas, all under a `SIMULATED` badge.
- **Revenue Not Recovered** — policy-blocked vs no-classification, from
  persisted state (never a hidden outcome).

### Event Decision Trace (`components/EventTrace.jsx`)
- Searchable/filterable persisted event list on the left.
- Vertical timeline for the selected event: **Ingest → AI Classification →
  Policy Gate(s) → Execution → Outcome → Closed-loop verification (Phase 12)**,
  with a final-decision banner (ALLOW / DENY / NO ACTION / NOT CLASSIFIED).
- **Closed-loop verification (Phase 12)** renders each real Razorpay Payment
  Link as `WAITING` (neutral/warn) until a verified `payment_link.paid` webhook
  marks it `RECOVERED` (success) with the trusted `amount_paid`. It never
  fabricates a recovered amount; events with no real link show an honest empty
  state instead.
- Explicit "No AI classification" / "Not executed" empty states.

### Policy & Blocked Actions (`components/PolicyBlocks.jsx`)
- Block-category breakdown (Fraud, Retry limit, Cooldown, Terminal, Duplicate,
  Spend cap).
- Denied-intervention table: event, customer, amount, risk flag, proposed
  action, denial rule, category, evaluated time.

## Data Integrity in the UI

- Financial amounts are displayed from integer paise via integer-only
  formatters (`core/format.js`); the persisted paise value is never converted
  to a float or mutated.
- The `useAsync` hook (`core/api.js`) exposes `loading / ok / error` so every
  screen renders a real loading state, a real error state, or data — never a
  hardcoded figure, and never `data?.recovered ?? 0`.
- Reads go through the Vite `/api` dev proxy to the FastAPI backend.

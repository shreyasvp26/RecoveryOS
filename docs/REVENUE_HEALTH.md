# RecoveryOS Revenue Health — incident-level degradation detection (Phase 20)

RecoveryOS decides what to do about one failed payment. Phase 20 adds the
system-level question above that loop:

> **Where is recovery performance itself degrading, what is that worth, which
> payments does it cover, and would a different policy have done better on
> exactly those payments?**

Everything below is computed by `app/incidents.py` (pure detection) and
`app/incident_analysis.py` (evidence and Policy Lab wiring), and exposed by
`app/routes/incidents.py`.

## What this is not

Not a monitor, not a forecaster, not an anomaly model, and not an alerting
system. There is no streaming infrastructure, no EWMA, no ARIMA, no z-score
framework and no learned baseline. There is no incident workflow either — no
acknowledgement, assignment or escalation — and detection never changes a
policy and never executes anything.

## Methodology

```
persisted PaymentEvents
   + observed evaluated outcomes (Phase 19 replay of the ACTIVE policy)
        ↓
two equal-width time windows
        ↓
deterministic aggregation per segment
        ↓
minimum-sample gate
        ↓
recovery-rate degradation threshold
        ↓
modelled financial impact  →  severity
        ↓
INCIDENT → affected events → existing Event Decision Trace
                           → existing Phase 19 Policy Lab replay
```

### Windows

The anchor is the **latest observed event timestamp** in the analysed dataset —
never the wall clock, so the same dataset always yields the same windows.

```
baseline = (anchor - 56 days, anchor - 28 days]
current  = (anchor - 28 days, anchor]
```

Both windows are **exclusive at the start and inclusive at the end**, so an
event exactly on `anchor - 28 days` belongs to the baseline, an event exactly
on the anchor belongs to the current window, and the two windows partition
their span with nothing counted twice and nothing lost at the seam. Events
outside both windows are ignored.

### Segmentation

Four fixed segmentations, evaluated independently: `bank`, `payment_method`,
`failure_reason`, and the composite `bank + payment_method` (a degradation
often lives in one rail at one bank and is diluted by either dimension alone).
This is a fixed tuple, not a general multidimensional analytics engine. Segment
order is fixed by the declaration order and then lexically by value.

### Minimum sample

A segment is compared only when **both** windows produced at least **5
evaluated outcomes**. Three or four current-window payments never raise an
incident, however badly they performed. The denominator is *scored* events —
events with an evaluated outcome — so twenty payments with four outcomes is a
sample of four. Sample size is an eligibility gate; it never contributes to
severity.

### Metrics

Rates are integer **basis points** (10,000 bps = 100%, one percentage point =
100 bps), so no rate is a float that later multiplies money.

| Metric | Definition |
| --- | --- |
| Recovery rate | recovered events ÷ scored events — the canonical RecoveryOS definition, identical to `replay_metrics.recovery_rate` |
| Unrecovered rate | 1 − recovery rate |
| Observed payment-event failure rate | failed payments ÷ total payments — the classic metric, stated explicitly |
| Degradation | `baseline recovery rate − current recovery rate` (positive = worse) |
| Simulated revenue at risk | `max(0, degradation) × current-window payment value ÷ 10,000`, in integer paise |

**On "failure rate".** The classic failure rate is calculated and published —
`observed_failure_rate_bps`, on the analysed population in `GET /incidents` and
on both windows of every incident — but it is **non-discriminating here, by
construction rather than by measurement**. RecoveryOS only ever ingests payments
that have ALREADY failed, so the population is failure-selected at the door and
the rate is 10000 bps (100%) for any non-empty population: identical in the
baseline and the current window, identical in every segment, unable to move. The
published delta between the two windows is therefore always 0. An empty
population reads 0 rather than dividing by zero. It is never an input to
detection, and it cannot raise an incident.

The meaningful failure-side reading is the **unrecovered rate**: how much of that
failed volume the control plane did not recover. It is the exact complement of
the recovery rate, is reported as supporting evidence, and can never raise an
incident of its own either — every incident is revenue-oriented and comes from
the recovery-rate rule.

### Detection threshold

An incident exists when, for one segment:

```
current scored  >= 5
baseline scored >= 5
degradation     >= 15 percentage points (1500 bps)
```

72% → 54% (−18 pp) is an incident. 72% → 64% (−8 pp) is not. Improvement never
is, at any magnitude.

### Severity

Two gates in a fixed order, with no model and no probability:

1. **Deviation proposes** — ≥15 pp LOW, ≥20 pp MEDIUM, ≥30 pp HIGH, ≥40 pp
   CRITICAL.
2. **Impact confirms** — every level above LOW additionally requires at least
   its affected-event count **or** its revenue at risk. A candidate that fails
   its impact test is demoted one level and re-tested, down to LOW.

| Level | Affected events | or simulated revenue at risk |
| --- | --- | --- |
| MEDIUM | ≥ 10 | ≥ ₹10,000 |
| HIGH | ≥ 25 | ≥ ₹50,000 |
| CRITICAL | ≥ 50 | ≥ ₹1,00,000 |

So severity is `min(deviation level, highest impact-qualified level)`: a 60 pp
swing over six small payments stays LOW rather than becoming CRITICAL on the
strength of a tiny denominator.

### Affected payments

The payments an incident covers are the **current-window payments in that
segment that were evaluated and stayed unrecovered**, sorted by event id. They
are carried as *ids* — references into the existing `payment_events` — never as
copies, so the incident points at real decision chains.

### Leading observed contributor

The most frequent failure reason in the current window, ranked by current count,
then by largest increase over the baseline, then lexically by reason, so the
answer is unique for every possible dataset. It is deliberately **not** called a
root cause: RecoveryOS has established no causal link, only a count and a
movement.

### Identity, status and ordering

`incident_id` is a blake2b digest of the methodology, the detector
configuration, both window bounds, the segment and the observed metrics on both
sides — no wall clock and no random component, matching the fingerprint
convention already used by the benchmark, the policy scenarios and the event
generator. `detected_at` is the **latest observed event timestamp**, not a
production detection time.

Incidents are **derived, never stored**: no incidents table, no duplicated
events, no schema change.

Status follows from that directly:

| Status | Meaning |
| --- | --- |
| `OPEN` | The incident is in the current detection result. |
| `RESOLVED` | The identity was in a previously observed result and is absent from the current one. |

Phase 20 incidents are derived from the observed dataset. Current detections are
`OPEN`. `RESOLVED` is a **reconciliation state** available when comparing a
previously observed incident set with a newer detection result; **Phase 20 does
not persist incident history**. A stateless `GET /incidents` therefore returns
only `OPEN` incidents — it has nothing earlier to compare against, and inventing
a lifecycle it does not track would be a lie about the system.

Reconciliation is the pure function `incidents.reconcile_incidents(previous,
current)`: current incidents stay `OPEN`, previous identities missing from the
current set become `RESOLVED`, ordering stays canonical (open first), and the
same inputs always produce the same output. It stores nothing, mutates nothing,
executes nothing, and changes no policy. RecoveryOS deliberately implements no
acknowledgement, assignment, escalation or incident workflow.

Listing order is simulated revenue at risk descending, then degradation
descending, then incident id ascending — a total order fixed by the data.

## Where the recovery evidence comes from

The durable pipeline records **execution**, not per-event recovery. Recovery in
RecoveryOS is produced by the controlled evaluation, so Phase 20 reads it from
the existing Phase 19 replay engine, run over the persisted events under the
**active** policy. Only the observed result crosses into detection — a boolean
and an integer amount per event. `true_probability`, `true_EV`, oracle values
and hidden-world parameters are never read, and `EvaluatedOutcome` has no field
one could travel in. Events whose replay failed produce no outcome and are
excluded from the denominators rather than counted as misses.

## API

| Endpoint | Purpose |
| --- | --- |
| `GET /incidents` | every currently detected incident, with the detector configuration, the windows and the evaluation identity |
| `GET /incidents/{id}` | one incident's complete evidence |
| `GET /incidents/{id}/events` | the affected payments, each with a `trace_path` into the existing `GET /events/{id}/trace` |
| `POST /incidents/{id}/replay` | the Phase 19 Policy Lab, run over exactly the affected subset |

The events endpoint returns the locked `PaymentEvent` contract plus a pointer to
the existing decision trace; it deliberately does not restate diagnosis, policy,
optimizer or execution detail, because RecoveryOS has exactly one Event Decision
Trace.

## Incident replay

`POST /incidents/{id}/replay` resolves the affected ids to their existing
`PaymentEvent` records and hands that subset to the frozen Phase 19
`replay_scenarios`, which already accepts an explicit event set; the comparison
comes from Phase 19's `compare_replays`, including its fairness checks. No
replay logic is reimplemented and no Phase 19 semantics were changed.

Phase 20 adds one thing: a deterministic `incident_replay_id` over the incident,
its sorted affected event ids and the compared policy fingerprints, because the
canonical replay id identifies a scenario against a *configuration* and cannot
distinguish one incident's subset from another's. Every underlying result still
carries its own canonical Phase 19 id.

## Safety

- **No execution.** No Phase 20 module imports the Razorpay client or any HTTP
  client; replay runs entirely through the benchmark's offline simulator. No
  Payment Link, no customer-facing action, no provider call.
- **No mutation.** Detection and replay write nothing — not to the database, not
  to the active policy, not to the benchmark records. Verified by comparing
  persisted state before and after, and by scanning the modules' executable
  tokens for write operations.
- **No ground-truth leakage.** Asserted structurally against the modules'
  executable code and against every API response.
- **No nondeterminism.** No `datetime.now`, no `uuid`, no `random` in any Phase
  20 module; identity and windows come from the data.

## Honest limitations

- **The evidence is simulated.** Recovery rates come from a controlled synthetic
  evaluation against a hidden model of the world, not from production Razorpay
  recovery. "Simulated revenue at risk" is one step further removed: a modelled
  estimate obtained by applying the observed recovery-rate gap to the current
  window's payment value. It is **not** merchant loss, provider loss, production
  revenue, or confirmed recoverable money.
- **The dataset is synthetic.** The analysed workload is the deterministic
  generator's output, whose events are drawn independently of time, so a
  detected degradation is a real property of that dataset rather than evidence
  of a real-world bank or rail problem. RecoveryOS never claims an outage.
- **A window comparison is not causal.** A segment can degrade because of sample
  composition rather than because anything changed operationally. The detector
  reports what it observed and names the leading *observed contributor*; it does
  not diagnose.
- **Both windows must contain data.** With fewer than 28 days of events on
  either side of the anchor, segments fail the sample gate and the screen
  honestly reports no incidents rather than lowering the bar. Populate a
  workload that spans the generator's full window (for example
  `python -m app.populate --seed 42 --count 500`) before expecting incidents.
- **The unrecovered rate is not independent evidence.** It is the exact
  complement of the recovery rate and moves by construction.
- **The affected batch is a selected sample, and the incident replay inherits
  that.** Affected payments are exactly the ones that stayed unrecovered under
  the active policy, so the reference arm starts at zero simulated recovered
  revenue on that batch *by construction*. The comparison therefore answers
  "would an alternative policy have recovered any of these?", which is the
  operational question, and **not** "which policy is better overall" — that is
  what the Policy Lab's full-workload comparison is for. A run in which every
  arm recovers nothing is an honest result on this batch, not a broken replay.

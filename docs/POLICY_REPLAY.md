# Policy Replay & the What-If Decision Lab (Phase 19)

> **Policy replay results are controlled simulated evaluations and are not production revenue forecasts.**
>
> **The replay path never performs real Razorpay execution.**

## 1. What policy replay is

Policy replay answers exactly one question:

> *What would RecoveryOS have done, on the exact same workload, if the safety/control policy had been different?*

It takes the canonical Phase 17 event set, runs it through the **real** RecoveryOS decision
pipeline — the same classifier output, the same candidate recommendations, the same
deterministic policy engine, the same Phase 18 economic optimizer, the same hidden outcome
model, the same seed — and changes **one** variable: the policy configuration. Everything
that differs in the result is therefore attributable to the policy.

```
Event set (fixed)
    ↓
Policy scenario  ← the ONLY experimental variable
    ↓
Existing classification
    ↓
Existing candidate interventions
    ↓
Existing policy engine, given the scenario's configuration
    ↓
Existing Phase 18 economic optimizer
    ↓
Simulated execution
    ↓
Existing hidden outcome evaluator
    ↓
Replay metrics
```

## 2. Why it exists

Phase 18 made the economic decision auditable: an operator can see *why* RecoveryOS chose
`retry_delayed` for a given event. But the policy bounds that constrain those choices — how
many times a customer may be contacted in 24 hours, how long to wait between events, how much
may be spent in a day — were untestable. Changing them meant changing production and finding
out afterwards.

Replay makes that trade-off measurable before it is taken. The pitch is not "the AI recommends
things"; it is "you can safely test how changing the control policy changes recovery,
intervention volume, cost and safety, using the exact same workload."

## 3. How scenarios work

A **policy scenario** (`app/policy_scenario.py`) is a validated `PolicyConfig` plus identity:
an id, a name, a human-readable derivation, and a `policy_fingerprint` computed by
deterministic serialization. It is *data*, not a code path — there is no
`if scenario == "aggressive"` anywhere in the decision logic. The scenario is injected into the
existing `PolicyEngine`, which is unchanged.

Four scenarios exist:

| Scenario | Max interventions / 24h | Event cooldown | Daily spend cap | Derivation |
|---|---|---|---|---|
| **Current** | 2 | 30 min | ₹50,000 | The shipped RecoveryOS policy defaults, read unchanged from `app/config.py` |
| **Conservative** | 1 | 60 min | ₹25,000 | Current made 2× less permissive: `limit // 2`, `cooldown * 2`, `cap // 2` |
| **Aggressive** | 4 | 15 min | ₹100,000 | Current made 2× more permissive: `limit * 2`, `cooldown // 2`, `cap * 2` |
| **Custom** | operator-defined | operator-defined | operator-defined | Validated server-side against fixed bounds |

No value here is invented. Current reads the actual shipped defaults; Conservative and
Aggressive apply a single documented factor to those defaults, so if the defaults ever change,
the derived scenarios move with them and stay reproducible.

## 4. Which parameters are configurable

Only the three that correspond to real, existing knobs on `PolicyConfig`:

- `max_interventions_per_customer_24h` — bounds `1 … 10`
- `event_cooldown_minutes` — bounds `1 … 1440`
- `daily_spend_cap_paise` — bounds `0 … 100,000,000`

Nothing was added to the policy engine to make replay possible. The engine already accepted an
explicit `PolicyConfig`; replay supplies one.

## 5. Which protections are immutable

Three rules are unconditional and have **no setting at all**:

- `fraud_protection` — a fraud-suspect event is never intervened on
- `terminal_failure` — a terminal failure is never intervened on
- `duplicate_intervention` — a customer with a successful intervention is never re-executed

These are not "locked in the UI." They are absent from the configuration surface entirely, and
a custom scenario that mentions them is rejected at the API boundary with a 422. An
"aggressive" policy means *more permissive bounded thresholds*, never *less safety*. The
comparison output asserts, per run, that these rules still fired.

## 6. How replay uses the existing policy engine

`app/replay.py` calls the same `PolicyEngine` the production `execute_event` calls, with the
scenario's `PolicyConfig`. Policy rules are not duplicated in the replay layer. A test inspects
the module to prove replay holds a real engine instance and passes it the scenario config.

### Why replay keeps its own policy history

Phase 17 evaluates each event as an *independent* decision problem and hands the engine an
empty `PolicyHistory`. That is correct for comparing decision **engines** — it removes event
order as a confound — but it also means the limit, cooldown, duplicate and spend-cap rules can
never fire, and those are precisely the rules a scenario configures. Replaying scenarios
through that harness unchanged would produce byte-identical results for every scenario: a lab
incapable of showing a difference.

Replay therefore accumulates history **across** events, in memory, exactly as production does:

- history is derived with the same four facts and the same rolling-24h semantics as
  `db.get_policy_history`, computed over attempts this replay itself performed;
- each event is evaluated at **its own timestamp**, so the 24h window and cooldown mean what
  they say rather than collapsing onto one frozen instant;
- events are processed in a canonical order fixed by the data (`timestamp`, then `event_id`),
  so accumulation is a pure function of the event *set*, not of argument order.

Nothing is written to the database. Replay cannot touch `intervention_attempts`, so it cannot
change what the real policy engine would decide about the next real payment.

## 7. How replay uses the Phase 18 optimizer

Replay calls `select_for_strategy` — the same entry point `execution_service` uses. The
optimizer is unchanged and receives an `AllowedCandidates` set, which by construction can only
contain interventions the policy engine authorized. Policy-denied candidates cannot reach the
optimizer; this is enforced by the type, and a test asserts it directly by capturing the
optimizer's input.

The ordering **policy → optimizer** is preserved. It is never reversed for replay.

## 8. How replay preserves benchmark fairness

Replay reuses `Phase17BenchmarkConfig` and replaces *only* the `policy_config` field
(`dataclasses.replace`). Every other parameter — event count, event seed, outcome seed,
methodology id, economic model — is shared by construction rather than by convention.

A comparison additionally *verifies* fairness rather than asserting it, and reports the result
in the API payload:

- identical event set and event ids across scenarios
- identical hidden-world identity
- identical classification source
- every execution simulated
- zero unauthorized attempts
- immutable protections held in every scenario

If any check fails the comparison reports it instead of quietly producing a misleading number.

## 9. How classifications are reused

Classifications are produced once per comparison by the deterministic
`DeterministicClassifier` and shared across all scenarios via `build_replay_contexts`. No LLM
is called during replay, and no scenario re-classifies. This is both a fairness requirement
(classification must not be a confound) and a performance one — 500 events × N scenarios of LLM
calls would be slow, costly and nondeterministic.

## 10. How hidden ground truth is isolated

The hidden world is consulted **once per event, after** the decision is made and the simulated
execution has run. No hidden probability reaches the classifier, policy engine, optimizer or
executor.

More strongly: `ReplayEventRecord` does not carry hidden probabilities *at all*, so there is
nothing for the API to leak. Whether an attempt counts as "successful" for policy-history
purposes is read from the simulated **execution status**, never from whether money came back —
letting ground truth feed the duplicate rule would leak the benchmark's answer into the system
under test.

## 11. How simulated outcomes are calculated

Outcome realization is the existing Phase 17 mechanism: `deterministic_draw_bps` keyed to
stable event identity and the benchmark seed, compared against the hidden world's true
probability. It is therefore keyed to *what the event is*, not to *when it was evaluated*, so
the same event receives the same underlying realization under every scenario regardless of
which scenario ran first.

All money is integer paise throughout. There is no floating-point financial arithmetic.

## 12. How replay differs from production execution

| | Production | Replay |
|---|---|---|
| Executor | `BoundedExecutor` | `SimulatedExecutor` |
| Razorpay | Real Test Mode calls | Never |
| Payment Links | Created | Never |
| Policy history | `intervention_attempts` table | In-memory ledger |
| Persistence | Writes decisions and outcomes | Writes nothing |
| Audit records | `optimizer_decisions`, `execution_outcomes` | Separate in-memory `ReplayEventRecord` |
| Outcome | Real webhook | Synthetic hidden world |
| Labelling | Actual recovered revenue | **Simulated** recovered revenue |

Replay results are computed on demand and are not persisted — no new tables were added. Storage
is unnecessary for reproducibility because a run is a deterministic function of (event set,
scenario, seed), and each result carries a `replay_id` that names exactly that function:

```
recoveryos-replay:phase19-policy-replay-v1:scenario=current
  :policy=<policy fingerprint>:config=<benchmark config fingerprint>
```

alongside the benchmark methodology, seeds, randomization version and classification source.
Anyone holding that identity can reconstruct the run. It reuses the Phase 17 fingerprint
infrastructure rather than introducing a second benchmark identity system, and uses
deterministic serialization — never Python's `hash()`.

Historical production audit records and canonical benchmark records are never touched.

## 13. How replay avoids Razorpay

Structurally, not by a mode flag:

- replay uses `SimulatedExecutor`, which has no provider dependency (verified by AST inspection
  of its imports, not by searching prose);
- `app/replay.py` does not import the Razorpay client, the production executor or the execution
  service — also verified by AST inspection of its imports;
- tests assert a Razorpay client call count of zero, zero Payment Link creations, that no
  credential is read, and that `build_razorpay_client` is never invoked;
- tests assert `db.connect_database` and `insert_intervention_attempt` are never called.

## 14. How results are compared

One scenario is the reference (default: Current). For every other scenario the comparison
reports absolute metrics, incremental metrics against the reference, and **decision deltas** —
the specific events where the two scenarios decided differently, keyed by stable event id.

Metrics reported per scenario:

- **Financial** — simulated recovered revenue, recoverable revenue, unrecovered revenue,
  recovery rate, incremental recovery vs reference
- **Intervention** — total, per customer, by type, efficiency, simulated spend
- **Safety** — total blocked, blocks broken out per rule (fraud, terminal, duplicate,
  intervention limit, cooldown, spend cap), fully blocked events, unauthorized attempts, and
  fraud/terminal interventions (which must always be zero)
- **Operational** — `processed`, `failures`, and `failures_by_category` covering
  `classification_failure`, `policy_failure`, `selection_failure`, `simulation_failure` and
  `replay_failure`

Failures are explicit results, never silently converted into `recovered_revenue = 0`. The
distinction between *"nothing was recovered"* and *"the evaluation failed"* is preserved, per
the Phase 17 failure-accounting philosophy.

## 15. Limitations

These are stated plainly because the lab is only useful if its numbers are trusted.

- **The workload is synthetic.** The canonical 500-event set and the hidden world are the
  Phase 17 benchmark's, not production traffic. Replay measures how a policy behaves *in that
  world*.
- **Simulated, not forecast.** These are controlled evaluation results. They are not a
  prediction of production rupees.
- **Not every configurable rule is load-bearing on this workload.** On the canonical dataset
  with the default economic model, `intervention_cost_paise` is zero for all interventions, so
  the **spend cap never binds**. Same-customer event clustering within 24h is sparse (15 of 500
  events have a prior same-customer event within the window), so the **cooldown rarely binds**
  and the **intervention limit** produces a small number of deltas. The comparison surfaces a
  "which rules were load bearing" table showing actual block counts per rule, so an operator
  can see that a knob did nothing rather than being led to believe it mattered.
- **Consequently, Conservative and Aggressive can produce identical or near-identical
  headline revenue** on this workload. That is an honest property of the dataset, not a bug,
  and the lab reports it rather than manufacturing a difference.
- **Classification is deterministic, not the live LLM.** Replay deliberately uses the
  benchmark's deterministic classifier. It therefore does not capture how classifier
  variability interacts with policy.

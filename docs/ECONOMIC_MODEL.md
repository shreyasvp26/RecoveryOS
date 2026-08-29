# RecoveryOS V2 Economic Decision Engine (Phase 16)

Phase 16 replaces V1's fixed-priority intervention selector with a deterministic
economic decision engine. It answers one engineering question:

> Given a set of interventions that the deterministic policy engine has already
> declared safe, can RecoveryOS choose the intervention with the highest expected
> economic value using only information legitimately available at decision time?

## Three statements that govern how this document should be read

1. **V2 probability estimates are deterministic model estimates, not empirically
   validated production recovery probabilities.** No coefficient in the estimator
   was measured against real payment outcomes.
2. **V2 intervention costs and friction are RecoveryOS controlled evaluation
   assumptions**, not Razorpay commercial pricing and not observed customer
   behaviour.
3. **Phase 16 does not establish improved recovery performance.** It builds and
   verifies the decision engine. Demonstrating that V2 recovers more money
   requires a signal-bearing hidden outcome model, which is Phase 17 work.

## V1 selection vs V2 selection

**V1 (`app/selector.py`, frozen and still present).** Intersect the classifier's
candidates with those carrying an authoritative ALLOW, then take the highest
entry in a fixed list:

```
retry_delayed > payment_link > reminder > alternate_method_prompt > retry_immediate
```

No expected value, no cost model, no ranking by predicted recovery.

**V2 (`app/optimizer.py`).** Same authorized set, but ranked by expected economic
value. The V1 ordering survives only as a tie-breaker.

## The decision flow

```
Payment Event
      ↓
AI Diagnosis (advisory)
      ↓
Candidate Interventions
      ↓
Deterministic Policy Gate          ← authoritative; unchanged from V1
      ↓
Allowed Candidates                 ← AllowedCandidates
      ↓
Recovery Probability Estimator     ← RecoveryProbabilityEstimator
      ↓
Economic Scoring                   ← evaluate_candidate
      ↓
Deterministic Best Candidate       ← EconomicInterventionOptimizer
      ↓
Existing Executor                  ← unchanged from V1
      ↓
Outcome → Audit
```

The ordering is non-negotiable: **policy runs before optimization.** The
optimizer is a decision layer, never an authorization layer.

## The policy / optimizer safety boundary

The controlling invariant is:

```
optimizer_decision_set  ⊆  policy_allowed_candidates
```

This is enforced **structurally, not by convention**. The optimizer's `select`
method accepts only an `AllowedCandidates` value, and `AllowedCandidates` can
only be built by `from_policy_decisions(candidates, decisions)`, which derives
the allowed set itself from authoritative `PolicyDecision` objects. There is no
code path that accepts a bare list of interventions, so a denied candidate
cannot be smuggled in. Passing a plain list raises `OptimizerError`.

`from_policy_decisions` excludes a candidate when it is denied, when it has no
decision at all (absence of a decision is not permission), and when it is
`no_action`. A decision whose `proposed_intervention` does not match the
candidate it is filed under is malformed input and stops the decision.

The optimizer reads exactly one field of a decision: `allowed`. It never reads
`denial_reason` and knows nothing about fraud protection, terminal-failure
protection, duplicate protection, cooldowns, retry limits, customer intervention
limits, or spend caps. Those rules live in `app/policy.py` and are not
duplicated, re-checked, or overridden anywhere in the decision engine.

**A policy-denied action with enormous expected value remains unavailable.** If
`payment_link` is denied and `retry_delayed` is allowed, the optimizer returns
`retry_delayed` even when `payment_link` would be worth 500× more.

## Probability representation

Probabilities are **integer basis points** on `[0, 10000]`, where `10000 bps`
is a probability of 1.0. Integer basis points keep every downstream monetary
calculation exact and reproducible; binary floating point is never used for
money.

`RecoveryProbability` validates its input and **rejects** anything outside the
domain or of the wrong type with `InvalidProbabilityError`. Out-of-domain values
are never clamped: an estimator returning `p > 1` is broken, and silently
repairing it would produce an economic decision from invalid state.

Distinct from that, the estimator's internal additive score **saturates** at both
endpoints. That is defined model behaviour rather than silent repair: the score
is a sum of bounded coefficients, so an extreme feature combination can
legitimately push the total past an endpoint, and 0 or 1 is the correct reading.

## The estimator

`RecoveryProbabilityEstimator.estimate(event, classification, intervention)` is a
pure function returning `P(recovery | event, intervention)`:

```
probability_bps = saturate(
      base(intervention)
    + root_cause_adjustment(intervention)
    + failure_reason_adjustment(intervention)
    + payment_method_adjustment(intervention)
    + customer_history_adjustment(intervention)
)
```

It requires no LLM, no network, no benchmark, and no hidden ground truth, and it
is independently unit-testable.

### Observable features used

All of these exist in the locked domain contracts today; none were invented.

| Feature | Source | Modelled effect |
| --- | --- | --- |
| `root_cause_category` | `ClassificationResult` | A transient fault rewards re-attempting; a fault needing the customer to act rewards reaching the customer; fraud and terminal suppress everything |
| `failure_reason` | `PaymentEvent` | The causal core: a bank timeout punishes immediate retry and rewards delayed retry; an expired card suppresses retries and rewards a different instrument; insufficient funds rewards a nudge |
| `payment_method` | `PaymentEvent` | Rail-specific retryability and the value of offering an alternative |
| `customer_history.prior_successful_payments` | `PaymentEvent` | A long successful history raises every recovery path |
| `customer_history.prior_failed_payments` | `PaymentEvent` | Repeated recent failures lower every recovery path |
| `customer_history.has_active_subscription` | `PaymentEvent` | A stored mandate lets an automated re-attempt succeed with no customer involvement |

### Features deliberately NOT used

| Feature | Why not |
| --- | --- |
| `bank` | The repository holds no bank-reliability data, so any bank coefficient would be fabricated |
| `amount_paise` | Value enters the decision through the expected-value calculation; using it in the probability too would double-count it |
| `confidence` | The classifier's self-reported confidence is a non-deterministic LLM output; depending on it would make estimates non-reproducible |
| `event_id`, `timestamp`, `order_id`, `payment_id`, `customer_id` | Identifiers carry no recovery signal, and an id-keyed lookup is the exact shape a ground-truth leak would take |

`failure_reason` has no finite taxonomy in the locked contract, so an
**unrecognized value contributes zero** rather than being guessed at.

## Cost model

Per-intervention direct cost of performing the action once, in integer paise.
These are RecoveryOS controlled evaluation assumptions.

| Intervention | `cost_paise` | Rationale |
| --- | --- | --- |
| `retry_immediate` | 0 | API-only re-attempt |
| `retry_delayed` | 0 | API-only re-attempt |
| `reminder` | 20 | Notional ₹0.20 messaging cost |
| `alternate_method_prompt` | 20 | Notional ₹0.20 messaging cost |
| `payment_link` | 100 | Notional ₹1.00 to create and deliver a hosted link |

`EconomicModel` construction fails unless the model prices **exactly** the
executable interventions: a missing entry would make an intervention silently
free, and an extra entry would price something that cannot be executed.
`no_action` is never priced, because it is not executable. Negative costs are
rejected at construction, and an unknown intervention raises
`UnsupportedInterventionError` rather than defaulting to zero.

This is separate from `PolicyConfig.intervention_cost_paise`, which feeds the
spend-cap rule and is unchanged.

## Friction model

Friction is **modelled customer-experience cost, not observed customer
behaviour.** It is expressed in basis points and converted to money as a
proportion of the transaction value, on the assumption that the relationship
cost of pestering a customer scales with the size of the transaction being
pursued:

```
friction_cost_paise = amount_paise × friction_bps // 10000
```

| Intervention | `friction_bps` | Rationale |
| --- | --- | --- |
| `retry_immediate` | 0 | Invisible to the customer |
| `retry_delayed` | 0 | Invisible to the customer |
| `reminder` | 5 | A passive nudge |
| `payment_link` | 10 | Requires the customer to complete a checkout |
| `alternate_method_prompt` | 15 | Requires the customer to change payment instrument |

Friction is therefore never an unexplained bare number: the conversion to a
monetary deduction is explicit, documented, and tested.

## The expected-value equation

For every policy-allowed candidate:

```
expected_recovered_value = estimated_recovery_probability × payment_amount
expected_value           = expected_recovered_value
                         - intervention_cost
                         - friction_cost
```

Select `argmax(expected_value)`. Expected value **may be negative**: an action
that costs more than it is expected to recover is a real and meaningful result,
not an error.

## Monetary arithmetic and rounding

Money is integer paise everywhere, reusing the existing `amount_paise` /
`cost_paise` conventions. No second money representation was introduced.

Every paise-valued product uses **floor division on non-negative integers**:

```
expected_recovered_value_paise = amount_paise × probability_bps // 10000
friction_cost_paise            = amount_paise × friction_bps    // 10000
```

Floor division is exact, deterministic, and independent of platform floating
point. It is also conservative: it never over-states expected recovery and never
under-states friction. Python's `round()` is deliberately not used anywhere, and
the decision engine contains no float literals at all.

Worked example — ₹1,000.00 (100000 paise) at 3000 bps via `payment_link`:

```
recovered = 100000 × 3000 // 10000 = 30000
friction  = 100000 ×   10 // 10000 =   100
cost      =                            100
EV        = 30000 - 100 - 100      = 29800
```

## Tie-breaking

Candidates are ranked by the total ordering
`(-expected_value_paise, v1_priority_index, intervention_name)`:

1. **Primary** — highest `expected_value_paise`.
2. **Secondary** — the V1 fixed-priority ordering, imported directly from
   `app/selector.py` so the ordering has exactly one authoritative definition.
3. **Final** — alphabetical intervention name, so the ordering stays total and
   stable even if the priority table ever grew a duplicate.

The V1 ordering is **only** a tie-breaker; one paise of expected-value
difference is enough to decide without ever consulting it.

No randomness, timestamps, UUIDs, hashing, or input ordering participates. The
sort key depends only on each evaluation's own content, so **any permutation of
the same candidate set produces the identical decision**.

## Failure and no-action behaviour

`no_action` is not an executable intervention, and V1 semantics are preserved: it
is never executed, never priced, and never scored.

A **controlled no-action result** is returned when there are no candidates
(`selection_reason = no_candidates`) or when policy left no allowed candidate
(`selection_reason = no_allowed_candidate`). In both cases `evaluations` is empty
and the existing `STATUS_NO_ACTION` path is taken — no new executor path exists.

An **explicit error** is raised, and no decision is produced, when the estimator
returns something that is not a `RecoveryProbability`, when a probability is
outside `[0, 10000]`, when the economic model is incomplete or malformed, when a
monetary value is negative or non-integer, when a candidate is outside the locked
taxonomy or duplicated, when a decision is forged or bound to a different
intervention, or when the event and classification disagree.

There is **no silent exception handling anywhere in the decision engine** — a
test asserts the modules contain no exception handler at all — and no silent
fallback from V2 to V1.

## Benchmark isolation

The decision engine is completely independent of benchmark ground truth, and
`app/outcome_model.py` is byte-for-byte unchanged by Phase 16.

`app/economics.py`, `app/estimator.py`, and `app/optimizer.py` import only
`dataclasses`, `typing`, and the sibling modules `classification`, `models`,
`policy`, and `selector`. Verified by `tests/test_optimizer_isolation.py`, which
checks the **transitive** app-module import closure and asserts the decision
engine cannot reach `benchmark`, `benchmark_metrics`, `benchmark_store`,
`outcome`, or `outcome_model`; cannot reach `executor`, `execution_service`,
`razorpay_client`, or `db`; cannot reach the `classifier`; imports no `random`,
`socket`, `http`, `urllib`, `httpx`, `time`, `uuid`, `os`, or `sqlite3`; contains
no `event_id`-keyed subscript; and contains no float literals.

The optimizer selects. The existing executor executes. There is no
`optimizer → executor` path.

## Benchmark arm: why the RecoveryOS arm is pinned to V1

The benchmark's RecoveryOS arm runs `execute_event(..., selection_strategy=
SELECTION_V1_FIXED_PRIORITY)` and therefore still measures the recorded V1
baseline exactly (seed 42 / 500 events reproduces 266,939,600 / 271,854,300 /
264,715,100 unchanged).

This is deliberate. The benchmark harness configures **no Razorpay client**, so
`payment_link` has no execution path there and can only produce a controlled
`REAL_RAZORPAY` / `FAILED` / `configuration_missing` outcome. V1's fixed priority
happened to mask this, because `retry_delayed` outranked `payment_link` and was
almost always an offered candidate. Economic selection does not mask it: at seed
42 / 80 events the optimizer chose `payment_link` for 21 of 29 attempts, and the
outcome simulator would have credited simulated recovery to executions that
provably never ran.

That is a limitation of the current harness, not of the optimizer's decision
theory, and repairing it means changing how the benchmark models executability —
explicitly Phase 17 work. Pinning the arm keeps the V1 baseline honest and
reproducible in the meantime.

`selection_strategy` affects **ranking only**. It cannot widen the authorized
set: both strategies consume the same authoritative policy decisions, and a
fraud event is inert under either.

## Known limitations

- **No empirical validation.** Every estimator coefficient, cost, and friction
  value is a modelled assumption. None is fitted, measured, or validated against
  real recovery outcomes, and none was tuned against the benchmark's hidden
  labels.
- **The current benchmark cannot evaluate this work.** `generate_hidden_outcome_model`
  draws independent uniform probabilities per (event, intervention) pair, so
  hidden recovery is uncorrelated with every observable feature the estimator
  reads. Under that model no targeting strategy can outperform any other, so the
  existing benchmark can neither confirm nor refute the optimizer. Phase 17
  introduces a signal-bearing model.
- **No V2 benchmark arm exists yet**, by design (see above).
- **Friction proportionality is an assumption.** Modelling friction as a fixed
  proportion of transaction value is defensible but unmeasured; real friction may
  not scale linearly with amount.
- **The estimator is intentionally coarse.** It is an additive score model with a
  handful of interpretable terms, chosen for transparency and testability over
  accuracy. There is no learning, fitting, or online adaptation.
- **`payment_link` is the only intervention with a real provider path.** The
  other four are `SIMULATED`, so their modelled costs and probabilities cannot be
  reconciled against provider behaviour.

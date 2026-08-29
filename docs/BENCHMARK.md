# RecoveryOS Benchmark Methodology

> **All batch recovery figures in this document are simulated evaluation results produced by RecoveryOS's controlled synthetic benchmark. They are not claims of production Razorpay recovery performance.**

RecoveryOS has **two** benchmark methodologies. They use **different hidden worlds**, so their numbers are **not comparable to each other** and must never be mixed in a single claim.

| Methodology | Module | Hidden world | Status |
| --- | --- | --- | --- |
| `phase9_v1_compat` | `app/benchmark.py` | `app/outcome_model.py` — independent uniform probabilities | **Frozen.** Preserved for historical reproducibility of the V1 baseline. |
| `phase17_signal_bearing_v1` | `app/benchmark_phase17.py` | `app/hidden_world.py` — feature-driven causal probabilities | **Current.** The V2 validation experiment. |

Every report identifies which methodology produced it, and the methodology name is part of the run id and of the configuration fingerprint. A future change to a frozen parameter must publish under a **new** methodology name rather than silently making old numbers incomparable.

---

# Part 1 — Phase 9 compatibility benchmark (frozen)

This is the original three-strategy benchmark. It is **unchanged by Phase 17** and remains reproducible via `python -m app.benchmark --seed 42 --count 500`. Its methodology and its disclosed limitation are preserved below exactly as recorded, and its numbers have not been rewritten.

## Purpose

The benchmark measures whether RecoveryOS recovers more *simulated* revenue than baseline approaches under identical conditions. It proves value by comparison, not by claiming absolute revenue.

## Baselines

The benchmark compares RecoveryOS against two baselines over the **same 500-event set** (Phase 9 canonical: `BENCHMARK_EVENT_COUNT = 500`, seed 42):

- **No Action** — the control: nothing is attempted. Establishes the natural (zero-intervention) outcome. Every event is valued at its modeled `no_action` baseline.
- **Naive Retry** — `retry_immediate` on every eligible **non-fraud** event (`risk_flag != "fraud_suspect"`); fraud events are skipped. Naive Retry has no AI, no policy, and no selector, so its retries are modeled directly by the outcome simulator and it never fabricates a policy authorization. Skipped fraud events and any event with no retry are valued at the modeled `no_action` baseline (uniform "do nothing" rule).
- **RecoveryOS** — the full real pipeline: advisory classification → deterministic policy gate → deterministic selection → bounded execution, run through the existing frozen modules against an isolated in-memory SQLite database. The selector is pinned to V1 fixed priority. Recovery is simulated only after execution was already determined.

Because all three run against the **same event set** and the **same hidden outcome model**, differences in outcome are attributable to the strategy, not the data.

## Outcome model

- Unit of evaluation: a chosen intervention on a specific event.
- **Deterministic**: a per-event `random.Random(f"{seed}:{event_id}")` draws each intervention probability from an explicit integer seed.
- **Event-specific**: every event receives its own probability for every locked intervention (including `no_action`).
- **Hidden**: evaluation-owned. The classifier, policy gate, selector, executor, and Razorpay boundary never receive, see, or act on it.
- Every probability satisfies `0 <= p <= 1`; an invalid or missing value fails explicitly (never clamped, never defaulted).

Outcomes for any `(seed, event, intervention)` triple are drawn from their own `random.Random(f"{seed}:{event_id}:{intervention}")`, so results are **independent of evaluation order, strategy order, and prior simulations**. The harness accepts an explicit strategy `order` and the integrity tests assert order-invariance.

## Disclosed limitation — the Phase 9 outcome model carries no signal

This is the most important caveat about interpreting a Phase 9 result, and it is a property of that hidden model by construction.

Recovery probabilities are drawn as **independent uniform values** — `rng.random()` per (event, intervention) pair. They are therefore **uncorrelated with every event feature** and **uncorrelated across interventions**. Every intervention on every event consequently has an expected recovery probability of ≈0.5, and two conclusions follow:

1. **The Phase 9 benchmark cannot reward intelligent targeting.** Since no intervention is truly better for any event, no classifier or selector — however good — can beat a blanket strategy on expected recovery. The only lever that moves simulated recovered revenue is *how many* events a strategy intervenes on.
2. **The canonical seed-42 Phase 9 result reflects exactly this.** No Action recovers 242/500 events, Naive Retry 246/500, and RecoveryOS 241/500 — statistically flat at the ~0.5 the model dictates. Naive Retry's marginal edge comes from attempting more interventions (240) than RecoveryOS (159), not from choosing better ones.

**What the Phase 9 benchmark does establish:** harness integrity — fairness over a shared event set and shared hidden model, order-invariant determinism, strict isolation of ground truth from the decision path, honest accounting (`processed + skipped + exceptions == event_count`), and a demonstrably non-rigged comparison that reports RecoveryOS losing.

**What it cannot establish:** that RecoveryOS's targeting recovers more revenue than a naive blanket retry. That is precisely the gap Phase 17 exists to close.

## Phase 9 metrics

All metrics are pure functions over the collected per-event records (`app/benchmark_metrics.py`): simulated recovered revenue; recovery rate (`recovered_events / event_count`); intervention count; recovery efficiency (`None` when zero interventions); incremental over No Action; RecoveryOS vs Naive Retry; fraud intervention rate (`None` when no fraud events). The **false-intervention rate is NOT computed** in Phase 9 and reports `METRIC DEFINITION AMBIGUITY`, because Phase 9 defined no canonical threshold. Phase 17 defines one; see below.

---

# Part 2 — Phase 17 signal-bearing benchmark

## 1. Purpose

Phase 16 built the economic decision engine (V2). Phase 17 builds the experimental environment capable of honestly determining whether that engine actually improves decisions.

> Given the same events, the same policy configuration, the same hidden world, the same execution assumptions and deterministic evaluation, does V2 economically outperform V1 and the naive baselines?

The harness must be able to answer **V2 wins**, **V2 loses**, or **V2 is not yet better**. All three are valid outcomes and the benchmark was never tuned after observing a result.

## 2. Event generation and observable features

Events come from the existing `app/generator.py` at `seed = 42`, `count = 500` — the same generator, seed and size as Phase 9, so dataset scale stays comparable. Nothing about the generator changed.

The observable domain features are: `failure_reason`, `payment_method`, `amount_paise`, `risk_flag`, `bank`, and `customer_history` (`prior_successful_payments`, `prior_failed_payments`, `has_active_subscription`).

## 3. The hidden world (`app/hidden_world.py`)

`P_true(recovery | event, intervention)` is an integer basis-point score, saturated into `[0, 10000]`:

```
score = base(intervention)
      + failure_reason  x intervention
      + payment_method  x intervention
      + customer_history
      + subscription    x intervention
      + amount_band     x intervention
```

### Frozen coefficients

**`base(intervention)`** — `no_action` 500, `retry_immediate` 1500, `retry_delayed` 2600, `payment_link` 2400, `reminder` 1700, `alternate_method_prompt` 1900.

**`failure_reason x intervention`** (bps), the primary source of signal:

| failure_reason | no_action | retry_immediate | retry_delayed | payment_link | reminder | alternate |
| --- | --- | --- | --- | --- | --- | --- |
| `bank_timeout` | +300 | −900 | **+2200** | +200 | 0 | +700 |
| `network_issue` | +200 | −500 | **+1800** | +100 | 0 | +400 |
| `insufficient_funds` | −100 | −1300 | +1100 | +500 | **+1400** | +200 |
| `authentication_failed` | −200 | −800 | −300 | **+1500** | +400 | +1200 |
| `expired_card` | −300 | −1400 | −1300 | +1800 | −200 | **+2000** |
| `declined_by_bank` | −200 | −900 | −400 | +600 | 0 | **+1300** |
| `transaction_declined` | −400 | −4500 | −4500 | −4500 | −4500 | −4500 |
| `payment_failed` | −400 | −4500 | −4500 | −4500 | −4500 | −4500 |

**`payment_method x intervention`** (bps): `upi` retry_immediate +400, retry_delayed +500, payment_link −100, alternate +200. `card` retry_delayed −100, payment_link +400, alternate +300. `netbanking` retry_immediate −200, retry_delayed +300, payment_link +200. `wallet` retry_immediate +100, reminder +300, alternate +500.

**`customer_history`** (uniform across interventions): `prior_successful_payments >= 15` → +500, `>= 5` → +250, `== 0` → −350. `prior_failed_payments >= 4` → −700, `>= 2` → −250.

**`subscription`** (when `has_active_subscription`): no_action +300, retry_immediate +500, retry_delayed +700, payment_link −200, reminder +200, alternate 0.

**`amount_band`** (when `amount_paise >= 1_000_000`): no_action −100, payment_link +300, reminder +200, alternate +200.

### Why this world is signal-bearing

The correct action genuinely **differs by event class**: a bank outage rewards waiting, a dead card rewards a new instrument, an empty account rewards a nudge, and a terminal refusal rewards nothing. No single intervention is globally best, so a benchmark over this world tests **targeting** rather than enthusiasm. `test_hidden_world.py` asserts each of these qualitative properties directly.

### Probability from features, never from identity

There is no `probabilities[event_id]` mapping anywhere in the module — the exact design Phase 17 moves away from. `event_id`, `order_id`, `payment_id`, `customer_id`, `timestamp` and `bank` never enter the probability; two events with identical observable features necessarily have identical hidden probabilities, and reversing the event list changes nothing. Asserted by AST inspection, not by convention.

### `no_action` semantics

`no_action` has a **real, non-zero natural recovery process** (`base` 500 bps, adjusted by the same feature terms). A failed payment is not a closed door: some customers notice and re-pay unprompted. Modelling the control as a genuine baseline is what lets the benchmark express "intervening was not worth it"; a zero control would make every intervention look free. `no_action` is never executed, is never priced, and its true EV is simply its passive recovery value. The same baseline applies uniformly to **every** arm on **every** event where it attempts nothing.

### Fraud

The world assigns fraud events the **same** probabilities as equivalent non-fraud events. Fraud is kept inert by the **policy gate**, not by ground truth. Encoding "fraud is unrecoverable" into the world would make the safety result trivially true for the wrong reason.

## 4. RecoveryOS's belief vs the world's truth — why this is not circular

Two distinct, independently defined functions:

| | RecoveryOS estimator (`app/estimator.py`) | Hidden world (`app/hidden_world.py`) |
| --- | --- | --- |
| Meaning | `P_hat(recovery \| event, intervention)` — the system's belief at decision time | `P_true(recovery \| event, intervention)` — synthetic ground truth |
| Owner | production decision path | evaluation layer only |
| Root cause | uses the classifier's advisory `root_cause_category` | derives its own effects directly from `failure_reason` |
| Amount | **deliberately unused** (value enters V2 only through the EV multiplication) | **used** (high-value band interaction) |
| Reliability bands | 10 / 3 / 0 successes, 5 / 3 failures | 15 / 5 / 0 successes, 4 / 2 failures |
| Coefficients | Phase 16, independently authored | Phase 17, independently authored |

The estimator was **not** fitted to the world and the world was **not** copied from the estimator. `test_hidden_world.py` proves they disagree on many `(event, intervention)` pairs **and** that they disagree about the *ranking* of interventions on some events — being wrong about levels is cheap, being wrong about order costs money. The benchmark can therefore reveal good estimates, bad estimates, wrong rankings, unnecessary actions, missed opportunities, and regret.

## 5. Strategy arms

| Arm | Decision |
| --- | --- |
| **A. No Action** | Control. Nothing is ever attempted. |
| **B. Naive Retry** | `retry_immediate` on every non-fraud event. No AI, no policy gate, no economics, no ground truth. Phase 9 eligibility, unchanged. |
| **C. RecoveryOS V1** | classifier → policy → `selector.select_intervention` (frozen priority: `retry_delayed`, `payment_link`, `reminder`, `alternate_method_prompt`, `retry_immediate`). |
| **D. RecoveryOS V2** | classifier → policy → Phase 16 `EconomicInterventionOptimizer` + `RecoveryProbabilityEstimator`. |
| **E. Oracle** | **Evaluation only.** Reads hidden truth, respects the identical policy boundary, picks the highest true-EV allowed option. |

V1 and V2 run through the **real production selection code** (`execution_service.select_for_strategy`), not a benchmark reimplementation. Neither is given hidden probabilities.

Naive Retry deliberately has **no policy gate** — that is what makes it naive. Its attempts are permanently recorded as `authorized = False`, and because it acts outside the policy boundary the policy-bounded Oracle is **not** an upper bound for it. Its regret is therefore reported as `None`, not as a misleading number.

## 6. The Oracle

Option set: every policy-**ALLOWED** executable intervention, plus `no_action` (always available; the honest floor for "the best thing to do here may be nothing").

Tie-break, total and deterministic: highest true EV → `no_action` ahead of any action at an exact tie (spending money to achieve the same modelled value is not better) → V1 priority order → alphabetical name.

The Oracle is **not** a RecoveryOS strategy and is never importable by the optimizer, estimator, classifier, policy, executor, production API, or the dashboard decision path. `test_phase17_isolation.py` asserts this structurally.

## 7. Policy boundary

Both V1 and V2 use the **same authoritative `PolicyEngine`**, and so does the Oracle. The optimizer can never widen the candidate set: `AllowedCandidates` can only be built from genuine ALLOW `PolicyDecision` objects. A `payment_link` worth ₹10,000 that policy denies cannot be selected by V1, V2 **or** the Oracle.

**Cross-event policy state — an explicit design decision.** Phase 9 ran RecoveryOS through the database-backed `execute_event`, so each intervention became history for the next event and the duplicate / per-customer-limit / cooldown / spend-cap rules fired according to event processing order. That makes results depend on event order, which Phase 17 must not do. **Phase 17 therefore evaluates each event as an independent decision problem**, giving the real policy engine an empty `PolicyHistory` per event.

The consequence is a genuine limitation and is stated plainly: within a Phase 17 run the **fraud** and **terminal** rules are load bearing, while the **duplicate, per-customer-limit, cooldown and spend-cap** rules never fire. Phase 9 keeps its sequential behaviour unchanged for historical comparability.

## 8. Simulated execution (`app/benchmark_simulation.py`)

Every executable intervention — `retry_immediate`, `retry_delayed`, `reminder`, `alternate_method_prompt`, **and `payment_link`** — is simulatable with **no Razorpay credential and no network call**. `execution_mode` is structurally pinned to `SIMULATED` for every benchmark execution.

**The payment-link repair.** Before Phase 17 a credential-less batch run recorded every `payment_link` as `configuration_missing`/FAILED, which silently deleted an entire intervention from the comparison and pinned the benchmark to V1. Phase 17 fixes this with a benchmark-owned simulator rather than by teaching the production executor to pretend. The production `BoundedExecutor` still couples `payment_link` to `REAL_RAZORPAY` and still returns `configuration_missing` without a client — asserted by regression test. The live path is untouched.

**Real vs simulated boundary:** batch benchmark → `SIMULATED`; live demo → `REAL_RAZORPAY`. Simulated benchmark recovery is never reported as real Razorpay recovery.

**Execution is not recovery.** A simulated `SUCCESS` only means the action was performed. Whether money came back is decided afterwards and independently by the hidden world:
`selection → simulated execution → hidden outcome model → recovery realization`.

## 9. Randomization and the common randomness contract

```
draw_bps = blake2b("phase17-blake2b-uniform-v1|{seed}|{event_id}|{intervention}|{replication}") mod 10000
recovered = draw_bps < P_true_bps
```

The draw is a **pure function of that key**. No shared mutable RNG stream, no `random.seed`, no wall clock, no UUID, no network. Two arms that pick the same action on the same event see the **same coin**, and an arm that runs first cannot consume a draw a later arm needed. Reversing the events, reordering the arms, or replaying one event in isolation all produce byte-identical outcomes.

`RANDOMIZATION_VERSION` is part of the key and part of the frozen configuration: changing it changes every realized outcome, so it is versioned rather than edited.

## 10. Frozen parameters (`app/benchmark_config.py`)

One serializable `Phase17BenchmarkConfig` holds every parameter that can move a number: `methodology`, `event_count`, `event_seed`, `outcome_seed`, `hidden_model_seed`, `replication`, `evaluation_time`, `evaluation_mode`, `randomization_version`, `false_intervention_rule`, the full `policy_config`, the full `economic_model`, and an `estimator_fingerprint` digest of V2's coefficient tables. `fingerprint()` digests the whole thing; two runs with the same fingerprint must produce identical results.

`hidden_model_seed` is recorded but **deliberately unused**: the Phase 17 world is a pure function of event features and needs no randomness to define its probabilities. Only the Bernoulli realization is seeded, via `outcome_seed`. Keeping the field makes that auditable.

The policy configuration is pinned to the shipped defaults rather than read from the environment — a benchmark whose safety configuration depends on the shell it was launched from is not reproducible.

**Frozen before results:** hidden model structure and coefficients, estimator coefficients, event seed and count, outcome seed, intervention costs, friction, policy configuration, false-intervention threshold, the randomization contract, canonical event ordering, and the verdict rule. None was altered after observing whether V2 wins.

## 11. False-intervention threshold

> An attempted intervention is a **false intervention** when its true expected value is **strictly less** than the true expected value of doing nothing on that same event.

- **Unit:** paise (integer), compared per event.
- **Threshold value:** the event's own `true_EV(no_action)` — not a flat constant.
- **Rationale:** "false" should mean *the world says this action destroyed value relative to the available alternative of not acting*. A flat paise threshold would be arbitrary and would scale wrongly across a ₹50 and a ₹20,000 failure. The no-action baseline is derived from the methodology's own control arm, so it was frozen by the same act that froze the hidden world — it cannot have been chosen to flatter V2.
- **Denominator:** `interventions_attempted` by that strategy.
- **Edge cases:** zero attempts → `None`, never `0.0`. An attempt whose true EV exactly equals the baseline is **not** false (strict `<`), because breaking even is not a mistake.

## 12. Metric formulas (`app/benchmark_phase17_metrics.py`)

| Metric | Formula | Zero/undefined denominator |
| --- | --- | --- |
| A. Simulated recovered revenue | `sum(recovered_amount_paise)` | — |
| B. Incremental vs No Action | `strategy_recovered − no_action_recovered`; pct also reported | pct is `None` when the baseline recovered nothing |
| C. Incremental vs V1 | `v2_recovered − v1_recovered` | — |
| D. Intervention count | count of attempted interventions | — |
| E. Recovery efficiency | `recovered_paise / interventions_attempted` | `None` |
| F. False-intervention rate | false interventions / attempts (§11) | `None` |
| G. Negative-EV rate | `true_EV < 0` attempts / attempts | `None` |
| H. Optimal-selection rate | value-optimal decisions / decidable events | `None` |
| I. Economic regret | per event `oracle_true_EV − strategy_true_EV`; total, mean, median | `None` for policy-unbounded arms |
| J. Oracle value capture | gross `strategy_EV / oracle_EV`; incremental `(strategy_EV − noaction_EV) / (oracle_EV − noaction_EV)` | `None` unless the denominator is strictly positive |
| K. Fraud intervention rate | fraud interventions / fraud events | `None` when no fraud events |
| L. Unauthorized execution | attempts without an authoritative ALLOW | — |
| M. Exceptions | count by category | — |

**True EV** uses the **same** cost and friction assumptions as V2, so regret measures decision quality and not an accounting difference:

```
true_EV(event, intervention) = amount_paise * P_true_bps // 10000
                             - intervention_cost_paise
                             - amount_paise * friction_bps // 10000
```

Integer paise, floor division, everywhere.

**Decidable events (denominator for H and the honest reading of I):** events with at least one policy-allowed intervention and no exception. Where policy authorized nothing, every arm is forced to `no_action` and "did it choose well?" has no content.

**Ties (H):** optimality is measured by **value**, not by name — a choice is optimal when its true EV equals the Oracle's. Two actions the world values identically are equally optimal, and penalizing an arm for picking the other one would measure agreement with the Oracle's tie-break rule rather than decision quality. `oracle_choice_match_rate` reports the stricter name-identity version alongside it.

**Regret is a primary metric.** Optimal-selection rate alone cannot distinguish an arm that picks a ₹790 action when ₹800 was available from one that picks a ₹5 action. Total, average and median regret are reported next to it. **A negative regret fails the benchmark** with `BenchmarkIntegrityError` rather than being clamped: a policy-bounded arm cannot exceed the policy-bounded Oracle, so that condition means the harness is wrong.

**Value capture is reported twice.** The gross ratio includes the passive no-action value every arm inherits for free and is therefore generous by construction; the **incremental** figure is the honest one to quote and can legitimately be negative.

**Exceptions are never outcomes.** Categories: `classification_failure`, `policy_failure`, `selection_failure`, `simulation_failure`, `malformed_strategy_result`, `benchmark_configuration_failure`. A failed event never reports recovery.

## 13. Isolation

Hidden truth never enters `classifier.py`, `classification.py`, `policy.py`, `selector.py`, `estimator.py`, `economics.py`, `optimizer.py`, `executor.py` or `execution_service.py`, and the Oracle is unreachable from all of them. Hidden per-event probabilities, draws and true expected values live only on benchmark-only record types; they are never added to production API models, never persisted into the operational tables, and never served by the operator dashboard. `test_phase17_isolation.py` asserts all of this against the code (AST and transitive import graph), because behavioural evidence that the optimizer currently does not read ground truth is much weaker than proof that it structurally cannot.

The hidden world in turn cannot see the system under test: `P_true` takes no strategy argument, reads no run state, and `HiddenWorld` holds no mutable per-event state, so asking it a question can never change an answer.

## 14. Fairness, verified rather than asserted

Everything an arm cannot control is computed **once** per event, before any arm runs, and handed to every arm identically: the event, its classification, the authoritative policy decisions, the allowed set, and the Oracle's answer. There is only one of each and none is recomputed per arm, so two arms structurally cannot see different worlds, policies or cost models.

Every run reports computed checks: `strategy_order_invariant`, `event_order_invariant`, `deterministic_replay`, `same_event_set_for_every_arm`, `same_policy_boundary_for_every_arm`, `same_hidden_world_for_every_arm`. The adversarial tests additionally run V2 before V1, run the Oracle first and confirm the system under test is unmoved, reverse the event list, replay for byte equality, and confirm a different seed produces a different world.

## 15. Reproducibility

From `backend/`:

```
python -m app.benchmark_phase17                       # canonical: 500 events, seed 42
python -m app.benchmark_phase17 --seed 42 --count 500 # explicit
python -m app.benchmark_phase17 --json                # machine-readable only
python -m app.benchmark_store --seed 42 --count 500   # run + persist for the dashboard
python -m app.benchmark --seed 42 --count 500         # the frozen Phase 9 benchmark
python -m app.benchmark_store --phase9 --seed 42 --count 500
```

The default classifier is the project-owned deterministic `DeterministicClassifier` (advisory, decision-time inputs only). Any adapter satisfying the classifier Protocol may be injected, but such a run is model-dependent and explicitly **not** reproducible.

**Declared robustness seeds:** `42, 7, 1337, 2024, 31415`, fixed in `benchmark_config.ROBUSTNESS_SEEDS` **before** any result was observed, so no seed can be cherry-picked after the fact. Seed 42 is the single canonical headline.

## 16. Canonical result — seed 42, 500 events, `phase17_signal_bearing_v1`

Configuration fingerprint `788e79ad98728b0d713dda5c19da1d84`. **All figures SIMULATED.**

| Arm | Revenue | vs No Action | Attempts | Total true EV | Total regret | Optimal |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| No Action | ₹238,077 | ₹0 | 0 | ₹275,540.51 | ₹731,932.93 | 0.0% |
| Naive Retry | ₹245,603 | ₹7,526 | 240 | ₹307,321.65 | n/a | 0.0% |
| RecoveryOS V1 | ₹843,352 | ₹605,275 | 180 | ₹856,657.20 | ₹150,816.24 | 53.3% |
| RecoveryOS V2 | ₹920,207 | ₹682,130 | 180 | ₹956,663.92 | ₹50,809.52 | 57.2% |
| Oracle | ₹1,101,885 | ₹863,808 | 180 | ₹1,007,473.44 | ₹0 | 100.0% |

- **V2 − V1 (true EV): +₹100,006.72.** Materiality threshold ₹7,319.32 (100 bps of the ₹731,932.93 of decision value at stake). **Verdict: V2 WON.**
- V2 − V1 (realized revenue): **+₹76,855**.
- Incremental Oracle value capture: **V2 93.1%**, V1 79.4%, Naive Retry 4.3%.
- V1/V2 selection disagreements: **129 of 500** events.
- Intervention mix — V1: `retry_delayed` 180. V2: `payment_link` 129, `retry_delayed` 51. Oracle: `retry_delayed` 96, `payment_link` 52, `alternate_method_prompt` 32.
- Safety — V1 and V2 both: fraud intervention rate **0%**, unauthorized executions **0**, false-intervention rate **0%**, negative-EV rate **0%**, exceptions **0**. Naive Retry: 240 unauthorized attempts and a **29.6%** false-intervention rate, both measured from records.
- Fairness — all six checks **PASS**.

### Verdict rule (frozen before results)

The criterion is **total true economic value**, not realized revenue: realized revenue is a sum of 500 Bernoulli draws and moves on luck alone, whereas true EV is what the decision actually controlled. Materiality is **1% of `(Oracle total true EV − No Action total true EV)`** — the value genuinely at stake in the decisions. A difference below that is reported as a tie, not spun as a win. `test_benchmark_phase17.py` proves the same rule returns `V2 LOST` when fed an inverted comparison.

### Robustness seeds (all declared in advance, all reported)

500 events each, `event_seed = outcome_seed`. Every seed in `ROBUSTNESS_SEEDS` is listed; none was added or dropped after the fact.

| Seed | Verdict | V2 − V1 true EV | V2 − V1 realized revenue | V1 capture | V2 capture |
| ---: | --- | ---: | ---: | ---: | ---: |
| **42** (canonical) | V2 WON | +₹100,006.72 | +₹76,855 | 79.4% | 93.1% |
| 7 | V2 WON | +₹114,604.83 | +₹153,178 | 79.4% | 94.7% |
| 1337 | V2 WON | +₹118,257.40 | **−₹121,281** | 77.4% | 93.2% |
| 2024 | V2 WON | +₹137,916.92 | +₹89,810 | 78.1% | 94.5% |
| 31415 | V2 WON | +₹98,737.26 | +₹311,114 | 80.6% | 93.4% |

Fraud intervention rate 0%, unauthorized executions 0 and exceptions 0 for V1 and V2 on every seed.

**Seed 1337 is the most instructive row and is reported rather than buried.** V2 made materially better decisions (+₹118,257.40 of true EV, 93.2% vs 77.4% Oracle capture) and still recovered **₹121,281 less simulated revenue than V1**, purely through Bernoulli luck. Realized revenue over 500 coin flips is a noisy statistic; this is exactly why the frozen verdict criterion is true economic value, and it is also a warning against quoting a single realized-revenue delta as evidence of anything.

### Interpretation

V2's gain is a **targeting** gain, not an **effort** gain: both arms intervened on exactly the same 180 events. V1's frozen priority puts `retry_delayed` first, so it selects `retry_delayed` on every actionable event; V2 re-ranks by expected value and moves 129 of them to `payment_link`. On `expired_card`, `authentication_failed` and `declined_by_bank` events the world genuinely rewards a customer-facing path over a silent retry, and that is where the ₹100,006.72 comes from.

V2 is nonetheless **well short of optimal**: it captures 93.1% of the available decision value and carries ₹50,809.52 of residual regret. The mix shows why — V2 chooses `payment_link` 129 times where the Oracle chooses it only 52 times and prefers `alternate_method_prompt` 32 times. The estimator systematically under-rates `alternate_method_prompt`, and because it deliberately ignores `amount` while the world's high-value band rewards customer-facing actions on large failures, V2 also mis-ranks some large-ticket events. Both are honest, visible estimation errors and neither was corrected to improve the headline.

The naive baseline loses badly here for two compounding reasons: `retry_immediate` is a genuinely poor action in this world, and having no policy gate it burns 240 attempts including on terminal refusals, producing a 29.6% false-intervention rate.

## 17. Limitations

1. **The world is synthetic and authored.** Its coefficients express an interpretable causal story, not measured recovery rates. A different plausible world could rank the arms differently. The result says V2 targets better *in this world*, not that it will recover more money in production.
2. **The estimator and the world share a modelling vocabulary.** They were independently authored with different coefficients, bands and feature sets, and are proven to disagree on levels and on rankings — but both were written by the same project, so this is not a blind test against nature.
3. **Only two policy rules bind** (fraud, terminal). See §7.
4. **One Bernoulli replication.** Realized revenue carries sampling noise; this is exactly why the verdict criterion is true EV. The `replication` field exists for future averaging and is not yet exercised.
5. **The classifier is the deterministic controlled classifier**, not a live LLM. An LLM run would not be reproducible.
6. **No production validation.** Nothing here has been checked against real Razorpay recovery outcomes.

## 18. Reporting rule

Any revenue figure produced by either benchmark **must be labeled as a simulated evaluation result** and never presented as production Razorpay revenue. The batch benchmark and the live Razorpay Test Mode verification described in the README are entirely separate exercises and their numbers must never be combined.

# Recovery Intelligence — Outcome Feedback (Phase 22)

RecoveryOS could already decide *what to do about a failed payment*, execute it under a deterministic policy gate, and show whether the money came back for that one payment. What it could not answer was the question that decides whether any of it is worth trusting:

> RecoveryOS predicted a recovery probability. Across everything it actually did, how close was that prediction to what was observed?

Phase 22 answers that and stops there. It is a **measurement layer**: it observes, measures, calibrates and reports. It does not retrain, does not touch the estimator, does not change the optimizer, does not modify policy, and cannot execute anything. Adaptive estimation is a later phase; the Phase 22 boundary is deliberately `DECIDE → EXECUTE → OBSERVE → MEASURE`.

---

## What "observed feedback" means here

An observation is one **executed action** paired with the **prediction that drove it** and the **provider evidence about what happened next**. It is operational: it exists only where RecoveryOS actually contacted Razorpay and a real Payment Link was created. Nothing simulated and nothing from the benchmark can become an observation.

There is **no feedback table**. The feedback layer is a deterministic projection (`app/outcome_feedback.py`) over records the existing decision path already persists, aggregated by `app/recovery_intelligence.py`. A duplicated feedback lifecycle would be a second source of truth that could disagree with the authoritative records; a projection cannot.

| Role | Source | Phase |
| --- | --- | --- |
| Prediction | `optimizer_decisions` | 18 |
| Execution | `execution_outcomes` | 7 / 11 |
| Verified recovery | `webhook_recovery_outcomes` | 12 |

---

## Prediction source

The prediction is **read** from the persisted optimizer decision — specifically `evaluations[].estimated_probability_bps` for the intervention that was selected. It is never recomputed.

This matters more than it looks. A historical decision must be judged against the number RecoveryOS actually used at the time, not against the number a newer estimator would produce today. Recomputing would silently measure the current estimator against the past, which is not calibration.

The estimator is frozen in Phase 22. Nothing in this layer reads it, imports it, or writes to it.

---

## Prediction → execution → outcome join

The join is deterministic and uses authoritative identifiers only. Amount, customer, email, timestamp proximity, and event similarity are never used to correlate anything.

1. **Execution → prediction.** Among the persisted optimizer decisions for the same `event_id`, keep those whose `selected_intervention` equals the execution's intervention *and* whose `decided_at` is at or before the execution's `reported_at`; take the latest. An execution cannot have been driven by a decision made after it, and a decision that selected a different intervention did not drive this action. If no such decision exists, the observation is ineligible (`missing_prediction`) rather than paired with a guess.
2. **Execution → provider evidence.** Correlation is by `payment_link_id`, which is only ever persisted on a `REAL_RAZORPAY` `payment_link` SUCCESS outcome. This is the same key the Phase 12 webhook path already uses.
3. **Repeated interventions.** Each execution is its own observation, paired with its own decision. An event intervened on twice contributes two observations.

Ordering is total everywhere (reported time, then intervention; then event id), so the same records always produce the same observations in the same order.

---

## Outcome definitions

### RECOVERED

`REAL_RAZORPAY` execution + `SUCCESS` + a `payment_link_id` + a verified, correlated `webhook_recovery_outcomes` row for that exact link.

The recovered amount is the **trusted `amount_paid_paise` the provider reported**. The original event amount is never substituted. If the provider reported no amount, the amount is recorded as missing (`null`) and excluded from value averages — the binary recovery is still verified, so the observation still counts for calibration.

### PENDING

A real Payment Link was created successfully and no verified payment has been observed yet.

This means *the outcome has not been observed*. It is not a failure, it is not counted as a non-recovery, and it never enters a statistic.

### UNKNOWN

Evidence cannot establish success or failure. Currently produced for:

- an **ambiguous provider result** (the provider was called and returned nothing RecoveryOS could interpret — a link may exist);
- a **failed execution** (the attempt never got as far as requesting payment);
- a real success that recorded **no Payment Link id**, so nothing can be correlated;
- a **simulated execution**, where no provider observed anything at all.

Uncertainty is never converted into failure.

### NOT_RECOVERED

Defined in the contract, and **never inferred from current evidence**.

A failed execution is a failed *intervention*, not an observed payment failure. The only provider evidence RecoveryOS verifies today is `payment_link.paid`; there is no authoritative signal that a customer saw a live link and declined to pay. Manufacturing `NOT_RECOVERED` from execution failure or from link expiry we do not observe would corrupt every calibration number downstream, so it is left ineligible instead.

**This is the single most important limitation of Phase 22 and it is stated again below.**

---

## Eligibility

| Situation | Eligible? | Outcome | Reason code |
| --- | --- | --- | --- |
| Real Payment Link, verified paid | yes | `RECOVERED` | `eligible` |
| Real Payment Link, waiting for payment | no (not yet) | `PENDING` | `awaiting_outcome` |
| Ambiguous provider result | no | `UNKNOWN` | `ambiguous_provider_result` |
| Known execution failure | no | `UNKNOWN` | `execution_failed` |
| Real success without a Payment Link id | no | `UNKNOWN` | `missing_payment_link_id` |
| Simulated execution | no | `UNKNOWN` | `simulated_execution` |
| Verified recovery with no persisted prediction | no | `RECOVERED` | `missing_prediction` |
| Verified webhook that matched no execution | no observation exists | — | — |

Every ineligible observation carries exactly one reason, and the API reports the counts. Nothing is silently dropped, and the UI shows the breakdown.

---

## Calibration methodology

For each eligible observation: `predicted = the persisted probability in basis points`, `recovered = 1 or 0`.

```
mean_predicted_probability = mean(predicted over eligible observations)
observed_recovery_rate     = recovered_observations / eligible_observations
calibration_gap            = observed_recovery_rate - mean_predicted_probability
```

The gap is displayed in percentage points. **The sign is never reversed: a negative gap means observed recovery came in below prediction.** Predicted 70%, observed 65% is a gap of `-5 pp`.

Arithmetic is integer-safe: probabilities stay in the basis points the optimizer persisted, means are computed as exact rationals (`fractions.Fraction`) and rounded once for display, and the gap is computed from the exact difference rather than from the two rounded means — so a genuinely perfect calibration reports exactly `0` instead of a rounding artefact. Rounding is half-away-from-zero, which is sign-symmetric.

The mean predicted probability is computed over the **same population** as the observed rate (the eligible observations). Mixing populations would produce a gap between two different things.

---

## Minimum sample threshold

`MIN_OBSERVATIONS = 10`, defined once in `app/recovery_intelligence.py`, applied identically to the overall figure, every intervention row and every segment row, and tested at 0, 1, 9, 10 and above.

Below the threshold the API reports `status: INSUFFICIENT_OBSERVATIONS`, `observed_recovery_rate_bps: null` and `calibration_gap_bps: null`, and the UI renders **Insufficient observations**. No conclusion — "inaccurate", "underperforming", "overperforming" — is drawn.

The predicted probability may still be shown where eligible observations exist, because it is a model estimate and does not depend on sample size. Where there are no eligible observations at all there is nothing to average, and the predicted cell is empty rather than filled from a different population.

---

## Intervention metrics

Grouped by the intervention actually recorded on each execution, so no intervention list is hardcoded and an intervention that was never executed simply does not appear. Per intervention:

attempts · eligible observations · observed recoveries · observed recovery rate · mean predicted rate · calibration gap · average recovered amount · total recovered amount · average expected recovered value.

`attempts` counts every projected execution including ineligible ones; the statistics use only the eligible subset. Observations whose provider reported no amount are **excluded** from the amount averages, never counted as zero.

---

## Segment metrics

Only three clean, locked columns of the `PaymentEvent` contract are grouped: `payment_method`, `bank`, `failure_reason`. Each row reports the same predicted / observed / gap / samples set, with the threshold applied per segment.

This is deliberately not a general segmentation engine, a feature store, or a customer analytics platform.

---

## Expected vs realized value

Where an eligible verified recovery carries **both** a persisted `expected_recovered_value_paise` and a provider-reported amount, the two are summed and shown side by side.

This is **not profit** and **not revenue uplift**. It places a modelled estimate next to what the provider actually reported, for the recoveries where both figures exist. Observations missing either half are excluded, because a comparison missing one side is not a comparison.

---

## Benchmark separation

The three worlds stay separate and only the first produces observations:

| World | Outcome source | Feeds operational feedback? |
| --- | --- | --- |
| Operational | verified Razorpay webhook on a real Test Mode Payment Link | **yes** |
| Benchmark | hidden deterministic outcome model | never |
| Policy Lab | replay / simulation | never |

The hidden benchmark outcome model must not enter operational feedback under any circumstances. This is enforced structurally, not by convention: `tests/test_feedback_integrity.py` parses the Phase 22 source and asserts that none of `hidden_world`, `outcome_model`, `outcome`, the benchmark modules, the executor, the execution service, the webhook service, the policy engine, the estimator, the optimizer or the classifier is imported, and that no benchmark vocabulary appears in the source at all.

Benchmark ground truth is never used as an observed outcome, never used to calibrate the live estimator, and never displayed as operational recovery evidence.

---

## The simulated-world limitation

A `SIMULATED` intervention can execute successfully and produce **no operational payment outcome whatsoever**, because no provider was contacted and no money moved. Simulated executions are therefore structurally ineligible.

This is the reason the screen will legitimately read *Insufficient observations* on a demo dataset dominated by simulated executions. That is the honest answer. Fabricating a rate from simulated executions — or borrowing the benchmark's hidden outcomes to fill the gap — would be the single most damaging shortcut available here, and it is explicitly forbidden.

---

## Uncertainty handling

Explicitly handled: no outcome yet, failed execution, ambiguous provider result, missing webhook, unmatched webhook, duplicate webhook (deduplicated at the database level by `delivery_id`, yielding one logical recovery), missing recovered amount, late outcome (included deterministically whenever it arrives), multiple historical decisions, and repeated intervention attempts.

In every one of these cases the layer reports what the evidence supports and nothing more.

---

## Why Phase 22 does not modify the optimizer

The flow is:

```
observed outcomes → measurement → calibration evidence
```

and explicitly **not**:

```
observed outcomes → automatic estimator update → new decisions
```

An automatic feedback loop into the estimator would mean money-moving decisions changing based on a handful of observations, with no human reading the evidence and no way to explain after the fact why a decision was made the way it was. It would also break the audit property that makes the decision trail defensible: a persisted decision must be reproducible from the model that produced it.

There is no feedback → estimator path, no feedback → optimizer path, no feedback → policy path, and no feedback → execution path. The Recovery Intelligence API exposes `GET` only; the router carries no `POST`, `PUT`, `PATCH` or `DELETE`, and tests assert it.

---

## API

```
GET /recovery-intelligence[?include_observations=true]
```

Read-only, deterministic, derived entirely from persisted records. Returns `calibration`, `interventions`, `segments`, `expected_vs_realized`, an `evidence` block with the ineligibility breakdown, and a `methodology` block naming the authoritative sources. `include_observations` attaches the per-observation rows so any aggregate can be traced back to the exact event, decision, execution and provider evidence it came from.

No metric is hardcoded anywhere in the backend or the frontend.

---

## Terminology

This document says **observed performance**, **prediction calibration**, **outcome feedback** and **recovery evidence**. It does not say *the AI learned*, *the model retrained*, or *adaptive optimizer* — those describe a system RecoveryOS does not have.

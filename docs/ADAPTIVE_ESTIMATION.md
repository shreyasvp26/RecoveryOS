# Adaptive Recovery Estimation (Phase 23)

Phase 23 evolves the frozen Phase 16 economic decision chain from a *static
additive score* into **evidence-calibrated economics**: the V2 optimizer keeps
ranking by expected value, but the recovery probabilities it ranks with are now
periodically corrected by a deterministic, versioned calibration snapshot built
from real operational outcomes. This document states the boundary precisely: what
calibration is, what evidence can enter it, how a snapshot becomes *active*, and
the invariants that keep it from ever becoming an authority.

## The shape

```
frozen baseline estimator (Phase 16)
        │
        ▼
calibration evidence (REAL_RAZORPAY terminal outcomes)
        │
        ▼
versioned, immutable snapshot  (v1, v2, …)
        │
        ▼
adaptive estimator = baseline ⊕ active snapshot   →   RecoveryProbability
```

The `CalibratedRecoveryProbabilityEstimator` (`app/adaptive_estimation.py`)
wraps the frozen `RecoveryProbabilityEstimator`. For an intervention with an
**active** posterior in the snapshot it returns that posterior; otherwise it
returns the frozen baseline estimate. Its public contract is unchanged:
`estimate(event, classification, intervention) -> RecoveryProbability`.

The decision chain consumes the wrapper the same way it consumed the baseline
(the optimizer only ever calls `.estimate()`), so:

- Selection is additive and minimal. `select_for_strategy` and `execute_event`
  accept an optional `estimator` that **defaults to the frozen baseline**, so the
  V1/V2 benchmark and Policy Replay arms reproduce their recorded results exactly.
  Only the production execute endpoint constructs the calibrated wrapper, and only
  when an active gated snapshot exists.
- **Policy is still the authorization boundary.** The estimator only changes the
  *probability* used to rank; `optimizer_decision_set ⊆ policy_allowed_candidates`
  is untouched. The estimator authorizes nothing and executes nothing.

## Evidence: the terminal contract

Calibration is **intervention-level only** and fed exclusively by real,
operator-side provider evidence on `REAL_RAZORPAY` `payment_link` executions:

| Provider status | Calibration outcome | Sample? |
|-----------------|---------------------|---------|
| `paid`          | `RECOVERED`         | yes     |
| `expired`       | `NOT_RECOVERED`     | yes     |
| `created` / `partially_paid` | `PENDING` | no |
| `cancelled`, provider failure, timeout, failed execution | `UNKNOWN` | no |

The mapping lives in one place: `calibration.map_provider_status`. Hard rules:

- **Never negative from absence.** Only a provider-confirmed `expired` settles a
  link as `NOT_RECOVERED`; `cancelled` and any unreadable provider result are
  `UNKNOWN`, **never** a negative sample.
- **Structural ineligibility.** SIMULATED executions, the benchmark, the Policy
  Lab, replay and the hidden ground-truth world cannot reach a calibration sample.
  Only `REAL_RAZORPAY` `payment_link` `SUCCESS` executions with a persisted
  Payment Link id are projected (`calibration_service._executed_links`).
- **Positive evidence is authoritative webhook recovery.** A verified Phase 12
  webhook recovery is `RECOVERED` and that link is never re-polled.
- **Provider poll fills the gap.** A link with no verified webhook recovery is
  resolved by a read-only `razorpay_client.get_payment_link`; a terminal result is
  persisted once into `provider_payment_link_outcomes` (deduped by link id as
  PRIMARY KEY) and a PENDING/UNKNOWN result is never persisted.
- **Never borrow across interventions.** An intervention's samples are only ever
  its own.

## The gate

A snapshot row is only **active** (i.e. allowed to change the probabilities that
rank decisions) when an intervention meets **every** threshold with its own
terminal evidence:

```
observed_total     >= MIN_TOTAL_OBSERVATIONS  (10)
observed_recovered >= MIN_POSITIVE            (1)
observed_not_rec   >= MIN_NEGATIVE            (1)
```

An intervention that does not meet the gate keeps its frozen baseline probability
and `STATUS_BASELINE`. If no intervention is gated, the snapshot's `active_bps`
is empty and production keeps the frozen baseline unchanged.

## Arithmetic (integer-exact)

Probabilities remain integer basis points on `[0, PROBABILITY_SCALE]`. The
posterior is a Beta-binomial update against a baseline-derived prior of strength
`PRIOR_STRENGTH`, computed entirely with integer floor division:

```
prior_successes_i = floor(baseline_bps_i * PRIOR_STRENGTH / PROBABILITY_SCALE)
prior_failures_i  = PRIOR_STRENGTH - prior_successes_i
posterior_bps_i   = (recovered_i + prior_successes_i) * PROBABILITY_SCALE
                    // (total_i + PRIOR_STRENGTH)
```

Money stays integer paise; no binary float is ever introduced for money or
probability.

## Immutable, versioned snapshots

- Each build appends exactly **one** row to `estimator_calibration_snapshots`,
  with `version` as PRIMARY KEY (1, 2, …). A duplicate version is rejected.
- `active_bps_json` holds the gated posteriors; `evidenced_json` holds the
  baseline + prior + observed counts for every intervention.
- History is **never rewritten**: there is no update/delete path, and a past
  snapshot (and the historical decisions it preceded) stays reconstructable.
- An operator triggers a build explicitly via `POST /estimator-evidence/recalibrate`;
  nothing recalibrates on its own.

## Provenance

The wrapper's `provenance(intervention)` reports, read-only, which source produced
a probability (snapshot version + evidence counts, or `BASELINE`). It is surfaced
by `GET /estimator-evidence` and the **Estimator Evidence** frontend screen.
Historical `optimizer_decisions` are never rewritten; provenance is display data
for new decisions and for understanding which snapshot produced a given estimate.

## Boundaries

- `app/estimation` is estimator-only: it executes nothing, authorizes nothing, and
  imports no executor, policy engine, optimizer, webhook boundary, classifier, or
  benchmark. `app/calibration_service` depends only on persistence reads/writes of
  immutable snapshot/evidence rows and the pure calibration module.
- Integrity tests (`tests/test_estimator_evidence_integrity.py`) forbid the
  calibration modules from importing any authority or benchmark vocabulary, mirror
  the Phase 22 assertion that measurement never acquires authority, and confirm the
  modules expose no `execute`/`authorize` surface.

## Limitations (stated honestly)

- Calibration is **not** a learned model and has **no hidden ground truth**. It is
  a transparent, deterministic correction over observed terminal outcomes.
- A posterior is only trustworthy after the gate is met; below it the baseline is
  all RecoveryOS honestly knows.
- Provider polling observes only what Razorpay reports. A link the provider cannot
  read, or has `cancelled`, is `UNKNOWN` and contributes nothing.
- Calibration is intervention-level only; it does not currently correct for
  event/segment features inside an intervention.

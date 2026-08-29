# Recovery Operations Center (Phase 21)

RecoveryOS could already answer *what should we do about this failed payment?* and *where is recovery performance degrading?* What it could not answer was the question an operator actually asks at the start of a shift:

> Which failed payments need attention right now, what does RecoveryOS recommend, is it allowed, did we do it, and did the money actually come back?

Phase 21 answers that without inventing a new pipeline. The Recovery Operations Center is a **projection over the decisions RecoveryOS already makes and already persists**, plus one narrow safety primitive that closes a genuine concurrency gap.

---

## The queue is a read model

Every field on a queue row is derived from records the existing decision path writes:

| Source table | What it contributes |
| --- | --- |
| `payment_events` | the failed payment: amount, customer, method, bank, failure reason, risk flag |
| `classification_results` | the advisory AI diagnosis, its confidence and the candidate interventions (Phase 5) |
| `policy_decisions` | the authoritative ALLOW/DENY per candidate and the denial rule (Phase 6) |
| `optimizer_decisions` | the selected intervention, its reason and the optimizer's own expected value (Phase 18) |
| `execution_outcomes` | what was executed, in which mode, and the Payment Link id (Phase 7/11) |
| `webhook_recovery_outcomes` | verified recovery: the trusted `amount_paid` on a paid link (Phase 12) |

There is **no `recovery_queue` table** and no second lifecycle store. `build_queue_row` is a pure function of those records, so a row cannot drift from the authoritative state, and the projection can be tested without a database.

Only the most recent policy evaluation drives a row's policy state; the full history stays visible in the Event Decision Trace, which the queue links to rather than duplicating.

---

## Derived states

`lifecycle_state` collapses the persisted evidence into one operational label. Strongest evidence wins: what happened outranks what was decided, and what was decided outranks what was recommended.

| State | Means | Derived from |
| --- | --- | --- |
| `NOT_CLASSIFIED` | never diagnosed, so nothing downstream exists | no classification record |
| `RECOMMENDED` | diagnosed, the policy gate has not run | classification, no policy decision |
| `POLICY_ALLOWED` | the gate authorized at least one candidate | ≥1 ALLOW in the latest evaluation |
| `SELECTED` | the optimizer chose an actionable intervention | optimizer decision ≠ `no_action` |
| `BLOCKED` | every candidate was refused | all decisions in the latest evaluation are DENY |
| `EXECUTED` | a `SIMULATED` intervention ran successfully | successful non-`REAL_RAZORPAY` execution |
| `PENDING_OUTCOME` | a real Payment Link exists and is waiting for payment | successful `REAL_RAZORPAY` execution, no verified recovery |
| `RECOVERED` | a verified webhook confirmed the link was paid | correlated `webhook_recovery_outcomes` row |
| `FAILED` | the execution attempt itself failed | most recent execution has `status = FAILED` |

`actionable` marks the rows where the authoritative state leaves room to act. It is a UI affordance derived from persisted evidence, **not an authorization**: the server re-derives policy on every execute regardless of what the row said.

---

## Two rules the screen must never blur

**Execution is not recovery.** A successful `POST /recovery/{id}/execute` for `payment_link` means a real Razorpay Test Mode Payment Link was created. Nobody has paid anything. The row reads `PENDING_OUTCOME` and the UI reads "Waiting for payment" until the Phase 12 webhook path verifies the signature, correlates the delivery to that exact `payment_link_id`, and persists a recovery. Only then is the row `RECOVERED`, and the amount shown is the **trusted `amount_paid` the provider reported**, never the original event amount.

**Simulated is not real.** `retry_immediate`, `retry_delayed`, `reminder` and `alternate_method_prompt` execute in `SIMULATED` mode: no provider is contacted, so there is no payment outcome to observe. Those rows reach `EXECUTED` and stop, always carry a `SIMULATED` badge, and never report a recovered amount. `payment_link` remains the only intervention that touches Razorpay, and only in Test Mode.

---

## The operator execution path

```
Operator presses Execute
  -> POST /recovery/{event_id}/execute   (empty body)
  -> execution_service.execute_event     (the SAME function the Phase 7 endpoint calls)
       -> load event + persisted classification
       -> deterministic policy gate, per candidate      (authoritative)
       -> economic optimizer over the authorized set    (authoritative)
       -> persist the optimizer decision                (audit before action)
       -> durable execution claim                       (concurrency boundary)
       -> bounded executor                              (the only side effect)
       -> persist outcome + intervention attempt
  -> the freshly projected queue row is returned
```

The route reimplements none of those steps. The operator therefore chooses only **whether** to act on an event — never what is done, whether it is permitted, or when it is evaluated.

A request carrying `intervention`, `selected_intervention`, `allowed`, `policy_decision`, `authorization`, `authorized`, `evaluation_time`, `execution_mode` or `force` is refused with `422 client_authority_rejected` and nothing executes. Refusing loudly is deliberate: silently ignoring the field would let a caller believe its value mattered.

---

## The concurrency boundary

The Phase 6 duplicate rule reads persisted intervention history, which protects **sequential** duplicates completely. It cannot protect concurrent ones: two requests that both read the history before either writes its attempt are both genuinely policy-authorized, and for `payment_link` that means two real Payment Links for one failed payment.

Phase 21 adds `execution_claims`, keyed `PRIMARY KEY (event_id, intervention)`:

- The claim is taken **immediately before** the executor and is the last thing before the external side effect.
- The insert is a database-level compare-and-set, so exactly one concurrent caller proceeds. Everyone else is told `execution_in_progress`, `already_executed`, or `provider_result_unknown` with HTTP 409, and nothing is written.
- A **successful** execution resolves the claim to `completed`, which is defense in depth behind the policy duplicate rule.
- A **failed** execution releases the claim, so Phase 11 retry-after-failure semantics are unchanged.
- If the provider was called but the result could not be confirmed or persisted, the claim is parked as `provider_result_unknown` and **never retried automatically**. RecoveryOS would rather say it does not know than fabricate a `FAILED` it cannot substantiate or risk duplicating a real provider-side action.

This is a concurrency/idempotency primitive and nothing more. It stores no decision, grants no permission, and cannot make a denied candidate executable — a denied event never reaches the claim at all, because the gate runs first.

### Known limitation

The Phase 11 executor maps a provider timeout to an explicit `FAILED` outcome, because the Razorpay client boundary cannot distinguish "the request never landed" from "the response was lost". Phase 21 does not change that frozen behavior. `PROVIDER_RESULT_UNKNOWN` therefore covers uncertainty that arises **after** the executor returned (a crash or persistence failure between the side effect and its record), not a provider-side timeout. Widening it would require changing the Phase 11 outcome contract and is deliberately out of scope.

---

## API

### `GET /recovery/queue`

Filters: `lifecycle_state`, `execution_mode`, `risk_flag`, `failure_reason`, `intervention`, `policy_status`.
Sort orders: `newest` (default), `amount_desc`, `expected_recovery_desc`, `oldest_pending_outcome`.

Event-level filters are pushed into SQL; derived-state filters are applied to the projection, because a derived state is not a stored column. Sorting is total — `event_id` breaks every tie — so the same data always produces the same order. An unknown state or sort is rejected with 422 rather than silently ignored, so an operator never reads a filtered view that quietly did not filter.

The response reports `scanned`, `scan_limit` and `truncated_scan` alongside the rows, so a bounded read is never mistaken for the whole workload.

### `POST /recovery/{event_id}/execute`

Empty body. Returns the execution result plus the freshly projected row, so the operator sees the state the server actually recorded rather than an optimistic client-side guess.

| Situation | Response |
| --- | --- |
| executed | 200, `execution_success` / `execution_failed` |
| every candidate denied, or nothing economically worthwhile | 200, `no_action` (the row explains which) |
| another attempt is in flight, or this already ran | 409 |
| a previous attempt's provider result is unknown | 409, never auto-retried |
| the client supplied authority | 422, nothing executed |
| no classification | 422, nothing executed |
| unknown event | 404 |

---

## What Phase 21 does not do

- It does not detect degradation. Revenue Health remains the analytical layer; it now links across to operations, and the boundary is unchanged.
- It does not compare policies. The Policy Lab remains the only replay engine.
- It does not touch the benchmark. No hidden-world value, oracle option or realized benchmark figure can appear on a queue row, and a test asserts that.
- It does not give the LLM any authority. The diagnosis is displayed as advice; the deterministic gate still decides, and the optimizer still chooses only among what the gate authorized.

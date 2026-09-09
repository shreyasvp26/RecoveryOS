# RecoveryOS

### Autonomous Payment Recovery Infrastructure

RecoveryOS is a payment recovery system that turns failed payments into **controlled, explainable recovery actions**.

Instead of treating a failed payment as a terminal event, RecoveryOS builds a recovery plan around the failure, evaluates whether recovery is economically and operationally justified, executes the appropriate action, and records the complete outcome.

> **Payments fail. Recovery shouldn't.**

---

## Why RecoveryOS?

A failed payment creates more than a technical error.

It can mean:

- Lost revenue
- Unnecessary retry attempts
- Poor customer experience
- Duplicate recovery actions
- Wasted processing costs
- Difficult-to-audit payment decisions

Most systems stop at:

```text
payment failed → retry
```

RecoveryOS treats recovery as a **decision problem**:

```text
Payment Failure
      ↓
Understand the Failure
      ↓
Build Recovery Policy
      ↓
Evaluate Eligibility
      ↓
Check Economic Constraints
      ↓
Select Recovery Action
      ↓
Execute Safely
      ↓
Observe Outcome
      ↓
Record + Replay + Audit
```

The goal is not to retry everything.

The goal is to determine:

> **What is the safest and most economically rational next action?**

---

## What RecoveryOS Does

### 1. Failure Classification

RecoveryOS interprets payment failures using their available context rather than blindly triggering the same retry behavior.

### 2. Policy-Driven Recovery

Recovery behavior is determined by explicit policies and constraints rather than hard-coded retry loops.

### 3. Economic Guardrails

Recovery decisions account for the real economic cost of attempting recovery.

A recovery attempt is therefore not considered successful merely because it can technically be executed.

### 4. Idempotent Recovery

The system is designed so repeated delivery of the same payment event does not create duplicate recovery work.

### 5. Safe Webhook Ingestion

Webhook deliveries are handled with duplicate detection and payload consistency checks.

The same delivery cannot silently overwrite a previously accepted event with a different payload.

### 6. Concurrent-Safe Recovery

Recovery execution is protected against concurrent attempts for the same payment/recovery target.

### 7. Replayable Decisions

Recovery scenarios can be replayed to understand:

- What happened
- Why a decision was made
- Which policy was applied
- What action was selected
- What the resulting outcome was

### 8. Operator Controls

Sensitive operational APIs are protected by an operator authentication boundary, while intentionally public endpoints such as health checks and webhook ingestion remain separately handled.

### 9. Auditability

Recovery outcomes are persisted so the system can answer:

> **What happened, what did RecoveryOS do, and why?**

---

## Architecture

At a high level:

```text
                    ┌─────────────────────┐
                    │   Payment Provider  │
                    └──────────┬──────────┘
                               │
                               │ Webhook
                               ▼
                    ┌─────────────────────┐
                    │   Event Ingestion   │
                    │  Idempotency Check  │
                    └──────────┬──────────┘
                               │
                               ▼
                    ┌─────────────────────┐
                    │ Failure Interpretation│
                    └──────────┬──────────┘
                               │
                               ▼
                    ┌─────────────────────┐
                    │  Recovery Policy    │
                    │     Evaluation      │
                    └──────────┬──────────┘
                               │
                    ┌──────────┴──────────┐
                    │                     │
                    ▼                     ▼
             ┌─────────────┐       ┌─────────────┐
             │  Economic   │       │  Safety /   │
             │  Guardrails │       │  Eligibility│
             └──────┬──────┘       └──────┬──────┘
                    │                     │
                    └──────────┬──────────┘
                               ▼
                    ┌─────────────────────┐
                    │ Recovery Decision   │
                    └──────────┬──────────┘
                               │
                               ▼
                    ┌─────────────────────┐
                    │ Recovery Execution  │
                    └──────────┬──────────┘
                               │
                               ▼
                    ┌─────────────────────┐
                    │ Outcome + Audit Log │
                    └──────────┬──────────┘
                               │
                               ▼
                    ┌─────────────────────┐
                    │ Replay / Operations │
                    └─────────────────────┘
```

---

## Core Design Principles

### Recovery is a decision, not a retry loop

RecoveryOS separates:

```text
Failure Detection
       ↓
Decision Making
       ↓
Execution
       ↓
Observation
```

This makes recovery behavior explicit, testable, and auditable.

### Idempotency is a system property

Duplicate events are expected in distributed payment systems.

RecoveryOS therefore treats idempotency as part of the architecture rather than an afterthought.

For example:

```text
Webhook A
   ↓
Accepted

Webhook A again
   ↓
Already Processed
   ↓
No Duplicate Recovery
```

If the same delivery identifier arrives with a different payload, it is rejected rather than silently replacing the original event.

### Concurrency must be safe

A system that works correctly for sequential requests but fails under concurrent delivery is not reliable payment infrastructure.

RecoveryOS explicitly protects recovery outcomes against concurrent attempts:

```text
Request 1 ─────┐
               ├──→ One Recovery Outcome
Request 2 ─────┘
```

rather than:

```text
Request 1 ──→ Recovery
Request 2 ──→ Recovery
               ↓
        Duplicate Outcome
```

### Economic safety matters

Retries have costs.

RecoveryOS therefore models the economic impact of recovery actions rather than assuming:

```text
more retries = more recovered revenue
```

A recovery action must make sense within the configured economic constraints.

### Fail closed

Operationally sensitive functionality should not become accidentally public because configuration is missing.

When required authentication configuration is unavailable, protected operational endpoints fail closed rather than silently falling back to insecure behavior.

---

## Security & Reliability

RecoveryOS has been subjected to adversarial testing around failure modes that can create real production problems.

The hardening work covers:

- Operator authentication
- Fail-closed authentication configuration
- Duplicate webhook delivery
- Conflicting duplicate webhook payloads
- Concurrent webhook ingestion
- Concurrent recovery execution
- Idempotent recovery outcomes
- Webhook retry/500 loops
- Correct event observation timestamps
- Economic/spend constraints
- Replay correctness
- Regression coverage for discovered defects

The system is designed around the assumption that:

> **Events can be duplicated, requests can race, providers can retry, and failures can happen at every boundary.**

---

## Example Recovery Flow

A simplified recovery lifecycle looks like:

```text
payment.failed
      │
      ▼
Identify Payment + Failure Context
      │
      ▼
Check Whether Recovery Is Applicable
      │
      ▼
Evaluate Recovery Policy
      │
      ▼
Evaluate Economic Limits
      │
      ▼
Select Recovery Action
      │
      ▼
Execute Exactly Once
      │
      ▼
Observe Actual Outcome
      │
      ▼
Persist Recovery Result
```

A recovery attempt can therefore end in different controlled states rather than simply "retry succeeded" or "retry failed."

---

## Replayability

Recovery decisions should be explainable after the fact.

RecoveryOS supports replaying recovery scenarios using the relevant payment/event context and policy configuration.

This enables teams to investigate questions such as:

- Why was this recovery action selected?
- Why wasn't another action attempted?
- Did an economic constraint prevent recovery?
- What would happen if the same scenario were evaluated again?
- Did the system observe the real payment outcome correctly?

Replayability also provides a foundation for safely testing changes to recovery policies.

---

## Production Readiness

RecoveryOS went through multiple security and production-readiness passes rather than stopping at the happy-path implementation.

### Authentication

Protected operational routers require operator authorization, while public health and webhook boundaries remain intentionally separated.

### Economic Controls

Real recovery costs are wired into policy evaluation and replay scenarios.

### Payment Failure Handling

Payment failure processing uses the actual event observation context instead of fabricating timestamps.

### Webhook Reliability

Duplicate and conflicting webhook deliveries are handled safely without creating duplicate recovery work or entering retry loops.

### Concurrency

Race conditions around webhook ingestion and recovery outcomes are explicitly tested.

### Regression Testing

Adversarial findings are converted into regression tests so fixes remain protected.

---

## Project Structure

```text
RecoveryOS/
│
├── backend/
│   ├── app/
│   │   ├── auth.py
│   │   ├── config.py
│   │   ├── main.py
│   │   └── ...
│   │
│   └── tests/
│       ├── ...
│       └── test_phase25_adversarial.py
│
├── ...
│
└── README.md
```

The backend contains the recovery engine, API boundaries, persistence, payment/event handling, policy evaluation, and automated tests.

---

## Testing

The project includes unit, integration, and adversarial regression coverage around the recovery lifecycle.

Particular emphasis is placed on distributed-system failure modes:

```text
Duplicate Delivery
        +
Conflicting Payload
        +
Concurrent Requests
        +
Provider Retries
        +
Partial Failures
        ↓
Safe, Deterministic Behavior
```

Run the backend test suite using the project's configured test command.

---

## Tech Stack

### Backend

- Python
- FastAPI
- SQLAlchemy
- Pydantic

### Payments & Events

- Stripe-compatible payment event flows
- Webhook-based event ingestion

### Testing

- Pytest
- Adversarial and concurrency-focused regression tests

### Architecture

- API-driven backend
- Policy-based recovery engine
- Persistent recovery outcomes
- Idempotent event processing
- Replayable scenarios

---

## Running Locally

Clone the repository:

```bash
git clone https://github.com/shreyasvp26/RecoveryOS.git
cd RecoveryOS
```

Install backend dependencies using the project's dependency configuration.

Configure the required environment variables.

Then start the backend using the project's configured FastAPI/ASGI entrypoint.

See the backend configuration files for the environment variables required by your local setup.

---

## Design Tradeoffs

RecoveryOS intentionally favors **correctness and auditability over blind automation**.

That means the system may choose not to recover a payment when:

- The failure is not recoverable
- The recovery policy does not permit the action
- Economic constraints are violated
- The action has already been performed
- Required context is unavailable
- The system cannot safely establish that execution is valid

This is deliberate.

A payment recovery system should optimize for:

```text
Recovered Revenue
        −
Recovery Cost
        −
Operational Risk
```

—not simply the number of retry attempts.

---

## What Makes RecoveryOS Different?

RecoveryOS is not just:

- A webhook receiver
- A retry scheduler
- A payment dashboard
- A collection of API endpoints

The core idea is to treat payment recovery as an **autonomous, policy-constrained decision system**.

It combines:

```text
Payment Events
      +
Failure Context
      +
Recovery Policies
      +
Economic Constraints
      +
Idempotent Execution
      +
Concurrency Safety
      +
Outcome Observation
      +
Replayability
      +
Auditability
```

into one recovery lifecycle.

---

## Current Status

RecoveryOS has completed its core implementation and production-hardening cycle.

The fundamental recovery, safety, idempotency, economic, authentication, concurrency, and replay mechanisms have been implemented and adversarially tested.

The project can now be evolved primarily at the product and operational UX layer without compromising the underlying recovery guarantees.

---

## Philosophy

> **Don't just retry payments. Reason about recovery.**

RecoveryOS is built around a simple principle:

**When money moves through distributed systems, correctness is more valuable than optimism.**

A recovery system should know:

1. **What failed**
2. **Why it failed**
3. **Whether recovery is appropriate**
4. **What recovery costs**
5. **What action is safe**
6. **Whether that action has already happened**
7. **What actually happened afterward**
8. **Why the system made that decision**

That is RecoveryOS.

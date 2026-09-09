# RecoveryOS

### Autonomous Payment Recovery Infrastructure

[![Status](https://img.shields.io/badge/status-production--ready-success)](https://github.com/shreyasvp26/RecoveryOS)
[![Python](https://img.shields.io/badge/Python-3.11+-blue?logo=python&logoColor=white)](https://www.python.org/)
[![FastAPI](https://img.shields.io/badge/FastAPI-API-009688?logo=fastapi&logoColor=white)](https://fastapi.tiangolo.com/)
[![Tests](https://img.shields.io/badge/tests-adversarial%20%2B%20regression-informational)](https://github.com/shreyasvp26/RecoveryOS)
[![License](https://img.shields.io/badge/license-MIT-green)](LICENSE)

> **Payments fail. Recovery shouldn't.**

RecoveryOS is a payment recovery system that turns failed payments into **controlled, explainable recovery decisions**.

Instead of treating a failed payment as a terminal event or blindly retrying it, RecoveryOS evaluates the failure, applies recovery policy and economic constraints, executes a safe recovery action, observes the actual outcome, and records the complete lifecycle for audit and replay.

---

## The Problem

A failed payment is not simply an error.

It can result in:

- Lost revenue
- Unnecessary retries
- Additional processing costs
- Poor customer experience
- Duplicate recovery attempts
- Difficult-to-explain payment decisions

A conventional approach often looks like:

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

The objective is not to maximize retry volume.

It is to determine:

> **What is the safest and most economically rational next action?**

---

# What RecoveryOS Provides

### Failure-Aware Recovery

Payment failures are interpreted using their available context rather than blindly triggering identical retry behavior.

### Policy-Driven Decisions

Recovery behavior is governed by explicit policies and constraints instead of an uncontrolled retry loop.

### Economic Guardrails

Recovery attempts account for their real economic cost.

A technically possible recovery action is not automatically an economically sensible one.

### Idempotent Event Processing

Repeated delivery of the same payment event does not create duplicate recovery work.

### Conflicting Payload Detection

If the same delivery identifier arrives with a different payload, RecoveryOS rejects the conflict rather than silently overwriting the previously accepted event.

### Concurrency Safety

Concurrent requests targeting the same recovery path are handled safely so that races do not produce duplicate recovery outcomes.

### Outcome Observation

RecoveryOS records what actually happened after an action rather than assuming execution itself represents success.

### Replayability

Recovery scenarios can be replayed to understand how the system evaluates the same payment/event context and policy configuration.

### Operator Authentication

Sensitive operational APIs are protected by an operator authentication boundary, while intentionally public endpoints such as health checks and webhook ingestion remain separately handled.

### Auditability

Recovery decisions and outcomes are persisted so the system can answer:

> **What happened, what did RecoveryOS do, and why?**

---

# Architecture

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

# Core Design Principles

## 1. Recovery is a decision, not a retry loop

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

---

## 2. Idempotency is a system property

Duplicate events are expected in distributed payment systems.

RecoveryOS treats idempotency as an architectural requirement.

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

The system also distinguishes between:

```text
Same delivery + same payload
        → idempotent replay

Same delivery + different payload
        → conflict / rejection
```

---

## 3. Concurrency must be safe

Sequential correctness is not enough for payment infrastructure.

RecoveryOS explicitly tests concurrent ingestion and recovery execution:

```text
Request 1 ─────┐
               ├──→ One Recovery Outcome
Request 2 ─────┘
```

rather than allowing:

```text
Request 1 ──→ Recovery
Request 2 ──→ Recovery
               ↓
        Duplicate Outcome
```

---

## 4. Economic safety matters

Recovery actions have costs.

RecoveryOS therefore evaluates recovery against configured economic constraints rather than assuming:

```text
more retries = more recovered revenue
```

The system instead considers the relationship between:

```text
Recovered Revenue
        −
Recovery Cost
        −
Operational Risk
```

---

## 5. Fail closed

Security-sensitive functionality should not become public because configuration is missing.

When required authentication configuration is unavailable, protected operational endpoints fail closed rather than silently falling back to insecure behavior.

---

# Recovery Lifecycle

A simplified recovery lifecycle:

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
Execute Safely
      │
      ▼
Observe Actual Outcome
      │
      ▼
Persist Recovery Result
```

The result is not simply:

```text
success / failure
```

Instead, the system maintains a controlled recovery state that explains what happened and why.

---

# Replayability

Recovery decisions should be explainable after the fact.

RecoveryOS supports replaying recovery scenarios using the relevant event context and policy configuration.

This enables investigation of questions such as:

- Why was this recovery action selected?
- Why wasn't another action attempted?
- Did an economic constraint prevent recovery?
- What would happen if this scenario were evaluated again?
- Did the system observe the real payment outcome correctly?

Replayability also provides a foundation for safely evaluating changes to recovery policy.

---

# Security & Reliability

RecoveryOS has undergone multiple security and production-readiness passes focused on realistic distributed-system failure modes.

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

The system is designed around an intentionally adversarial assumption:

> **Events can be duplicated, requests can race, providers can retry, and failures can happen at every boundary.**

---

# Production Readiness

RecoveryOS did not stop at the happy path.

The final hardening cycle addressed verified production-readiness findings across:

| Area | Protection |
|---|---|
| Authentication | Protected operational routers require operator authorization |
| Fail-closed behavior | Missing security configuration does not silently expose protected APIs |
| Webhooks | Duplicate deliveries are handled idempotently |
| Payload integrity | Conflicting duplicate deliveries are rejected |
| Concurrency | Race conditions are explicitly tested |
| Recovery outcomes | Duplicate concurrent recovery results are prevented |
| Payment failures | Real event observation context is preserved |
| Economic controls | Actual recovery costs participate in policy evaluation |
| Replay | Recovery scenarios can be re-evaluated consistently |
| Regression safety | Discovered vulnerabilities become regression tests |

---

# Tech Stack

### Backend

- Python
- FastAPI
- SQLAlchemy
- Pydantic

### Payments & Events

- Stripe payment/event flows
- Webhook-based event ingestion

### Testing

- Pytest
- Unit tests
- Integration tests
- Adversarial regression tests
- Concurrency-focused tests

### Architecture

- API-driven backend
- Policy-based recovery engine
- Persistent recovery outcomes
- Idempotent event processing
- Replayable scenarios
- Operator-protected operational APIs

---

# Project Structure

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
├── LICENSE
└── README.md
```

The backend contains the recovery engine, API boundaries, persistence, payment/event handling, policy evaluation, authentication, and automated tests.

---

# Getting Started

## Prerequisites

- Python 3.11+
- A configured payment provider test environment
- Required environment variables configured according to the backend configuration

## Clone

```bash
git clone https://github.com/shreyasvp26/RecoveryOS.git
cd RecoveryOS
```

## Install dependencies

Install the backend dependencies using the project's dependency configuration.

## Configure environment

Create/configure the required environment variables for the local environment.

Refer to the backend configuration files for the complete configuration surface.

## Run

Start the FastAPI application using the project's configured ASGI entrypoint.

---

# Testing

Run the project's test suite using the configured test command.

The test suite places particular emphasis on failure modes that are easy to miss in ordinary happy-path testing:

```text
Duplicate Delivery
        +
Conflicting Payload
        +
Concurrent Requests
        +
Provider Retries
        +
Partial Failure
        +
Economic Constraints
        ↓
Safe, Deterministic Recovery
```

---

# Example

A simplified failure scenario:

```text
Payment
   │
   ├── Payment succeeds
   │       └── Record successful outcome
   │
   └── Payment fails
           │
           ▼
      Failure Context
           │
           ▼
      Recovery Policy
           │
           ├── Not eligible
           │       └── Stop safely
           │
           ├── Economic limit exceeded
           │       └── Stop safely
           │
           └── Eligible
                   │
                   ▼
             Recovery Action
                   │
                   ▼
             Observe Outcome
                   │
                   ▼
             Persist Result
```

The important distinction is that **RecoveryOS decides whether recovery should happen before attempting it**.

---

# Design Tradeoffs

RecoveryOS intentionally favors **correctness and auditability over blind automation**.

The system may choose not to recover a payment when:

- The failure is not recoverable
- Policy does not permit the action
- Economic constraints are violated
- The action has already been performed
- Required context is unavailable
- Safe execution cannot be established

This is deliberate.

In payment infrastructure, an incorrect automated action can be worse than taking no action.

---

# What Makes RecoveryOS Different?

RecoveryOS is not simply:

- A webhook receiver
- A retry scheduler
- A payment dashboard
- A collection of API endpoints

It treats payment recovery as an **autonomous, policy-constrained decision system**.

The recovery lifecycle combines:

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

into one controlled system.

---

# Project Status

**Production-ready core / active product development**

The core recovery and reliability architecture has completed its implementation and hardening cycle.

The project has specifically addressed:

- Recovery decisioning
- Economic guardrails
- Webhook reliability
- Idempotency
- Concurrency safety
- Operator authentication
- Outcome observation
- Replayability
- Adversarial regression testing

The remaining evolution is primarily around product experience, operational UX, and future recovery strategies rather than the foundational recovery guarantees.

---

# Roadmap

Potential future directions include:

- More recovery strategies
- More sophisticated policy evaluation
- Additional payment providers
- Expanded observability
- Recovery analytics
- Policy simulation tooling
- More advanced operator workflows
- Improved recovery optimization based on historical outcomes

---

# Contributing

Contributions, bug reports, and ideas are welcome.

If you find a security or reliability issue, please avoid opening a public issue with sensitive details. Report it privately to the repository maintainer first.

For general contributions:

1. Fork the repository
2. Create a feature branch
3. Make your changes
4. Add or update tests
5. Verify the test suite
6. Open a pull request with a clear description of the change

---

# Security

Security and reliability are core design concerns of RecoveryOS.

If you discover a potential vulnerability, please report it responsibly rather than publicly disclosing an exploitable issue before it can be addressed.

Do not include real payment credentials, customer information, API keys, webhook secrets, or other sensitive data in issues or pull requests.

---

# License

RecoveryOS is licensed under the **MIT License**.

See the [`LICENSE`](LICENSE) file for the complete license text.

---

# Disclaimer

RecoveryOS is an engineering project and should be evaluated appropriately before being used in a production payment environment.

Payment systems involve financial, operational, and security risks. Production deployments should include appropriate provider configuration, monitoring, access controls, secrets management, compliance review, and operational safeguards.

---

# Author

**Shreyas Patil**

GitHub: [@shreyasvp26](https://github.com/shreyasvp26)

Project: [RecoveryOS](https://github.com/shreyasvp26/RecoveryOS)

---

# Acknowledgements

RecoveryOS builds on the excellent open-source ecosystem around:

- [FastAPI](https://fastapi.tiangolo.com/)
- [Pydantic](https://docs.pydantic.dev/)
- [SQLAlchemy](https://www.sqlalchemy.org/)
- [Pytest](https://docs.pytest.org/)
- [Stripe](https://stripe.com/)

---

## Philosophy

> **Don't just retry payments. Reason about recovery.**

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

---

<p align="center">
  <strong>Payments fail. Recovery shouldn't.</strong>
</p>

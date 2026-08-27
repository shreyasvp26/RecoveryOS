# RecoveryOS

AI Revenue Recovery Control Plane — Razorpay AI Buildathon 2026 (Revenue Recovery track).

## Core Principle

**AI recommends. Deterministic policy decides. Executor acts. Benchmark proves value.**

The LLM never has direct authority over a money-moving action. AI output is advisory; a deterministic policy gate is authoritative; an executor performs the action.

> **Note:** This repository is currently in **Phase 1 — Repository & Engineering Foundation**. It establishes scaffolded backend, frontend, documentation, and testing infrastructure only. NO revenue recovery functionality exists yet.

## Repository Structure

```
recoveryos/
├── backend/       FastAPI application + pytest
├── frontend/      React + Vite application
├── docs/          Engineering contracts and product notes
├── README.md
└── .gitignore
```

See `docs/ARCHITECTURE.md` for the locked V1 architecture and the distinction between planned and currently implemented scope.

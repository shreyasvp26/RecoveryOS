"""Phase 10: persist and read the canonical benchmark run.

The Phase 9 benchmark (``app/benchmark.py``) is frozen and CLI-only; it is
computed against an in-memory database and never persisted. Phase 10 adds a
thin read/persistence boundary so the Recovery Command Center can display real
backend benchmark data — WITHOUT touching any frozen benchmark algorithm,
strategy, or metric definition. This module only runs the frozen runner and
stores its already-computed summary.

Usage (from backend/):
    python -m app.benchmark_store --seed 42 --count 500
"""

from __future__ import annotations

import argparse
import json
from typing import Any, Sequence

from . import db
from .benchmark import run_benchmark


def persist_benchmark(
    conn, *, seed: int, event_count: int
) -> dict[str, Any]:
    """Run the frozen Phase 9 benchmark and persist its run summary."""
    run = run_benchmark(seed=seed, event_count=event_count).run
    db.upsert_benchmark_run(
        conn,
        run_id=run.run_id,
        seed=run.seed,
        event_count=run.event_count,
        model_seed=run.model_seed,
        evaluation_time=run.evaluation_time,
        evaluation_mode=run.evaluation_mode,
        summary_json=json.dumps(run.to_dict()),
    )
    return run.to_dict()


def persist_phase17_benchmark(
    conn, *, seed: int, event_count: int
) -> dict[str, Any]:
    """Run the Phase 17 signal-bearing benchmark and persist its summary.

    Uses the SAME ``benchmark_runs`` table — Phase 17 introduces no second
    database and no second persistence service. Only the compact aggregate
    summary is stored: per-event hidden probabilities, draws and true expected
    values stay in the in-memory report and never enter the operational
    database, so the operator dashboard cannot accidentally serve ground truth.
    """
    from .benchmark_config import Phase17BenchmarkConfig
    from .benchmark_phase17 import run_phase17_benchmark
    from .benchmark_phase17_report import summarize_report

    config = Phase17BenchmarkConfig(
        event_count=event_count, event_seed=seed, outcome_seed=seed
    )
    summary = summarize_report(run_phase17_benchmark(config))
    db.upsert_benchmark_run(
        conn,
        run_id=config.run_id(),
        seed=config.event_seed,
        event_count=config.event_count,
        model_seed=config.outcome_seed,
        evaluation_time=config.evaluated_at,
        evaluation_mode=config.evaluation_mode,
        summary_json=json.dumps(summary),
    )
    return summary


def _main(argv: Sequence[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        description="Run and persist the canonical RecoveryOS benchmark summary."
    )
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--count", type=int, default=500)
    parser.add_argument(
        "--phase9",
        action="store_true",
        help=(
            "persist the frozen Phase 9 three-strategy benchmark instead of the "
            "Phase 17 five-arm benchmark"
        ),
    )
    args = parser.parse_args(argv)
    conn = db.connect_database()
    db.init_db(conn)
    try:
        persist = persist_benchmark if args.phase9 else persist_phase17_benchmark
        result = persist(conn, seed=args.seed, event_count=args.count)
        print(json.dumps(result, indent=2))
    finally:
        conn.close()


if __name__ == "__main__":
    _main()

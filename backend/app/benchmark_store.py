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


def _main(argv: Sequence[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        description="Run and persist the canonical RecoveryOS benchmark summary."
    )
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--count", type=int, default=500)
    args = parser.parse_args(argv)
    conn = db.connect_database()
    db.init_db(conn)
    try:
        result = persist_benchmark(conn, seed=args.seed, event_count=args.count)
        print(json.dumps(result, indent=2))
    finally:
        conn.close()


if __name__ == "__main__":
    _main()

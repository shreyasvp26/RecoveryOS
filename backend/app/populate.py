"""Phase 10: populate the durable pipeline DB from generated events.

For a fresh or reset database, this generates a deterministic synthetic event
set, ingests it, classifies each event through the deterministic controlled
classifier, and runs each event through the real bounded execution flow
(SIMULATED — no Razorpay client is configured, so no real provider call is
possible). The result is the persisted decision chain the operator dashboard
renders. This reuses the frozen modules unchanged and never fabricates records.

Usage (from backend/, then start the API):
    python -m app.populate --seed 42 --count 100
"""

from __future__ import annotations

import argparse
import sqlite3
from datetime import datetime, timezone

from . import db
from .benchmark import DeterministicClassifier
from .classifier import classify_event
from .config import build_policy_config
from .execution_service import execute_event
from .generator import generate_events

# Fixed reference evaluation time (NOT wall clock). A deterministic reference
# keeps the persisted decision chain (and therefore a clean reset + rebuild)
# byte-for-byte reproducible across runs, matching the deterministic generator
# and benchmark. Within a single pass the cooldown/retry-limit/duplicate checks
# already used one constant; fixing the constant merely makes the absolute
# timestamps reproducible too.
REFERENCE_AT = datetime(2026, 8, 27, 13, 0, tzinfo=timezone.utc)


def populate(
    *,
    seed: int = 42,
    count: int = 100,
    conn: sqlite3.Connection | None = None,
) -> dict[str, int]:
    """Populate the durable pipeline DB and return counts of what happened.

    Idempotent: events that already carry a persisted classification are
    skipped, so re-running on an existing (non-clean) DB is a safe no-op and
    never duplicates outcomes, attempts, or decisions. Deterministic: the
    evaluation time is a fixed reference, so a clean DB rebuilt from the same
    seed reproduces the exact same persisted chain.
    """
    owns_connection = conn is None
    conn = conn or db.connect_database()
    db.init_db(conn)
    try:
        config = build_policy_config()
        classifier = DeterministicClassifier()
        events = generate_events(seed=seed, count=count)

        ingested = 0
        classified = 0
        executed = 0
        blocked = 0
        for event in events:
            if db.get_payment_event(conn, event.event_id) is None:
                db.insert_payment_event(conn, event)
                ingested += 1
            if db.get_classification_result(conn, event.event_id) is not None:
                # Already processed by a previous run: skip to stay idempotent.
                continue
            classification = classify_event(event, classifier)
            db.insert_classification_result(conn, classification)
            classified += 1
            result = execute_event(
                conn, event.event_id, REFERENCE_AT, config, razorpay_client=None
            )
            if result.outcome is not None:
                executed += 1
            elif result.status == "no_action":
                blocked += 1
        return {
            "events_generated": len(events),
            "ingested": ingested,
            "classified": classified,
            "executed": executed,
            "blocked_no_execution": blocked,
        }
    finally:
        if owns_connection:
            conn.close()


def _main(argv=None) -> None:
    parser = argparse.ArgumentParser(
        description="Populate the durable pipeline DB for the operator dashboard."
    )
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--count", type=int, default=100)
    args = parser.parse_args(argv)
    print(populate(seed=args.seed, count=args.count))


if __name__ == "__main__":
    _main()

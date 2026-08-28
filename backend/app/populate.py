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
from datetime import datetime, timezone

from . import db
from .benchmark import DeterministicClassifier
from .classifier import classify_event
from .config import build_policy_config
from .execution_service import execute_event
from .generator import generate_events


def populate(*, seed: int = 42, count: int = 100) -> dict[str, int]:
    """Populate the configured DB and return counts of what happened."""
    conn = db.connect_database()
    db.init_db(conn)
    try:
        config = build_policy_config()
        classifier = DeterministicClassifier()
        events = generate_events(seed=seed, count=count)
        now = datetime.now(timezone.utc)

        ingested = 0
        classified = 0
        executed = 0
        blocked = 0
        for event in events:
            if db.get_payment_event(conn, event.event_id) is None:
                db.insert_payment_event(conn, event)
                ingested += 1
            classification = classify_event(event, classifier)
            db.insert_classification_result(conn, classification)
            classified += 1
            result = execute_event(
                conn, event.event_id, now, config, razorpay_client=None
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

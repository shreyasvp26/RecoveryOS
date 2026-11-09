"""Phase 13: clean-database reset / populate idempotency and reproducibility.

Verifies that the canonical demo rebuild procedure is safe and deterministic:

- a clean DB rebuilt with the same seed reproduces the exact same persisted
  chain (events -> classifications -> policy decisions -> executions -> attempts),
  byte-for-byte, so a clean reset + rebuild yields identical dashboard/benchmark
  inputs;
- re-running populate on an already-populated DB is a safe no-op: it never
  crashes (no classification PK conflict) and never duplicates outcomes,
  attempts, or decisions.
"""

from __future__ import annotations

import sqlite3

from app.populate import populate
from app.db import connect, init_db
from app.generator import generate_events


def _fresh_db(tmp_path, name: str) -> sqlite3.Connection:
    conn = connect(str(tmp_path / name))
    init_db(conn)
    return conn


def _dump(conn: sqlite3.Connection) -> list[tuple[str, tuple]]:
    """Normalized dump of every table, invariant to insertion order/rowids."""
    tables = [
        r[0]
        for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' "
            "ORDER BY name"
        ).fetchall()
    ]
    out: list[tuple[str, tuple]] = []
    for table in tables:
        rows = conn.execute(f"SELECT * FROM {table}").fetchall()
        normalized = tuple(sorted((tuple(map(str, r)) for r in rows)))
        out.append((table, normalized))
    return out


def test_populate_clean_rebuild_is_reproducible(tmp_path) -> None:
    first = _fresh_db(tmp_path, "a.db")
    second = _fresh_db(tmp_path, "b.db")
    try:
        r1 = populate(seed=42, count=40, conn=first)
        r2 = populate(seed=42, count=40, conn=second)
        # Same seed + same count -> same work done.
        assert r1 == r2
        # Same persisted chain, byte-for-byte, across two clean rebuilds.
        assert _dump(first) == _dump(second)
    finally:
        first.close()
        second.close()


def test_populate_changes_with_seed(tmp_path) -> None:
    first = _fresh_db(tmp_path, "a.db")
    second = _fresh_db(tmp_path, "b.db")
    try:
        populate(seed=42, count=40, conn=first)
        populate(seed=43, count=40, conn=second)
        # A different seed produces different event attributes (amounts, risk,
        # histories), even though the index-derived event IDs are shared.
        ev_a = [e.to_dict() for e in generate_events(seed=42, count=40)]
        ev_b = [e.to_dict() for e in generate_events(seed=43, count=40)]
        assert ev_a != ev_b
        assert _dump(first) != _dump(second)
    finally:
        first.close()
        second.close()


def test_populate_is_idempotent_on_rerun(tmp_path) -> None:
    once = _fresh_db(tmp_path, "once.db")
    twice = _fresh_db(tmp_path, "twice.db")
    try:
        # Baseline: a single clean populate run on `once`.
        baseline = populate(seed=42, count=30, conn=once)
        assert baseline["classified"] == 30

        # Multiple runs against the SAME `twice` DB: the 2nd/3rd are no-ops.
        populate(seed=42, count=30, conn=twice)  # run 1
        rerun = populate(seed=42, count=30, conn=twice)  # run 2 -> no-op
        rerun2 = populate(seed=42, count=30, conn=twice)  # run 3 -> no-op
        assert rerun["classified"] == 0
        assert rerun2["classified"] == 0

        # The thrice-run DB is byte-identical to the single-run baseline,
        # proving the re-runs added no duplicate outcomes/attempts/decisions.
        assert _dump(once) == _dump(twice)
    finally:
        once.close()
        twice.close()


def test_populate_does_not_crash_on_rerun_without_idempotency_guard(db_conn) -> None:
    """Re-running must never raise (no classification PK IntegrityError)."""
    populate(seed=42, count=5, conn=db_conn)
    populate(seed=42, count=5, conn=db_conn)  # must not raise
    populate(seed=42, count=5, conn=db_conn)  # must not raise


def test_populate_seeds_benchmark_run_when_empty(db_conn) -> None:
    """Phase 42: A clean populate run seeds benchmark_runs for the Command Center."""
    from app import db
    assert db.get_latest_benchmark_run(db_conn) is None
    res = populate(seed=42, count=10, conn=db_conn)
    assert res["events_generated"] == 10
    bench = db.get_latest_benchmark_run(db_conn)
    assert bench is not None
    assert bench["seed"] == 42
    assert bench["event_count"] == 10


def test_populate_reset_clears_and_repopulates(db_conn) -> None:
    """Phase 42: populate(reset=True) clears operational tables and re-populates."""
    from app import db
    populate(seed=42, count=10, conn=db_conn)
    assert db.count_payment_events(db_conn) == 10

    # Populating with reset=True and new count
    res = populate(seed=42, count=5, conn=db_conn, reset=True)
    assert res["ingested"] == 5
    assert db.count_payment_events(db_conn) == 5
    bench = db.get_latest_benchmark_run(db_conn)
    assert bench is not None
    assert bench["event_count"] == 5


def test_populate_main_cli_parses_reset(monkeypatch) -> None:
    """Phase 42: _main accepts --reset flag."""
    from app.populate import _main
    called = {}

    def mock_populate(**kwargs):
        called.update(kwargs)
        return {"events_generated": 5, "ingested": 5, "classified": 5, "executed": 2, "blocked_no_execution": 3}

    monkeypatch.setattr("app.populate.populate", mock_populate)
    _main(["--reset", "--seed", "99", "--count", "15"])
    assert called == {"seed": 99, "count": 15, "reset": True}


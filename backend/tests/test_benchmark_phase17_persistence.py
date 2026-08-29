"""Phase 17 tests: persistence and the Command Center benchmark payload.

Phase 17 introduces no second database and no second persistence service: it
reuses the existing ``benchmark_runs`` table. These tests pin that, pin the
methodology dispatch that keeps the frozen Phase 9 display working, and pin
that no benchmark figure is ever hardcoded.
"""

from __future__ import annotations

import json

import pytest

from app import db
from app.benchmark_store import persist_benchmark, persist_phase17_benchmark
from app.dashboard import build_dashboard_summary


@pytest.fixture()
def conn():
    connection = db.connect(":memory:")
    db.init_db(connection)
    try:
        yield connection
    finally:
        connection.close()


def test_phase17_persists_into_the_existing_benchmark_table(conn) -> None:
    persist_phase17_benchmark(conn, seed=42, event_count=40)
    tables = {
        row[0]
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
    }
    assert "benchmark_runs" in tables
    assert not [name for name in tables if "phase17" in name]


def test_the_dashboard_shows_all_five_arms(conn) -> None:
    persist_phase17_benchmark(conn, seed=42, event_count=40)
    benchmark = build_dashboard_summary(conn)["benchmark"]
    assert benchmark["available"] is True
    assert benchmark["evaluation_mode"] == "SIMULATED"
    assert [row["strategy"] for row in benchmark["strategies"]] == [
        "no_action",
        "naive_retry",
        "recoveryos_v1",
        "recoveryos_v2",
        "oracle",
    ]
    for row in benchmark["strategies"]:
        for field in (
            "recovered_amount_paise",
            "incremental_vs_no_action_paise",
            "interventions_attempted",
            "total_regret_paise",
            "optimal_selection_rate",
            "unauthorized_attempts",
            "exceptions",
        ):
            assert field in row


def test_the_frozen_phase_9_display_still_works(conn) -> None:
    """The historical three-strategy benchmark must remain renderable."""
    persist_benchmark(conn, seed=42, event_count=20)
    benchmark = build_dashboard_summary(conn)["benchmark"]
    assert benchmark["available"] is True
    assert [row["strategy"] for row in benchmark["strategies"]] == [
        "no_action",
        "naive_retry",
        "recovery_os",
    ]
    assert "recovery_os_recovery_rate" in benchmark


def test_a_corrupt_summary_is_reported_not_guessed(conn) -> None:
    db.upsert_benchmark_run(
        conn,
        run_id="broken:phase17",
        seed=42,
        event_count=10,
        model_seed=42,
        evaluation_time="2026-08-27T13:00:00+00:00",
        evaluation_mode="SIMULATED",
        summary_json=json.dumps(
            {"config": {"methodology": "phase17_signal_bearing_v1"}}
        ),
    )
    benchmark = build_dashboard_summary(conn)["benchmark"]
    assert benchmark["available"] is False
    assert benchmark["error"] == "corrupt persisted benchmark summary"


def test_deleting_and_regenerating_reproduces_the_identical_numbers(conn) -> None:
    """No benchmark figure is hardcoded: wipe the store and it comes back."""
    first = persist_phase17_benchmark(conn, seed=42, event_count=60)
    before = build_dashboard_summary(conn)["benchmark"]

    conn.execute("DELETE FROM benchmark_runs")
    conn.commit()
    assert build_dashboard_summary(conn)["benchmark"] == {"available": False}

    second = persist_phase17_benchmark(conn, seed=42, event_count=60)
    after = build_dashboard_summary(conn)["benchmark"]

    assert first["strategies"] == second["strategies"]
    assert first["result"] == second["result"]
    assert {k: v for k, v in before.items() if k != "saved_at"} == {
        k: v for k, v in after.items() if k != "saved_at"
    }


def test_persisted_revenue_matches_the_recomputed_benchmark(conn) -> None:
    """The displayed number must be derivable from the harness, not stored lore."""
    from app.benchmark_config import Phase17BenchmarkConfig
    from app.benchmark_phase17 import STRATEGY_V2, run_phase17_benchmark
    from app.benchmark_phase17_metrics import strategy_metrics

    persist_phase17_benchmark(conn, seed=42, event_count=60)
    displayed = build_dashboard_summary(conn)["benchmark"]

    report = run_phase17_benchmark(
        Phase17BenchmarkConfig(event_count=60, event_seed=42, outcome_seed=42)
    )
    expected = strategy_metrics(report, STRATEGY_V2).recovered_revenue_paise
    assert displayed["recovery_os_recovered_amount_paise"] == expected

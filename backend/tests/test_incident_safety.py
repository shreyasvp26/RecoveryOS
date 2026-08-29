"""Phase 20 safety: what incident detection and incident replay must never do.

These are structural checks, not promises. The Phase 20 modules must not be
able to reach a payment provider, must not read hidden ground truth, must not
depend on the wall clock or on randomness, and must leave the database, the
benchmark and the active policy exactly as they found them.
"""

from __future__ import annotations

import io
import pathlib
import tokenize

import pytest
from fastapi.testclient import TestClient

from app import db
from app.benchmark_config import Phase17BenchmarkConfig
from app.generator import generate_events
from app.incident_analysis import (
    analyse_workload,
    evaluate_workload,
    load_workload,
    replay_incident,
)
from app.main import app
from app.policy_scenario import (
    aggressive_scenario,
    conservative_scenario,
    current_scenario,
)

client = TestClient(app)

PHASE20_SOURCES = (
    "app/incidents.py",
    "app/incident_analysis.py",
    "app/routes/incidents.py",
)
BACKEND_ROOT = pathlib.Path(__file__).resolve().parents[1]


def source_of(relative: str) -> str:
    return (BACKEND_ROOT / relative).read_text()


def code_of(relative: str) -> str:
    """The module's executable code, with comments and string literals removed.

    Phase 20 documents at length what it must never do, so scanning raw source
    for forbidden names would match the prose that promises to avoid them. This
    scans what actually runs: identifiers, attributes and operators only.
    """
    tokens = tokenize.generate_tokens(io.StringIO(source_of(relative)).readline)
    return " ".join(
        token.string
        for token in tokens
        if token.type not in (tokenize.COMMENT, tokenize.STRING)
    )


@pytest.fixture
def workload(monkeypatch, tmp_path) -> str:
    db_path = tmp_path / "incident_safety.db"
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{db_path}")
    conn = db.connect(str(db_path))
    db.init_db(conn)
    for event in generate_events(seed=42, count=500):
        db.insert_payment_event(conn, event)
    conn.close()
    return str(db_path)


@pytest.fixture
def analysis(workload):
    conn = db.connect(workload)
    db.init_db(conn)
    try:
        yield analyse_workload(conn)
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# No provider, ever
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("relative", PHASE20_SOURCES)
def test_phase20_modules_never_reach_the_payment_provider(relative):
    code = code_of(relative).lower()
    for forbidden in ("razorpay", "payment_link", "httpx", "requests", "socket"):
        assert forbidden not in code, f"{relative} must not reference {forbidden}"


def test_every_replayed_execution_is_simulated(analysis):
    incident = analysis["incidents"][0]
    comparison = replay_incident(
        incident,
        analysis["events"],
        (current_scenario(), conservative_scenario(), aggressive_scenario()),
    )
    assert comparison["replay_mode"] == "SIMULATED"
    for arm in comparison["scenarios"]:
        assert arm["metrics"]["replay_mode"] == "SIMULATED"


def test_the_evaluation_behind_detection_is_simulated(analysis):
    result = analysis["result"]
    assert result.replay_mode == "SIMULATED"
    for record in result.records:
        assert record.replay_mode == "SIMULATED"
        assert record.execution_mode in (None, "SIMULATED")


def test_an_unreachable_provider_cannot_affect_incident_analysis(
    monkeypatch, workload
):
    """No Phase 20 path builds a provider client, so breaking one changes nothing."""
    import app.razorpay_client as razorpay_client

    def explode(*args, **kwargs):
        raise AssertionError("Phase 20 must never construct a Razorpay client")

    monkeypatch.setattr(razorpay_client, "RazorpayClient", explode, raising=False)
    incident_id = client.get("/incidents").json()["incidents"][0]["incident_id"]
    assert client.get(f"/incidents/{incident_id}").status_code == 200
    assert client.post(f"/incidents/{incident_id}/replay").status_code == 200


# ---------------------------------------------------------------------------
# No hidden ground truth
# ---------------------------------------------------------------------------


def test_the_detector_never_imports_the_hidden_world():
    code = code_of("app/incidents.py")
    assert "hidden_world" not in code
    assert "outcome_model" not in code
    assert "HiddenWorld" not in code


@pytest.mark.parametrize("relative", PHASE20_SOURCES)
def test_no_phase20_module_reads_ground_truth_fields(relative):
    code = code_of(relative).lower()
    for forbidden in ("true_probability", "true_ev", "oracle", "recovery_probability"):
        assert forbidden not in code


def test_no_incident_payload_carries_a_probability(analysis):
    encoded = str([incident.to_dict() for incident in analysis["incidents"]])
    for forbidden in ("probability", "true_ev", "oracle"):
        assert forbidden not in encoded.lower()


def test_incident_replay_records_carry_no_ground_truth(analysis):
    comparison = replay_incident(
        analysis["incidents"][0],
        analysis["events"],
        (current_scenario(), conservative_scenario()),
    )
    encoded = str(comparison).lower()
    assert "true_probability" not in encoded
    assert "true_ev" not in encoded


# ---------------------------------------------------------------------------
# No nondeterminism
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("relative", PHASE20_SOURCES)
def test_phase20_modules_use_no_clock_and_no_randomness(relative):
    code = code_of(relative).replace(" ", "")
    for forbidden in ("datetime.now", "utcnow", "uuid", "random.", "time.time"):
        assert forbidden not in code, f"{relative} must not use {forbidden}"


def test_detection_does_not_depend_on_the_wall_clock(analysis):
    """The windows are anchored on data, so they never move on their own."""
    incident = analysis["incidents"][0]
    latest = max(event.timestamp for event in analysis["events"])
    assert incident.windows.anchor.isoformat() == max(
        event.timestamp
        for event in analysis["events"]
        if event.timestamp == latest
    )


# ---------------------------------------------------------------------------
# No mutation
# ---------------------------------------------------------------------------


def test_analysis_leaves_the_database_untouched(workload):
    def snapshot():
        conn = db.connect(workload)
        try:
            return (
                db.count_payment_events(conn),
                db.get_policy_decision_stats(conn),
                db.get_execution_outcome_stats(conn),
                db.get_latest_benchmark_run(conn),
            )
        finally:
            conn.close()

    before = snapshot()
    conn = db.connect(workload)
    db.init_db(conn)
    try:
        analyse_workload(conn)
    finally:
        conn.close()
    assert snapshot() == before


def test_analysis_never_mutates_the_active_or_benchmark_policy(analysis):
    before_active = current_scenario().parameters
    before_benchmark = Phase17BenchmarkConfig().fingerprint()

    replay_incident(
        analysis["incidents"][0],
        analysis["events"],
        (current_scenario(), aggressive_scenario()),
    )

    assert current_scenario().parameters == before_active
    assert Phase17BenchmarkConfig().fingerprint() == before_benchmark


def test_evaluation_never_writes_an_intervention_attempt(workload):
    conn = db.connect(workload)
    db.init_db(conn)
    try:
        events = load_workload(conn)
        evaluate_workload(events)
        attempts = conn.execute(
            "SELECT COUNT(*) AS c FROM intervention_attempts"
        ).fetchone()["c"]
        outcomes = conn.execute(
            "SELECT COUNT(*) AS c FROM execution_outcomes"
        ).fetchone()["c"]
    finally:
        conn.close()
    assert attempts == 0
    assert outcomes == 0


def test_phase20_modules_never_write_to_the_database():
    for relative in PHASE20_SOURCES:
        code = code_of(relative)
        for forbidden in ("insert", "upsert", "update", "delete", "commit"):
            assert forbidden not in code.lower(), (
                f"{relative} must not write ({forbidden})"
            )

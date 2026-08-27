"""Phase 10 API tests for the read-only dashboard endpoints.

Covers the Recovery Command Center (/dashboard/summary), Event Decision Trace
(/events/{id}/trace), event listing (/events), and Policy & Blocked Actions
(/decisions/blocked). These tests assert that the dashboard reflects real
persisted backend state, that benchmark values come from the backend (never
hardcoded), and that empty/unavailable states are honest — an API result is
never replaced with a fabricated zero or winning number.
"""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from app.db import connect, init_db
from app.main import app
from app.routes.events import get_classifier

client = TestClient(app)

VALID_EVENT = {
    "event_id": "evt_dash_1",
    "order_id": "order_dash_1",
    "payment_id": "pay_dash_1",
    "customer_id": "cust_dash_1",
    "amount_paise": 75000,
    "currency": "INR",
    "payment_method": "card",
    "failure_reason": "bank_timeout",
    "bank": "HDFC",
    "risk_flag": "normal",
    "customer_history": {
        "prior_successful_payments": 4,
        "prior_failed_payments": 1,
        "has_active_subscription": True,
    },
    "timestamp": "2026-08-27T12:00:00+00:00",
}


class StubClassifier:
    """Stub classifier returning one fixed valid classification."""

    def __init__(self, result: dict) -> None:
        self._result = result

    def generate(self, prompt: str) -> str:
        return json.dumps(self._result)


def _classification(root: str = "transient", candidates: list[str] | None = None):
    return {
        "event_id": "evt_dash_1",
        "root_cause_category": root,
        "confidence": 0.9,
        "reasoning": "transient bank timeout; a retry may recover the payment.",
        "candidate_interventions": candidates or ["retry_immediate", "retry_delayed"],
    }


@pytest.fixture(autouse=True)
def _reset_dependency_overrides():
    yield
    app.dependency_overrides.clear()


def _set_test_db(monkeypatch, tmp_path, name: str = "dash_api.db") -> str:
    db_path = tmp_path / name
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{db_path}")
    return str(db_path)


def _stub_classifier(result: dict) -> None:
    app.dependency_overrides[get_classifier] = lambda: StubClassifier(result)


def _seed_event(monkeypatch, tmp_path, payload=None) -> str:
    db_path = _set_test_db(monkeypatch, tmp_path)
    resp = client.post("/events", json=payload or VALID_EVENT)
    assert resp.status_code == 201
    return db_path


def _seed_full_chain(monkeypatch, tmp_path, payload=None, root="transient") -> str:
    db_path = _seed_event(monkeypatch, tmp_path, payload)
    _stub_classifier(_classification(root=root))
    assert client.post("/events/evt_dash_1/classify").status_code == 200
    execute = client.post("/events/evt_dash_1/execute")
    return db_path, execute


def _seed_benchmark(monkeypatch, tmp_path, seed=42, count=10) -> str:
    db_path = _set_test_db(monkeypatch, tmp_path, "bench.db")
    import app.benchmark_store as bs

    conn = connect(db_path)
    init_db(conn)
    bs.persist_benchmark(conn, seed=seed, event_count=count)
    conn.close()
    return db_path


# ---------------------------------------------------------------------------
# Command Center summary
# ---------------------------------------------------------------------------


def test_summary_empty_db_is_honest(monkeypatch, tmp_path) -> None:
    _set_test_db(monkeypatch, tmp_path)
    body = client.get("/dashboard/summary").json()
    assert body["operational"]["event_count"] == 0
    assert body["operational"]["revenue_at_risk_paise"] == 0
    assert body["operational"]["interventions_executed"] == 0
    assert body["operational"]["blocked_interventions"] == 0
    # No benchmark persisted -> available False, NOT a fabricated amount.
    assert body["benchmark"]["available"] is False
    assert "amount" not in json.dumps(body["benchmark"])
    assert body["recoverable_revenue"]["defined"] is False
    assert body["not_recovered"]["available"] is True


def test_summary_benchmark_is_never_hardcoded_when_absent(
    monkeypatch, tmp_path
) -> None:
    _set_test_db(monkeypatch, tmp_path)
    body = client.get("/dashboard/summary").json()
    assert body["benchmark"]["available"] is False
    assert "recovered_amount_paise" not in body["benchmark"]


def test_summary_with_pipeline_data(monkeypatch, tmp_path) -> None:
    _seed_full_chain(monkeypatch, tmp_path)
    body = client.get("/dashboard/summary").json()
    op = body["operational"]
    assert op["event_count"] == 1
    assert op["revenue_at_risk_paise"] == VALID_EVENT["amount_paise"]
    assert op["interventions_executed"] == 1
    assert op["blocked_interventions"] == 0
    assert body["recoverable_revenue"]["defined"] is False


def test_summary_reflects_recorded_benchmark(monkeypatch, tmp_path) -> None:
    _seed_benchmark(monkeypatch, tmp_path, seed=42, count=20)
    body = client.get("/dashboard/summary").json()
    bench = body["benchmark"]
    assert bench["available"] is True
    assert bench["seed"] == 42
    assert bench["event_count"] == 20
    assert bench["evaluation_mode"] == "SIMULATED"
    strategies = {s["strategy"]: s for s in bench["strategies"]}
    assert set(strategies) == {"no_action", "naive_retry", "recovery_os"}
    for s in bench["strategies"]:
        assert "recovered_amount_paise" in s
        assert "recovery_rate" in s
        assert "efficiency_paise_per_intervention" in s
    assert "incremental_over_no_action_paise" in bench
    assert "recoveryos_vs_naive_retry_paise" in bench


# ---------------------------------------------------------------------------
# Event listing + Decision Trace
# ---------------------------------------------------------------------------


def test_event_list_returns_persisted_events(monkeypatch, tmp_path) -> None:
    _seed_event(monkeypatch, tmp_path)
    body = client.get("/events").json()
    assert body["count"] == 1
    assert body["events"][0]["event_id"] == "evt_dash_1"
    assert body["events"][0]["amount_paise"] == VALID_EVENT["amount_paise"]


def test_event_list_query_filter(monkeypatch, tmp_path) -> None:
    _seed_event(monkeypatch, tmp_path)
    body = client.get("/events", params={"query": "dash_1"}).json()
    assert body["count"] == 1
    body = client.get("/events", params={"query": "nope"}).json()
    assert body["count"] == 0


def test_trace_reconstructs_full_chain(monkeypatch, tmp_path) -> None:
    _seed_full_chain(monkeypatch, tmp_path)
    trace = client.get("/events/evt_dash_1/trace").json()
    assert trace["event"]["event_id"] == "evt_dash_1"
    assert trace["classification"]["root_cause_category"] == "transient"
    assert trace["classification"]["confidence"] == 0.9
    assert len(trace["policy_decisions"]) >= 1
    assert trace["executions"], "expected a persisted execution"
    exec0 = trace["executions"][0]
    assert exec0["intervention"] == "retry_delayed"
    assert exec0["execution_mode"] == "SIMULATED"
    assert exec0["status"] == "SUCCESS"
    assert trace["summary"]["final_decision"] == "ALLOW"
    assert trace["summary"]["execution_mode"] == "SIMULATED"
    assert trace["summary"]["execution_status"] == "SUCCESS"


def test_trace_blocked_decision_preserved(monkeypatch, tmp_path) -> None:
    _seed_full_chain(
        monkeypatch, tmp_path, payload=dict(VALID_EVENT, risk_flag="fraud_suspect"),
        root="fraud_suspect",
    )
    trace = client.get("/events/evt_dash_1/trace").json()
    denied = [d for d in trace["policy_decisions"] if not d["allowed"]]
    assert denied, "expected at least one denied decision on a fraud event"
    assert trace["summary"]["final_decision"] == "DENY"
    assert not trace["executions"], "fraud must never execute"


def test_trace_missing_event_is_404(monkeypatch, tmp_path) -> None:
    _set_test_db(monkeypatch, tmp_path)
    resp = client.get("/events/nope/trace")
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# Policy & Blocked Actions
# ---------------------------------------------------------------------------


def test_blocked_empty_is_distinguishable_from_failure(monkeypatch, tmp_path) -> None:
    _set_test_db(monkeypatch, tmp_path)
    body = client.get("/decisions/blocked").json()
    assert body["count"] == 0
    assert body["blocked"] == []


def test_blocked_fraud_decision_with_context(monkeypatch, tmp_path) -> None:
    _seed_full_chain(
        monkeypatch, tmp_path, payload=dict(VALID_EVENT, risk_flag="fraud_suspect"),
        root="fraud_suspect",
    )
    body = client.get("/decisions/blocked").json()
    assert body["count"] >= 1
    first = body["blocked"][0]
    assert first["event_id"] == "evt_dash_1"
    assert first["customer_id"] == "cust_dash_1"
    assert first["amount_paise"] == VALID_EVENT["amount_paise"]
    assert first["denial_reason"] == "fraud_protection"
    assert first["rule_label"] == "Fraud protection"
    assert first["category"] == "fraud"
    cats = {c["key"]: c["count"] for c in body["categories"]}
    assert cats["fraud"] >= 1

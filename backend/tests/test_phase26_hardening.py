"""Phase 26 production-boundary hardening regression tests.

Each test locks in one verified audit finding from DEEP_AUDIT_REPORT.md as a
regression: spend-cap economics, timestamp integrity, deploy-time migrations,
LIKE escaping, provider URL validation, exception-detail stability, concurrent
duplicate ingestion, claim-release semantics, CORS fail-closed defaults and
pinned dependencies.
"""

from __future__ import annotations

import json
import os
import sqlite3

import pytest
from fastapi.testclient import TestClient

from app.classifier import OmniRouteClassifier, OmniRouteError, _validate_base_url
from app.config import build_policy_config, default_intervention_cost_paise, get_cors_origins
from app.db import (
    connect,
    init_db,
    insert_payment_event,
    list_payment_events,
    run_migrations,
)
from app.economics import DEFAULT_ECONOMIC_MODEL
from app.execution_service import (
    STATUS_EXECUTION_CLAIM_RELEASED,
    _claim_conflict_result,
)
from app.failed_payment_ingestion import map_failed_payment_to_event
from app.ingestion import IngestionStatus, ingest_event
from app.main import app
from app.models import CustomerHistory, PaymentEvent
from app.razorpay_webhook import parse_payment_failed_payload
from conftest import TEST_OPERATOR_HEADERS

client = TestClient(app, headers=TEST_OPERATOR_HEADERS)


def make_event(event_id: str = "evt_hard", customer_id: str = "cust_hard") -> PaymentEvent:
    return PaymentEvent(
        event_id=event_id,
        order_id="order_hard",
        payment_id="pay_hard",
        customer_id=customer_id,
        amount_paise=75000,
        currency="INR",
        payment_method="card",
        failure_reason="bank_timeout",
        bank="HDFC",
        risk_flag="normal",
        customer_history=CustomerHistory(
            prior_successful_payments=2,
            prior_failed_payments=1,
            has_active_subscription=True,
        ),
        timestamp="2026-08-27T12:00:00+00:00",
    )


# ---------------------------------------------------------------------------
# S1 — the spend cap is backed by real economic costs, never structurally off
# ---------------------------------------------------------------------------


def test_spend_cap_uses_the_economic_model_costs() -> None:
    costs = default_intervention_cost_paise()
    for intervention in DEFAULT_ECONOMIC_MODEL.assumptions:
        assert costs[intervention] == (
            DEFAULT_ECONOMIC_MODEL.assumptions[intervention].cost_paise
        )
    payment_link = DEFAULT_ECONOMIC_MODEL.assumptions["payment_link"].cost_paise
    assert payment_link > 0
    assert build_policy_config().intervention_cost_paise["payment_link"] == payment_link


def test_replay_scenario_costs_cannot_drift_from_the_economic_model() -> None:
    from app.policy_scenario import (
        aggressive_scenario,
        conservative_scenario,
        current_scenario,
    )

    runtime_costs = default_intervention_cost_paise()
    for scenario in (current_scenario(), conservative_scenario(), aggressive_scenario()):
        assert scenario.policy_config.intervention_cost_paise == runtime_costs


# ---------------------------------------------------------------------------
# S2 — payment.failed without provider created_at uses the OBSERVATION time
# ---------------------------------------------------------------------------


def _failed_payload_without_created_at() -> dict:
    return {
        "entity": "event",
        "account_id": "acc_x",
        "event": "payment.failed",
        "contains": ["payment"],
        "payload": {
            "payment": {
                "entity": {
                    "id": "pay_no_ts",
                    "order_id": "order_no_ts",
                    "amount": 123400,
                    "currency": "INR",
                    "status": "failed",
                    "method": "upi",
                    "error_code": "BAD_REQUEST_ERROR",
                    "error_description": "declined",
                    "customer_id": "cust_no_ts",
                }
            }
        },
    }


def test_mapping_without_created_at_uses_observed_at_never_1970(tmp_path) -> None:
    conn = connect(str(tmp_path / "no_ts.db"))
    try:
        init_db(conn)
        payload = parse_payment_failed_payload(
            json.dumps(_failed_payload_without_created_at()).encode("utf-8"),
            "delivery_no_ts",
        )
        event = map_failed_payment_to_event(
            conn, payload, observed_at="2026-08-27T14:30:00+00:00"
        )
        assert event.timestamp == "2026-08-27T14:30:00+00:00"
        assert not event.timestamp.startswith("1970")
    finally:
        conn.close()


def test_mapping_with_neither_timestamp_fails_explicitly(tmp_path) -> None:
    import dataclasses

    conn = connect(str(tmp_path / "no_ts2.db"))
    try:
        init_db(conn)
        payload = parse_payment_failed_payload(
            json.dumps(_failed_payload_without_created_at()).encode("utf-8"),
            "delivery_no_ts2",
        )
        payload = dataclasses.replace(payload, failed_at=None)
        with pytest.raises(ValueError, match="no created_at"):
            map_failed_payment_to_event(conn, payload)
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# S3 — migrations run at deploy time; per-request init_db stays non-destructive
# ---------------------------------------------------------------------------


def _seed_legacy_duplicates(conn: sqlite3.Connection) -> None:
    conn.execute(
        "CREATE TABLE webhook_recovery_outcomes ("
        "delivery_id TEXT PRIMARY KEY, payment_link_id TEXT NOT NULL, "
        "referenced_event_id TEXT NOT NULL, amount_paid_paise INTEGER, "
        "currency TEXT, payment_id TEXT, recovered_at TEXT NOT NULL)"
    )
    conn.execute(
        "INSERT INTO webhook_recovery_outcomes VALUES (?, ?, ?, ?, ?, ?, ?)",
        ("d_old", "plink_dup", "evt_dup", 100, "INR", "pay_old", "2026-08-01T10:00:00+00:00"),
    )
    conn.execute(
        "INSERT INTO webhook_recovery_outcomes VALUES (?, ?, ?, ?, ?, ?, ?)",
        ("d_new", "plink_dup", "evt_dup", 100, "INR", "pay_new", "2026-08-02T10:00:00+00:00"),
    )
    conn.commit()


def test_init_db_leaves_historical_duplicates_untouched(tmp_path) -> None:
    conn = sqlite3.connect(str(tmp_path / "legacy1.db"))
    conn.row_factory = sqlite3.Row
    try:
        _seed_legacy_duplicates(conn)
        # Per-request init_db must not raise on a legacy duplicate table and must
        # NOT delete anything (that is the deploy-time migration's job).
        init_db(conn)
        rows = conn.execute(
            "SELECT COUNT(*) AS c FROM webhook_recovery_outcomes "
            "WHERE payment_link_id = ?",
            ("plink_dup",),
        ).fetchone()
        assert rows["c"] == 2
    finally:
        conn.close()


def test_run_migrations_collapses_duplicates_and_installs_index(tmp_path) -> None:
    conn = sqlite3.connect(str(tmp_path / "legacy2.db"))
    conn.row_factory = sqlite3.Row
    try:
        _seed_legacy_duplicates(conn)
        init_db(conn)
        run_migrations(conn)
        rows = conn.execute(
            "SELECT * FROM webhook_recovery_outcomes WHERE payment_link_id = ?",
            ("plink_dup",),
        ).fetchall()
        assert len(rows) == 1
        assert rows[0]["delivery_id"] == "d_new"
        indexes = conn.execute(
            "PRAGMA index_list('webhook_recovery_outcomes')"
        ).fetchall()
        assert any(
            "ux_webhook_recovery_outcomes_link" in str(index[1]) for index in indexes
        )
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# S4 — dashboard LIKE search matches literal characters only
# ---------------------------------------------------------------------------


def test_like_search_is_literal_not_wildcard(tmp_path) -> None:
    conn = connect(str(tmp_path / "like.db"))
    try:
        init_db(conn)
        insert_payment_event(
            conn, make_event(event_id="evt_100%", customer_id="cust_100%")
        )
        insert_payment_event(conn, make_event(event_id="evt_1abc", customer_id="cust_1abc"))
        percent = list_payment_events(conn, limit=50, query="100%")
        assert {row["event_id"] for row in percent} == {"evt_100%"}
        literal = list_payment_events(conn, limit=50, query="evt_1abc")
        assert {row["event_id"] for row in literal} == {"evt_1abc"}
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# S5 — the single outbound provider sink is validated at the boundary
# ---------------------------------------------------------------------------


def test_omniroute_base_url_requires_https() -> None:
    with pytest.raises(OmniRouteError, match="scheme"):
        _validate_base_url("ftp://api.example.com/v1")
    with pytest.raises(OmniRouteError, match="scheme"):
        _validate_base_url("jdbc:sqlite://localhost/db")


def test_omniroute_base_url_rejects_plain_http_outside_loopback() -> None:
    with pytest.raises(OmniRouteError, match="localhost"):
        _validate_base_url("http://api.example.com/v1")
    assert _validate_base_url("http://localhost:8080/v1") == "http://localhost:8080/v1"
    assert _validate_base_url("http://127.0.0.1:8080/v1") == "http://127.0.0.1:8080/v1"


def test_omniroute_base_url_requires_a_host_and_normalizes() -> None:
    with pytest.raises(OmniRouteError, match="host"):
        _validate_base_url("https:///v1")
    assert (
        _validate_base_url("https://api.omniroute.ai/v1/")
        == "https://api.omniroute.ai/v1"
    )


def test_adapter_construction_validates_the_boundary() -> None:
    with pytest.raises(OmniRouteError):
        OmniRouteClassifier(
            api_key="k", model="m", base_url="http://evil.example.com/v1"
        )


# ---------------------------------------------------------------------------
# S6 — an unexpected classification failure never echoes its detail to a client
# ---------------------------------------------------------------------------


def _seed_event(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'cls.db'}")
    conn = connect(str(tmp_path / "cls.db"))
    try:
        init_db(conn)
        insert_payment_event(conn, make_event(event_id="evt_cls"))
    finally:
        conn.close()


def test_classify_failure_detail_is_stable_and_never_echoes_the_exception(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    _seed_event(monkeypatch, tmp_path)

    class BoomClassifier:
        def close(self) -> None:
            pass

        def classify(self, event):
            raise RuntimeError("secret https://internal.example.com/creds leaked")

    from app.routes import events as events_route

    app.dependency_overrides[events_route.get_classifier] = (
        lambda: _yield_once(BoomClassifier())
    )
    try:
        response = client.post("/events/evt_cls/classify")
    finally:
        app.dependency_overrides.clear()
    assert response.status_code == 500
    body = response.json()
    assert body["status"] == "classification_error"
    assert body["detail"] == (
        "unexpected classification failure; details recorded server-side for the operator"
    )
    assert "secret" not in json.dumps(body)
    assert "internal.example.com" not in json.dumps(body)


def _yield_once(instance):
    yield instance


# ---------------------------------------------------------------------------
# S7 — a concurrent duplicate ingestion is a benign duplicate, not an error
# ---------------------------------------------------------------------------


def test_concurrent_duplicate_insert_reports_duplicate(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    conn = connect(str(tmp_path / "dup.db"))
    try:
        init_db(conn)
        event = make_event(event_id="evt_race")

        import app.db as db_module
        import app.ingestion as ingestion_module

        real_insert = db_module.insert_payment_event

        def insert_after_race(c, e) -> None:
            # Mimic the lost-update race: a concurrent request committed the
            # exact row between the pre-check and this insert, turning ours
            # into an honest IntegrityError. The row is real and durable.
            real_insert(c, e)
            raise sqlite3.IntegrityError("UNIQUE constraint failed")

        monkeypatch.setattr(ingestion_module, "insert_payment_event", insert_after_race)
        raced = ingest_event(conn, event)
        assert raced.status == IngestionStatus.DUPLICATE
        assert "concurrent" in raced.detail

        # Subsequent idempotency is restored after the patch.
        later = ingest_event(conn, event)
        assert later.status == IngestionStatus.DUPLICATE
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# S8 — a released claim is retryable, never misreported as already-executed
# ---------------------------------------------------------------------------


def test_claim_conflict_with_no_claim_reports_released(tmp_path) -> None:
    conn = connect(str(tmp_path / "claim.db"))
    try:
        init_db(conn)
        result = _claim_conflict_result(conn, "evt_noclaim", "payment_link", None)
        assert result.status == STATUS_EXECUTION_CLAIM_RELEASED
    finally:
        conn.close()


def test_claim_conflict_with_held_claim_reports_in_progress(tmp_path) -> None:
    conn = connect(str(tmp_path / "claim2.db"))
    try:
        init_db(conn)
        conn.execute(
            "INSERT OR IGNORE INTO execution_claims "
            "(event_id, intervention, status, claimed_at) VALUES (?, ?, ?, ?)",
            ("evt_held", "payment_link", "claimed", "2026-08-27T12:00:00+00:00"),
        )
        conn.commit()
        result = _claim_conflict_result(conn, "evt_held", "payment_link", None)
        assert result.status == "execution_in_progress"
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# S9 — CORS is fail-closed (empty allow-list by default) and opt-in
# ---------------------------------------------------------------------------


def test_cors_allow_list_is_empty_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("RECOVERYOS_CORS_ORIGINS", raising=False)
    assert get_cors_origins() == []


def test_cors_allow_list_parses_comma_separated_origins(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(
        "RECOVERYOS_CORS_ORIGINS", "https://a.example.com, https://b.example.com ,"
    )
    assert get_cors_origins() == [
        "https://a.example.com",
        "https://b.example.com",
    ]


# ---------------------------------------------------------------------------
# S10 — dependencies are pinned so builds cannot silently drift
# ---------------------------------------------------------------------------


def test_requirements_are_pinned_exactly() -> None:
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    with open(os.path.join(root, "requirements.txt"), encoding="utf-8") as handle:
        lines = [
            line.strip()
            for line in handle
            if line.strip() and not line.strip().startswith("#")
        ]
    pinned = {
        line.split("==", 1)[0].split("[", 1)[0]: line for line in lines if "==" in line
    }
    for package in ("fastapi", "uvicorn", "pytest", "httpx", "python-dotenv", "razorpay"):
        assert package in pinned, f"{package} is not pinned"
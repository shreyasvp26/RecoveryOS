"""Phase 13: Why Not Recovered — trace-level accountability.

Verifies that the persisted decision chain (via ``build_event_trace``) reports
an honest, evidence-only reason for every category of non-recovery, using only
information actually available in the existing domain model:

- fraud, terminal, retry-limit, cooldown, duplicate, spend-cap  -> DENY +
  the specific locked policy denial reason (policy_blocked)
- a FAILED execution                                           -> EXECUTED with
  execution_status=FAILED (a visible failure, never a silent success)
- no classification                                             -> not_classified
- classified but neither executed nor denied                    -> the honest
  NO_EXECUTION_RECORDED state (never fabricated)

These reuse the identical seeding helpers from the adversarial policy proof and
assert the *trace summary* end to end.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from app.classification import ClassificationResult
from app.dashboard import build_event_trace
from app.db import (
    insert_classification_result,
    insert_intervention_attempt,
    insert_payment_event,
)
from app.execution_service import execute_event
from app.models import CustomerHistory, PaymentEvent
from app.policy import InterventionAttempt, PolicyConfig

NOW = datetime(2026, 8, 27, 13, 0, tzinfo=timezone.utc)
_DEFAULT_CONFIG = PolicyConfig()


def _event(event_id: str, customer_id: str = "cust_w", risk_flag: str = "normal") -> PaymentEvent:
    return PaymentEvent.from_dict(
        {
            "event_id": event_id,
            "order_id": f"order_{event_id}",
            "payment_id": f"pay_{event_id}",
            "customer_id": customer_id,
            "amount_paise": 10000,
            "currency": "INR",
            "payment_method": "card",
            "failure_reason": "bank_timeout",
            "bank": "HDFC",
            "risk_flag": risk_flag,
            "customer_history": CustomerHistory(4, 1, True).to_dict(),
            "timestamp": "2026-08-27T12:00:00+00:00",
        }
    )


def _classification(event_id: str, root: str = "transient", candidates=None) -> ClassificationResult:
    return ClassificationResult.from_dict(
        {
            "event_id": event_id,
            "root_cause_category": root,
            "confidence": 0.9,
            "reasoning": "why-not-recovered test",
            "candidate_interventions": candidates or ["retry_delayed", "payment_link"],
        }
    )


def _seed(conn, event: PaymentEvent, classification: ClassificationResult) -> None:
    insert_payment_event(conn, event)
    insert_classification_result(conn, classification)


def _attempt(conn, event_id: str, customer_id: str, status: str = "successful", when=None, cost_paise: int = 0) -> None:
    insert_intervention_attempt(
        conn,
        InterventionAttempt(
            event_id=event_id,
            intervention="retry_delayed",
            customer_id=customer_id,
            cost_paise=cost_paise,
            attempted_at=(when or NOW).isoformat(),
            status=status,
        ),
    )


def _run(conn, event_id: str, config: PolicyConfig = _DEFAULT_CONFIG):
    return execute_event(conn, event_id, NOW, config, None)


def _summary(conn, event_id: str) -> dict:
    return build_event_trace(conn, event_id)["summary"]


# -- DENY (policy blocked) categories ----------------------------------------
def test_why_not_recovered_fraud(db_conn) -> None:
    _seed(db_conn, _event("evt_w_fraud", risk_flag="fraud_suspect"), _classification("evt_w_fraud"))
    _run(db_conn, "evt_w_fraud")
    s = _summary(db_conn, "evt_w_fraud")
    assert s["final_decision"] == "DENY"
    assert s["execution_state"] == "POLICY_BLOCKED"
    assert "fraud_protection" in s["denial_reasons"]


def test_why_not_recovered_terminal(db_conn) -> None:
    _seed(db_conn, _event("evt_w_term"), _classification("evt_w_term", root="terminal"))
    _run(db_conn, "evt_w_term")
    s = _summary(db_conn, "evt_w_term")
    assert s["execution_state"] == "POLICY_BLOCKED"
    assert "terminal_failure" in s["denial_reasons"]


def test_why_not_recovered_retry_limit(db_conn) -> None:
    _attempt(db_conn, "evt_w_l1", "cust_w", when=NOW - timedelta(hours=1))
    _attempt(db_conn, "evt_w_l2", "cust_w", when=NOW - timedelta(hours=2))
    _seed(db_conn, _event("evt_w_limit"), _classification("evt_w_limit"))
    _run(db_conn, "evt_w_limit")
    s = _summary(db_conn, "evt_w_limit")
    assert s["execution_state"] == "POLICY_BLOCKED"
    assert "customer_intervention_limit_exceeded" in s["denial_reasons"]


def test_why_not_recovered_cooldown(db_conn) -> None:
    _attempt(db_conn, "evt_w_cd", "cust_w", status="failed", when=NOW - timedelta(minutes=5))
    _seed(db_conn, _event("evt_w_cd"), _classification("evt_w_cd"))
    _run(db_conn, "evt_w_cd")
    s = _summary(db_conn, "evt_w_cd")
    assert s["execution_state"] == "POLICY_BLOCKED"
    assert "event_cooldown_active" in s["denial_reasons"]


def test_why_not_recovered_duplicate(db_conn) -> None:
    _attempt(db_conn, "evt_w_dup", "cust_w", status="successful", when=NOW)
    _seed(db_conn, _event("evt_w_dup"), _classification("evt_w_dup"))
    _run(db_conn, "evt_w_dup")
    s = _summary(db_conn, "evt_w_dup")
    assert s["execution_state"] == "POLICY_BLOCKED"
    assert "duplicate_intervention" in s["denial_reasons"]


def test_why_not_recovered_spend_cap(db_conn) -> None:
    config = PolicyConfig(
        daily_spend_cap_paise=1000,
        intervention_cost_paise={"retry_delayed": 600, "payment_link": 600},
    )
    _attempt(db_conn, "evt_w_spend_other", "cust_w", when=NOW - timedelta(hours=1), cost_paise=600)
    _seed(db_conn, _event("evt_w_spend"), _classification("evt_w_spend"))
    _run(db_conn, "evt_w_spend", config)
    s = _summary(db_conn, "evt_w_spend")
    assert s["execution_state"] == "POLICY_BLOCKED"
    assert "spend_cap_exceeded" in s["denial_reasons"]


# -- visible execution failure -----------------------------------------------
def test_why_not_recovered_failed_execution_is_visible(db_conn) -> None:
    # payment_link selected without a configured Razorpay client -> a real
    # FAILED execution outcome is persisted (configuration_missing), which the
    # trace reports as an explicit failure, not a fabricated success.
    _seed(
        db_conn,
        _event("evt_w_fail"),
        _classification("evt_w_fail", candidates=["payment_link"]),
    )
    _run(db_conn, "evt_w_fail")
    s = _summary(db_conn, "evt_w_fail")
    assert s["final_decision"] == "ALLOW"
    assert s["execution_state"] == "EXECUTED"
    assert s["execution_status"] == "FAILED"


# -- classification failure --------------------------------------------------
def test_why_not_recovered_not_classified(db_conn) -> None:
    insert_payment_event(db_conn, _event("evt_w_nocls"))
    # No classification persisted.
    s = _summary(db_conn, "evt_w_nocls")
    assert s["final_decision"] == "not_classified"
    assert s["execution_state"] == "NOT_CLASSIFIED"


# -- classified but never run -> honest NO_EXECUTION_RECORDED ----------------
def test_why_not_recovered_no_execution_recorded(db_conn) -> None:
    # Classified but never run through the executor: no decision and no
    # execution exist, so the trace reports the honest NO_EXECUTION_RECORDED
    # state rather than claiming "policy denied".
    _seed(db_conn, _event("evt_w_noexec"), _classification("evt_w_noexec"))
    s = _summary(db_conn, "evt_w_noexec")
    assert s["final_decision"] == "no_action"
    assert s["execution_state"] == "NO_EXECUTION_RECORDED"

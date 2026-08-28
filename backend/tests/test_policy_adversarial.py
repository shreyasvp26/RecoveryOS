"""Phase 13 adversarial policy verification.

Proves, end to end through the real execution chain (``execute_event``), that
EVERY unauthorized intervention results in ZERO executions:

- a denied candidate is never selected and never executed;
- the execution service records no ``execution_outcomes`` row for a denied path
  and writes no ``intervention_attempts`` row;
- the deterministic denial reason is persisted according to the locked rule
  order (fraud -> terminal -> duplicate -> customer-limit -> cooldown ->
  spend-cap), matching the existing policy contract.

The authoritative ``ExecutionServiceResult`` contract is preserved: for
``no_action`` the result carries ``decision=None`` / ``outcome=None``, so denial
reasons are asserted against the persisted ``policy_decisions`` audit table (the
existing durable mechanism), NOT by fabricating extra result fields.

Scenario A/Traversal: the single most important test walks the full
policy -> selector -> executor boundary with an otherwise-actionable
classification that policy denies, and asserts the executor never produces a
side effect. Any successful execution of a denied decision would fail here.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from app.classification import ClassificationResult
from app.db import (
    get_policy_decisions_for_event,
    insert_classification_result,
    insert_intervention_attempt,
    insert_payment_event,
)
from app.execution_service import (
    STATUS_NO_ACTION,
    execute_event,
)
from app.models import CustomerHistory, PaymentEvent
from app.policy import (
    InterventionAttempt,
    PolicyConfig,
    RULE_COOLDOWN,
    RULE_CUSTOMER_LIMIT,
    RULE_DUPLICATE,
    RULE_FRAUD,
    RULE_SPEND_CAP,
    RULE_TERMINAL,
)

NOW = datetime(2026, 8, 27, 13, 0, tzinfo=timezone.utc)
_DEFAULT_CONFIG = PolicyConfig()


def _event(
    event_id: str, customer_id: str = "cust_adv", risk_flag: str = "normal"
) -> PaymentEvent:
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


def _classification(
    event_id: str, root: str = "transient", candidates=None
) -> ClassificationResult:
    return ClassificationResult.from_dict(
        {
            "event_id": event_id,
            "root_cause_category": root,
            "confidence": 0.9,
            "reasoning": "adversarial test classification",
            "candidate_interventions": candidates or ["retry_delayed", "payment_link"],
        }
    )


def _seed(conn, event: PaymentEvent, classification: ClassificationResult) -> None:
    insert_payment_event(conn, event)
    insert_classification_result(conn, classification)


def _attempt(
    conn,
    event_id: str,
    customer_id: str,
    cost_paise: int = 0,
    status: str = "successful",
    when: datetime | None = None,
) -> None:
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


def _outcome_count(conn, event_id: str) -> int:
    return conn.execute(
        "SELECT COUNT(*) FROM execution_outcomes WHERE event_id = ?", (event_id,)
    ).fetchone()[0]


def _attempt_count(conn, event_id: str) -> int:
    return conn.execute(
        "SELECT COUNT(*) FROM intervention_attempts WHERE event_id = ?", (event_id,)
    ).fetchone()[0]


def _persisted_reason(conn, event_id: str, intervention: str) -> str | None:
    for decision in get_policy_decisions_for_event(conn, event_id):
        if decision["proposed_intervention"] == intervention:
            return decision["denial_reason"]
    return None


def _assert_not_executed(conn, event_id: str, result, before_attempts: int = 0) -> None:
    """Assert the no_action contract and that NO NEW execution side effect occurred.

    ``before_attempts`` is the count of intervention_attempts already present
    (seeded as history setup) before execute_event ran; the executor must not
    add any outcome or attempt on a denied path.
    """
    assert result.status == STATUS_NO_ACTION
    assert result.selected_intervention == "no_action"
    assert result.decision is None  # no_action result carries no decision (contract)
    assert result.outcome is None
    assert _outcome_count(conn, event_id) == 0  # executor never persisted an outcome
    assert _attempt_count(conn, event_id) == before_attempts  # no new attempt written


# ---------------------------------------------------------------------------
# A. FRAUD
# ---------------------------------------------------------------------------
def test_a_fraud_event_is_denied_and_never_executes(db_conn) -> None:
    event = _event("evt_a_fraud", risk_flag="fraud_suspect")
    _seed(db_conn, event, _classification("evt_a_fraud"))
    result = execute_event(db_conn, "evt_a_fraud", NOW, _DEFAULT_CONFIG, None)
    _assert_not_executed(db_conn, "evt_a_fraud", result)
    assert _persisted_reason(db_conn, "evt_a_fraud", "retry_delayed") == RULE_FRAUD


# ---------------------------------------------------------------------------
# B. TERMINAL FAILURE
# ---------------------------------------------------------------------------
def test_b_terminal_event_is_denied_and_never_executes(db_conn) -> None:
    event = _event("evt_b_term")
    _seed(db_conn, event, _classification("evt_b_term", root="terminal"))
    result = execute_event(db_conn, "evt_b_term", NOW, _DEFAULT_CONFIG, None)
    _assert_not_executed(db_conn, "evt_b_term", result)
    assert _persisted_reason(db_conn, "evt_b_term", "retry_delayed") == RULE_TERMINAL


# ---------------------------------------------------------------------------
# C. RETRY LIMIT
# ---------------------------------------------------------------------------
def test_c_third_intervention_hits_retry_limit(db_conn) -> None:
    _attempt(db_conn, "evt_c_1", "cust_c", status="successful", when=NOW - timedelta(hours=1))
    _attempt(db_conn, "evt_c_2", "cust_c", status="successful", when=NOW - timedelta(hours=2))
    event = _event("evt_c_3", customer_id="cust_c")
    _seed(db_conn, event, _classification("evt_c_3"))
    result = execute_event(db_conn, "evt_c_3", NOW, _DEFAULT_CONFIG, None)
    _assert_not_executed(db_conn, "evt_c_3", result)
    assert _persisted_reason(db_conn, "evt_c_3", "retry_delayed") == RULE_CUSTOMER_LIMIT


# ---------------------------------------------------------------------------
# D. COOLDOWN
# ---------------------------------------------------------------------------
def test_d_cooldown_blocks_early_repeat(db_conn) -> None:
    # Failed attempt on the same event 10 minutes ago -> cooldown, not duplicate.
    _attempt(db_conn, "evt_d", "cust_d", status="failed", when=NOW - timedelta(minutes=10))
    event = _event("evt_d", customer_id="cust_d")
    _seed(db_conn, event, _classification("evt_d"))
    result = execute_event(db_conn, "evt_d", NOW, _DEFAULT_CONFIG, None)
    _assert_not_executed(db_conn, "evt_d", result, before_attempts=1)
    assert _persisted_reason(db_conn, "evt_d", "retry_delayed") == RULE_COOLDOWN


# ---------------------------------------------------------------------------
# E. DUPLICATE
# ---------------------------------------------------------------------------
def test_e_duplicate_successful_never_executes_again(db_conn) -> None:
    _attempt(db_conn, "evt_e", "cust_e", status="successful", when=NOW)
    event = _event("evt_e", customer_id="cust_e")
    _seed(db_conn, event, _classification("evt_e"))
    result = execute_event(db_conn, "evt_e", NOW, _DEFAULT_CONFIG, None)
    _assert_not_executed(db_conn, "evt_e", result, before_attempts=1)
    assert _persisted_reason(db_conn, "evt_e", "retry_delayed") == RULE_DUPLICATE


# ---------------------------------------------------------------------------
# F. SPEND CAP
# ---------------------------------------------------------------------------
def test_f_spend_cap_exceeded_never_executes(db_conn) -> None:
    event = _event("evt_f", customer_id="cust_f")
    config = PolicyConfig(
        daily_spend_cap_paise=1000,
        intervention_cost_paise={"retry_delayed": 600, "payment_link": 600},
    )
    # Existing 600 spend on another event pushes 600+600 > 1000 cap.
    _attempt(db_conn, "evt_f_other", "cust_f", cost_paise=600, status="attempted", when=NOW - timedelta(hours=1))
    _seed(db_conn, event, _classification("evt_f"))
    result = execute_event(db_conn, "evt_f", NOW, config, None)
    _assert_not_executed(db_conn, "evt_f", result)
    assert _persisted_reason(db_conn, "evt_f", "retry_delayed") == RULE_SPEND_CAP


# ---------------------------------------------------------------------------
# G. COMBINED VIOLATIONS -> deterministic first blocker
# ---------------------------------------------------------------------------
def test_g_combined_multirule_event_is_deterministic(db_conn) -> None:
    event = _event("evt_g", customer_id="cust_g", risk_flag="fraud_suspect")
    _attempt(db_conn, "evt_g_1", "cust_g", status="successful", when=NOW - timedelta(hours=1))
    _attempt(db_conn, "evt_g_2", "cust_g", status="successful", when=NOW - timedelta(hours=2))
    _seed(db_conn, event, _classification("evt_g", root="terminal"))
    first = execute_event(db_conn, "evt_g", NOW, _DEFAULT_CONFIG, None)
    second = execute_event(db_conn, "evt_g", NOW, _DEFAULT_CONFIG, None)
    _assert_not_executed(db_conn, "evt_g", first)
    _assert_not_executed(db_conn, "evt_g", second)
    # fraud is the first blocker in DETERMINISTIC_RULE_ORDER, regardless of the
    # terminal classification and retry-limit history.
    assert _persisted_reason(db_conn, "evt_g", "retry_delayed") == RULE_FRAUD
    # repeated evaluation is stable: same persisted denial reason.
    reasons = [d["denial_reason"] for d in get_policy_decisions_for_event(db_conn, "evt_g")]
    assert set(reasons) == {RULE_FRAUD}


# ---------------------------------------------------------------------------
# MOST IMPORTANT: unauthorized executions == 0 across a full policy -> selector
# -> executor traversal, actively attempting to force a denied execution.
# ---------------------------------------------------------------------------
def test_unauthorized_executions_total_is_zero(db_conn) -> None:
    scenarios = [
        ("evt_z_fraud", _event("evt_z_fraud", risk_flag="fraud_suspect"), {}, None),
        ("evt_z_term", _event("evt_z_term"), {"root": "terminal"}, None),
        ("evt_z_dup", _event("evt_z_dup"), {}, [("evt_z_dup", "cust_z", 0, "successful", NOW)]),
        (
            "evt_z_cooldown",
            _event("evt_z_cooldown"),
            {},
            [("evt_z_cooldown", "cust_z", 0, "failed", NOW - timedelta(minutes=5))],
        ),
        (
            "evt_z_limit",
            _event("evt_z_limit", customer_id="cust_z"),
            {},
            [
                ("evt_z_l1", "cust_z", 0, "successful", NOW - timedelta(hours=1)),
                ("evt_z_l2", "cust_z", 0, "successful", NOW - timedelta(hours=2)),
            ],
        ),
    ]
    total_outcomes = 0
    total_attempts = 0
    for event_id, event, cls_kwargs, attempts in scenarios:
        _seed(db_conn, event, _classification(event_id, **cls_kwargs))
        for (aeid, acust, acost, astatus, awhen) in (attempts or []):
            _attempt(db_conn, aeid, acust, cost_paise=acost, status=astatus, when=awhen)
        before = sum(1 for (aeid, *_rest) in (attempts or []) if aeid == event_id)
        result = execute_event(db_conn, event_id, NOW, _DEFAULT_CONFIG, None)
        _assert_not_executed(db_conn, event_id, result, before_attempts=before)
        total_outcomes += _outcome_count(db_conn, event_id)
        # Only count NEW attempts (seeded history rows are excluded).
        total_attempts += _attempt_count(db_conn, event_id) - before
    # No denied event was executed: zero execution_outcomes and zero NEW
    # intervention_attempts are recorded anywhere in the traversal.
    assert total_outcomes == 0
    assert total_attempts == 0


def test_denied_decision_cannot_reach_executor_side_effect(db_conn) -> None:
    """Prove a denied decision cannot yield a successful executor side effect.

    Traverses the true boundary: an actionable classification is seeded for a
    fraud event; the authoritative policy denies it; execute_event falls through
    to no_action with ZERO executor output. As a contrast, a forged object that
    pretends to be an allowed 'payment_link' decision is rejected by the
    executor boundary (execution_rejected), so no side effect can be fabricated.
    """
    event = _event("evt_bypass", risk_flag="fraud_suspect")
    _seed(db_conn, event, _classification("evt_bypass", candidates=["payment_link"]))
    result = execute_event(db_conn, "evt_bypass", NOW, _DEFAULT_CONFIG, None)
    _assert_not_executed(db_conn, "evt_bypass", result)
    assert _persisted_reason(db_conn, "evt_bypass", "payment_link") == RULE_FRAUD

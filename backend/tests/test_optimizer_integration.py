"""Phase 16 integration tests: classifier -> policy -> optimizer -> executor.

Exercises the REAL chain through ``execute_event`` against a real SQLite
database and the real policy engine, executor, and economic optimizer. The
adversarial cases drive each V1 policy rule to a denial while the denied
intervention is the economically attractive one, and assert that it neither
executes nor leaves a side effect.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from app.classification import ClassificationResult
from app.db import (
    get_policy_decisions_for_event,
    insert_classification_result,
    insert_intervention_attempt,
    insert_payment_event,
)
from app.economics import DEFAULT_ECONOMIC_MODEL
from app.execution_service import (
    SELECTION_V1_FIXED_PRIORITY,
    SELECTION_V2_ECONOMIC,
    STATUS_EXECUTION_FAILED,
    STATUS_EXECUTION_SUCCESS,
    STATUS_NO_ACTION,
    SelectionStrategyError,
    execute_event,
)
from app.models import CustomerHistory, PaymentEvent
from app.optimizer import REASON_MAX_EXPECTED_VALUE, REASON_NO_ALLOWED_CANDIDATE
from app.policy import (
    RULE_COOLDOWN,
    RULE_CUSTOMER_LIMIT,
    RULE_DUPLICATE,
    RULE_FRAUD,
    RULE_SPEND_CAP,
    RULE_TERMINAL,
    InterventionAttempt,
    PolicyConfig,
)

NOW = datetime(2026, 8, 27, 13, 0, tzinfo=timezone.utc)
DEFAULT_CONFIG = PolicyConfig()

# The full executable taxonomy, so the optimizer always has a real choice.
ALL_CANDIDATES = [
    "retry_immediate",
    "retry_delayed",
    "payment_link",
    "reminder",
    "alternate_method_prompt",
]


def _event(
    event_id: str,
    customer_id: str = "cust_opt",
    risk_flag: str = "normal",
    failure_reason: str = "bank_timeout",
    amount_paise: int = 10_000,
) -> PaymentEvent:
    return PaymentEvent.from_dict(
        {
            "event_id": event_id,
            "order_id": f"order_{event_id}",
            "payment_id": f"pay_{event_id}",
            "customer_id": customer_id,
            "amount_paise": amount_paise,
            "currency": "INR",
            "payment_method": "card",
            "failure_reason": failure_reason,
            "bank": "HDFC",
            "risk_flag": risk_flag,
            "customer_history": CustomerHistory(4, 1, True).to_dict(),
            "timestamp": "2026-08-27T12:00:00+00:00",
        }
    )


def _classification(
    event_id: str, root: str = "transient", candidates: list[str] | None = None
) -> ClassificationResult:
    return ClassificationResult.from_dict(
        {
            "event_id": event_id,
            "root_cause_category": root,
            "confidence": 0.9,
            "reasoning": "phase 16 integration test classification",
            "candidate_interventions": candidates or list(ALL_CANDIDATES),
        }
    )


def _seed(conn, event: PaymentEvent, classification: ClassificationResult) -> None:
    insert_payment_event(conn, event)
    insert_classification_result(conn, classification)


def _run(conn, event_id: str, config: PolicyConfig = DEFAULT_CONFIG, **kwargs):
    return execute_event(
        conn, event_id, NOW, config, razorpay_client=None, **kwargs
    )


def _counts(conn, event_id: str) -> tuple[int, int]:
    outcomes = conn.execute(
        "SELECT COUNT(*) FROM execution_outcomes WHERE event_id = ?", (event_id,)
    ).fetchone()[0]
    attempts = conn.execute(
        "SELECT COUNT(*) FROM intervention_attempts WHERE event_id = ?", (event_id,)
    ).fetchone()[0]
    return outcomes, attempts


def _denial_reasons(conn, event_id: str) -> set[str]:
    return {
        decision["denial_reason"]
        for decision in get_policy_decisions_for_event(conn, event_id)
        if decision["denial_reason"] is not None
    }


# ---------------------------------------------------------------------------
# The happy path: economics decides, the executor executes
# ---------------------------------------------------------------------------


def test_the_optimizer_drives_selection_through_the_real_chain(db_conn) -> None:
    event_id = "evt_int_happy"
    _seed(db_conn, _event(event_id), _classification(event_id))
    result = _run(db_conn, event_id)

    assert result.status == STATUS_EXECUTION_SUCCESS
    # transient bank_timeout on a card: a delayed retry is the economic choice.
    assert result.selected_intervention == "retry_delayed"
    assert result.outcome.intervention == "retry_delayed"
    assert result.outcome.status == "SUCCESS"
    assert result.decision.allowed is True
    assert result.decision.proposed_intervention == "retry_delayed"

    trace = result.optimizer_decision
    assert trace is not None
    assert trace.selection_reason == REASON_MAX_EXPECTED_VALUE
    assert set(trace.allowed_candidates) == set(ALL_CANDIDATES)
    # The selection really is the expected-value maximum of the reported trace.
    best = max(trace.evaluations, key=lambda e: e.expected_value_paise)
    assert best.intervention == result.selected_intervention


def test_the_economic_trace_is_coherent_and_hand_checkable(db_conn) -> None:
    event_id = "evt_int_trace"
    _seed(db_conn, _event(event_id), _classification(event_id))
    trace = _run(db_conn, event_id).optimizer_decision

    assert len(trace.evaluations) == len(ALL_CANDIDATES)
    for evaluation in trace.evaluations:
        economics = DEFAULT_ECONOMIC_MODEL.for_intervention(evaluation.intervention)
        assert evaluation.amount_paise == 10_000
        assert 0 <= evaluation.estimated_probability_bps <= 10_000
        assert evaluation.intervention_cost_paise == economics.cost_paise
        assert evaluation.expected_recovered_value_paise == (
            evaluation.amount_paise
            * evaluation.estimated_probability_bps
            // 10_000
        )
        assert evaluation.expected_value_paise == (
            evaluation.expected_recovered_value_paise
            - evaluation.intervention_cost_paise
            - evaluation.friction_cost_paise
        )


def test_the_economic_choice_can_differ_from_the_v1_fixed_priority_choice(
    db_conn,
) -> None:
    """V2 is an economic decision, not a relabelled priority list.

    An expired card cannot be retried into life, so economics routes the
    customer to a fresh checkout, whereas V1's fixed priority still picks
    retry_delayed purely because it sits at the top of the list.
    """
    event_id = "evt_int_divergent"
    _seed(
        db_conn,
        _event(event_id, failure_reason="expired_card"),
        _classification(event_id, root="customer_action_needed"),
    )
    v2 = _run(db_conn, event_id, selection_strategy=SELECTION_V2_ECONOMIC)
    assert v2.selected_intervention == "payment_link"
    # Both retry paths are economically last: the instrument itself is dead.
    ranked = [e.intervention for e in v2.optimizer_decision.evaluations]
    assert ranked[-2:] == ["retry_delayed", "retry_immediate"]

    other_id = "evt_int_divergent_v1"
    _seed(
        db_conn,
        _event(other_id, customer_id="cust_other", failure_reason="expired_card"),
        _classification(other_id, root="customer_action_needed"),
    )
    v1 = _run(db_conn, other_id, selection_strategy=SELECTION_V1_FIXED_PRIORITY)
    assert v1.selected_intervention == "retry_delayed"
    assert v1.optimizer_decision is None


def test_execution_is_deterministic_across_identical_runs(db_conn, tmp_path) -> None:
    from app.db import connect, init_db

    selections = []
    for index in range(3):
        conn = connect(str(tmp_path / f"determinism_{index}.db"))
        init_db(conn)
        try:
            event_id = "evt_int_determinism"
            _seed(conn, _event(event_id), _classification(event_id))
            result = _run(conn, event_id)
            selections.append(
                (
                    result.selected_intervention,
                    result.optimizer_decision.to_dict(),
                )
            )
        finally:
            conn.close()
    assert selections[0] == selections[1] == selections[2]


# ---------------------------------------------------------------------------
# Adversarial: every policy rule resists economic pressure
# ---------------------------------------------------------------------------


def test_fraud_denies_everything_and_nothing_executes(db_conn) -> None:
    event_id = "evt_int_fraud"
    _seed(
        db_conn,
        _event(event_id, risk_flag="fraud_suspect", amount_paise=5_000_00),
        _classification(event_id),
    )
    result = _run(db_conn, event_id)

    assert result.status == STATUS_NO_ACTION
    assert result.selected_intervention == "no_action"
    assert result.outcome is None
    assert result.optimizer_decision.allowed_candidates == ()
    assert result.optimizer_decision.evaluations == ()
    assert result.optimizer_decision.selection_reason == REASON_NO_ALLOWED_CANDIDATE
    assert _counts(db_conn, event_id) == (0, 0)
    assert _denial_reasons(db_conn, event_id) == {RULE_FRAUD}


def test_terminal_root_cause_denies_everything_and_nothing_executes(db_conn) -> None:
    event_id = "evt_int_terminal"
    _seed(
        db_conn,
        _event(event_id, amount_paise=5_000_00),
        _classification(event_id, root="terminal"),
    )
    result = _run(db_conn, event_id)

    assert result.status == STATUS_NO_ACTION
    assert _counts(db_conn, event_id) == (0, 0)
    assert _denial_reasons(db_conn, event_id) == {RULE_TERMINAL}


def test_a_prior_success_denies_everything_and_nothing_executes(db_conn) -> None:
    event_id = "evt_int_duplicate"
    _seed(db_conn, _event(event_id), _classification(event_id))
    insert_intervention_attempt(
        db_conn,
        InterventionAttempt(
            event_id=event_id,
            intervention="retry_delayed",
            customer_id="cust_opt",
            cost_paise=0,
            attempted_at=(NOW - timedelta(hours=1)).isoformat(),
            status="successful",
        ),
    )
    result = _run(db_conn, event_id)

    assert result.status == STATUS_NO_ACTION
    assert _counts(db_conn, event_id) == (0, 1)  # only the seeded history row
    assert _denial_reasons(db_conn, event_id) == {RULE_DUPLICATE}


def test_the_customer_limit_denies_everything_and_nothing_executes(db_conn) -> None:
    event_id = "evt_int_customer_limit"
    _seed(db_conn, _event(event_id), _classification(event_id))
    for index in range(2):
        insert_intervention_attempt(
            db_conn,
            InterventionAttempt(
                event_id=f"evt_other_{index}",
                intervention="retry_delayed",
                customer_id="cust_opt",
                cost_paise=0,
                attempted_at=(NOW - timedelta(hours=index + 1)).isoformat(),
                status="failed",
            ),
        )
    result = _run(db_conn, event_id)

    assert result.status == STATUS_NO_ACTION
    assert _counts(db_conn, event_id) == (0, 0)
    assert _denial_reasons(db_conn, event_id) == {RULE_CUSTOMER_LIMIT}


def test_the_cooldown_denies_everything_and_nothing_executes(db_conn) -> None:
    event_id = "evt_int_cooldown"
    _seed(db_conn, _event(event_id), _classification(event_id))
    insert_intervention_attempt(
        db_conn,
        InterventionAttempt(
            event_id=event_id,
            intervention="retry_delayed",
            customer_id="cust_opt",
            cost_paise=0,
            attempted_at=(NOW - timedelta(minutes=5)).isoformat(),
            status="failed",
        ),
    )
    result = _run(db_conn, event_id)

    assert result.status == STATUS_NO_ACTION
    assert _counts(db_conn, event_id) == (0, 1)
    assert _denial_reasons(db_conn, event_id) == {RULE_COOLDOWN}


def test_the_spend_cap_denies_everything_and_nothing_executes(db_conn) -> None:
    event_id = "evt_int_spend_cap"
    _seed(db_conn, _event(event_id), _classification(event_id))
    config = PolicyConfig(
        daily_spend_cap_paise=100,
        intervention_cost_paise={name: 600 for name in ALL_CANDIDATES},
    )
    result = _run(db_conn, event_id, config=config)

    assert result.status == STATUS_NO_ACTION
    assert _counts(db_conn, event_id) == (0, 0)
    assert _denial_reasons(db_conn, event_id) == {RULE_SPEND_CAP}


def test_a_denied_high_value_candidate_cannot_beat_an_allowed_low_value_one(
    db_conn,
) -> None:
    """The headline invariant, driven by a real policy denial.

    The spend cap prices payment_link out of the authorized set while leaving
    the zero-cost retries authorized. payment_link would otherwise be a strong
    economic candidate for this customer-action failure, but it is denied and
    must therefore be neither evaluated nor selected.
    """
    event_id = "evt_int_denied_high_value"
    _seed(
        db_conn,
        _event(event_id, failure_reason="expired_card", amount_paise=2_000_000),
        _classification(event_id, root="customer_action_needed"),
    )
    config = PolicyConfig(
        daily_spend_cap_paise=0,
        intervention_cost_paise={
            "payment_link": 100,
            "alternate_method_prompt": 100,
            "reminder": 100,
            "retry_delayed": 0,
            "retry_immediate": 0,
        },
    )
    result = _run(db_conn, event_id, config=config)

    trace = result.optimizer_decision
    assert set(trace.allowed_candidates) == {"retry_delayed", "retry_immediate"}
    assert result.selected_intervention in {"retry_delayed", "retry_immediate"}
    # The denied candidates were never even scored.
    evaluated = {evaluation.intervention for evaluation in trace.evaluations}
    assert evaluated == {"retry_delayed", "retry_immediate"}
    assert "payment_link" not in evaluated
    assert "alternate_method_prompt" not in evaluated
    # ... yet they were genuinely considered and genuinely denied.
    assert "payment_link" in trace.candidates_considered
    assert RULE_SPEND_CAP in _denial_reasons(db_conn, event_id)


def test_the_executor_only_ever_runs_the_selected_authorized_intervention(
    db_conn,
) -> None:
    event_id = "evt_int_authorization"
    _seed(db_conn, _event(event_id), _classification(event_id))
    result = _run(db_conn, event_id)

    row = db_conn.execute(
        "SELECT intervention FROM execution_outcomes WHERE event_id = ?", (event_id,)
    ).fetchall()
    assert [item["intervention"] for item in row] == [result.selected_intervention]
    attempts = db_conn.execute(
        "SELECT intervention FROM intervention_attempts WHERE event_id = ?",
        (event_id,),
    ).fetchall()
    assert [item["intervention"] for item in attempts] == [
        result.selected_intervention
    ]


def test_payment_link_without_a_client_fails_explicitly_and_is_recorded(
    db_conn,
) -> None:
    """The executor's provider boundary is unchanged by economic selection."""
    event_id = "evt_int_payment_link"
    _seed(
        db_conn,
        _event(event_id, failure_reason="insufficient_funds"),
        _classification(
            event_id, root="customer_action_needed", candidates=["payment_link"]
        ),
    )
    result = _run(db_conn, event_id)

    assert result.status == STATUS_EXECUTION_FAILED
    assert result.selected_intervention == "payment_link"
    assert result.outcome.execution_mode == "REAL_RAZORPAY"
    assert result.outcome.status == "FAILED"
    assert "configuration_missing" in result.outcome.detail


# ---------------------------------------------------------------------------
# Selection strategy plumbing
# ---------------------------------------------------------------------------


def test_the_default_strategy_is_the_v2_economic_optimizer(db_conn) -> None:
    event_id = "evt_int_default_strategy"
    _seed(db_conn, _event(event_id), _classification(event_id))
    assert _run(db_conn, event_id).optimizer_decision is not None


def test_an_unknown_strategy_is_rejected_and_nothing_executes(db_conn) -> None:
    event_id = "evt_int_bad_strategy"
    _seed(db_conn, _event(event_id), _classification(event_id))
    with pytest.raises(SelectionStrategyError):
        _run(db_conn, event_id, selection_strategy="v3_vibes")
    assert _counts(db_conn, event_id) == (0, 0)


def test_neither_strategy_can_widen_the_authorized_set(db_conn) -> None:
    """Strategy affects ranking only; a fraud event is inert under both."""
    for index, strategy in enumerate(
        (SELECTION_V1_FIXED_PRIORITY, SELECTION_V2_ECONOMIC)
    ):
        event_id = f"evt_int_strategy_fraud_{index}"
        _seed(
            db_conn,
            _event(event_id, customer_id=f"cust_{index}", risk_flag="fraud_suspect"),
            _classification(event_id),
        )
        result = _run(db_conn, event_id, selection_strategy=strategy)
        assert result.status == STATUS_NO_ACTION
        assert _counts(db_conn, event_id) == (0, 0)

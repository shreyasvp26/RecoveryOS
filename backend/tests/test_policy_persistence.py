"""Phase 6 tests for policy decision and intervention-history persistence."""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

from app.db import (
    get_intervention_attempt,
    get_policy_decision,
    get_policy_history,
    insert_execution_outcome,
    insert_intervention_attempt,
    insert_payment_event,
    insert_policy_decision,
    insert_provider_payment_link_outcome,
    insert_webhook_recovery_outcome,
)
from app.executor import ExecutionOutcome
from app.models import CustomerHistory, PaymentEvent
from app.policy import (
    PolicyDecision,
    InterventionAttempt,
)

T = timezone.utc
BASE_TS = datetime(2026, 8, 27, 12, 0, tzinfo=T)


def make_event(event_id: str = "evt_1", customer_id: str = "cust_1") -> PaymentEvent:
    return PaymentEvent(
        event_id=event_id,
        order_id="order_1",
        payment_id="pay_1",
        customer_id=customer_id,
        amount_paise=75000,
        currency="INR",
        payment_method="card",
        failure_reason="bank_timeout",
        bank="HDFC",
        risk_flag="normal",
        customer_history=CustomerHistory(
            prior_successful_payments=4,
            prior_failed_payments=1,
            has_active_subscription=True,
        ),
        timestamp="2026-08-27T12:00:00+00:00",
    )


def decision(**overrides) -> PolicyDecision:
    data = {
        "event_id": "evt_1",
        "proposed_intervention": "retry_delayed",
        "allowed": True,
        "denial_reason": None,
        "policy_rules_applied": [
            "fraud_check_passed",
            "terminal_check_passed",
            "spend_cap_passed",
        ],
        "evaluated_at": "2026-08-27T13:00:00+00:00",
    }
    data.update(overrides)
    return PolicyDecision.from_dict(data)


def attempt(**overrides) -> InterventionAttempt:
    data = {
        "event_id": "evt_1",
        "intervention": "retry_delayed",
        "customer_id": "cust_1",
        "cost_paise": 0,
        "attempted_at": "2026-08-27T12:30:00+00:00",
        "status": "attempted",
    }
    data.update(overrides)
    return InterventionAttempt.from_dict(data)


def test_database_initialization_creates_policy_tables(db_conn) -> None:
    names = {
        row[0]
        for row in db_conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
    }
    assert {"policy_decisions", "intervention_attempts"} <= names


def test_policy_decision_persists_and_is_retrievable(db_conn) -> None:
    insert_policy_decision(db_conn, decision())
    retrieved = get_policy_decision(
        db_conn, "evt_1", "retry_delayed", "2026-08-27T13:00:00+00:00"
    )
    assert retrieved is not None
    assert retrieved == decision()


def test_policy_decision_round_trip_preserves_contract(db_conn) -> None:
    data = {
        "event_id": "evt_1",
        "proposed_intervention": "payment_link",
        "allowed": False,
        "denial_reason": "spend_cap_exceeded",
        "policy_rules_applied": ["spend_cap_exceeded"],
        "evaluated_at": "2026-08-27T14:00:00+00:00",
    }
    insert_policy_decision(db_conn, PolicyDecision.from_dict(data))
    assert (
        get_policy_decision(
            db_conn, "evt_1", "payment_link", "2026-08-27T14:00:00+00:00"
        ).to_dict()
        == data
    )


def test_policy_decision_get_missing_returns_none(db_conn) -> None:
    assert (
        get_policy_decision(db_conn, "nope", "retry_delayed", "2026-08-27T13:00:00+00:00")
        is None
    )


def test_duplicate_policy_decision_is_rejected(db_conn) -> None:
    insert_policy_decision(db_conn, decision())
    with pytest.raises(sqlite3.IntegrityError):
        insert_policy_decision(db_conn, decision())


def test_multiple_policy_decisions_persist_independently(db_conn) -> None:
    insert_policy_decision(
        db_conn, decision(proposed_intervention="retry_delayed")
    )
    insert_policy_decision(
        db_conn, decision(proposed_intervention="payment_link")
    )
    assert (
        get_policy_decision(
            db_conn, "evt_1", "retry_delayed", "2026-08-27T13:00:00+00:00"
        ).proposed_intervention
        == "retry_delayed"
    )
    assert (
        get_policy_decision(
            db_conn, "evt_1", "payment_link", "2026-08-27T13:00:00+00:00"
        ).proposed_intervention
        == "payment_link"
    )


def test_intervention_attempt_persists_and_is_retrievable(db_conn) -> None:
    insert_intervention_attempt(db_conn, attempt())
    retrieved = get_intervention_attempt(
        db_conn, "evt_1", "retry_delayed", "2026-08-27T12:30:00+00:00"
    )
    assert retrieved == attempt()


def test_duplicate_intervention_attempt_is_rejected(db_conn) -> None:
    insert_intervention_attempt(db_conn, attempt())
    with pytest.raises(sqlite3.IntegrityError):
        insert_intervention_attempt(db_conn, attempt())


def test_policy_history_counts_customer_interventions_in_rolling_window(
    db_conn,
) -> None:
    event = make_event()
    evaluation_time = BASE_TS
    insert_intervention_attempt(
        db_conn,
        attempt(
            attempted_at=(evaluation_time - timedelta(hours=1)).isoformat()
        ),
    )
    insert_intervention_attempt(
        db_conn,
        attempt(
            attempted_at=(evaluation_time - timedelta(hours=2)).isoformat()
        ),
    )
    history = get_policy_history(db_conn, event, evaluation_time)
    assert history.customer_intervention_count_24h == 2


def test_policy_history_excludes_intervention_outside_24h(db_conn) -> None:
    event = make_event()
    evaluation_time = BASE_TS
    insert_intervention_attempt(
        db_conn,
        attempt(attempted_at=(evaluation_time - timedelta(hours=25)).isoformat()),
    )
    history = get_policy_history(db_conn, event, evaluation_time)
    assert history.customer_intervention_count_24h == 0


def test_policy_history_includes_exactly_24h_boundary(db_conn) -> None:
    event = make_event()
    evaluation_time = BASE_TS
    insert_intervention_attempt(
        db_conn,
        attempt(attempted_at=(evaluation_time - timedelta(hours=24)).isoformat()),
    )
    history = get_policy_history(db_conn, event, evaluation_time)
    assert history.customer_intervention_count_24h == 1


def test_policy_history_counts_only_matching_customer(db_conn) -> None:
    event = make_event()
    evaluation_time = BASE_TS
    insert_intervention_attempt(
        db_conn,
        attempt(
            customer_id="other_customer",
            attempted_at=(evaluation_time - timedelta(hours=1)).isoformat(),
        ),
    )
    history = get_policy_history(db_conn, event, evaluation_time)
    assert history.customer_intervention_count_24h == 0


def test_policy_history_reports_most_recent_event_intervention(db_conn) -> None:
    event = make_event()
    evaluation_time = BASE_TS
    insert_intervention_attempt(
        db_conn,
        attempt(attempted_at=(evaluation_time - timedelta(hours=2)).isoformat()),
    )
    insert_intervention_attempt(
        db_conn,
        attempt(attempted_at=(evaluation_time - timedelta(minutes=5)).isoformat()),
    )
    history = get_policy_history(db_conn, event, evaluation_time)
    assert history.most_recent_event_intervention_time == (
        evaluation_time - timedelta(minutes=5)
    )


def test_policy_history_reports_successful_duplicate(db_conn) -> None:
    event = make_event()
    evaluation_time = BASE_TS + timedelta(hours=1)
    insert_intervention_attempt(
        db_conn, attempt(status="successful")
    )
    history = get_policy_history(db_conn, event, evaluation_time)
    assert history.has_successful_intervention is True


def test_policy_history_failed_attempt_is_not_successful_duplicate(db_conn) -> None:
    event = make_event()
    evaluation_time = BASE_TS
    insert_intervention_attempt(
        db_conn,
        attempt(
            attempted_at=(evaluation_time - timedelta(minutes=20)).isoformat(),
            status="failed",
        ),
    )
    insert_intervention_attempt(
        db_conn,
        attempt(
            attempted_at=(evaluation_time - timedelta(minutes=10)).isoformat(),
            status="attempted",
        ),
    )
    history = get_policy_history(db_conn, event, evaluation_time)
    assert history.has_successful_intervention is False


def test_policy_history_sums_daily_spend_across_customers_in_window(db_conn) -> None:
    event = make_event()
    evaluation_time = BASE_TS
    insert_intervention_attempt(
        db_conn,
        attempt(
            event_id="evt_other",
            customer_id="other_customer",
            cost_paise=250,
            attempted_at=(evaluation_time - timedelta(hours=1)).isoformat(),
        ),
    )
    insert_intervention_attempt(
        db_conn,
        attempt(
            cost_paise=100,
            attempted_at=(evaluation_time - timedelta(hours=2)).isoformat(),
        ),
    )
    insert_intervention_attempt(
        db_conn,
        attempt(
            cost_paise=9999,
            attempted_at=(evaluation_time - timedelta(hours=25)).isoformat(),
        ),
    )
    history = get_policy_history(db_conn, event, evaluation_time)
    assert history.existing_daily_spend_paise == 350


def test_policy_history_requires_aware_evaluation_time(db_conn) -> None:
    event = make_event()
    with pytest.raises(Exception):
        get_policy_history(
            db_conn, event, datetime(2026, 8, 27, 12, 0)
        )


def test_sibling_events_sharing_order_id_share_cooldown_history(db_conn) -> None:
    event_1 = make_event(event_id="evt_order_1")
    event_2 = make_event(event_id="evt_order_2")
    insert_payment_event(db_conn, event_1)
    insert_payment_event(db_conn, event_2)

    evaluation_time = BASE_TS
    attempt_time = evaluation_time - timedelta(minutes=10)
    insert_intervention_attempt(
        db_conn,
        attempt(
            event_id="evt_order_1",
            status="attempted",
            attempted_at=attempt_time.isoformat(),
        ),
    )

    history = get_policy_history(db_conn, event_2, evaluation_time)
    assert history.most_recent_event_intervention_time == attempt_time


def test_sibling_events_sharing_order_id_share_duplicate_history(db_conn) -> None:
    event_1 = make_event(event_id="evt_order_1")
    event_2 = make_event(event_id="evt_order_2")
    insert_payment_event(db_conn, event_1)
    insert_payment_event(db_conn, event_2)

    evaluation_time = BASE_TS
    insert_intervention_attempt(
        db_conn,
        attempt(
            event_id="evt_order_1",
            status="successful",
            attempted_at=(evaluation_time - timedelta(minutes=45)).isoformat(),
        ),
    )

    history = get_policy_history(db_conn, event_2, evaluation_time)
    assert history.has_successful_intervention is True


def test_expired_payment_link_allows_reengagement_without_duplicate_block(
    db_conn,
) -> None:
    event = make_event(event_id="evt_plink_exp")
    insert_payment_event(db_conn, event)

    evaluation_time = BASE_TS
    attempt_time = evaluation_time - timedelta(minutes=45)
    insert_intervention_attempt(
        db_conn,
        attempt(
            event_id="evt_plink_exp",
            intervention="payment_link",
            status="successful",
            attempted_at=attempt_time.isoformat(),
        ),
    )
    insert_execution_outcome(
        db_conn,
        ExecutionOutcome(
            event_id="evt_plink_exp",
            intervention="payment_link",
            execution_mode="REAL_RAZORPAY",
            status="SUCCESS",
            external_reference="https://rzp.io/l/expired",
            detail=None,
            reported_at=attempt_time.isoformat(),
            payment_link_id="plink_exp_1",
        ),
    )
    insert_provider_payment_link_outcome(
        db_conn,
        payment_link_id="plink_exp_1",
        event_id="evt_plink_exp",
        status="expired",
        outcome="NOT_RECOVERED",
        observed_at=(evaluation_time - timedelta(minutes=15)).isoformat(),
    )

    history = get_policy_history(db_conn, event, evaluation_time)
    assert history.has_successful_intervention is False
    assert history.most_recent_event_intervention_time == attempt_time


def test_expired_payment_link_with_webhook_recovery_blocks_reengagement(
    db_conn,
) -> None:
    event = make_event(event_id="evt_plink_recovered")
    insert_payment_event(db_conn, event)

    evaluation_time = BASE_TS
    attempt_time = evaluation_time - timedelta(minutes=45)
    insert_intervention_attempt(
        db_conn,
        attempt(
            event_id="evt_plink_recovered",
            intervention="payment_link",
            status="successful",
            attempted_at=attempt_time.isoformat(),
        ),
    )
    insert_execution_outcome(
        db_conn,
        ExecutionOutcome(
            event_id="evt_plink_recovered",
            intervention="payment_link",
            execution_mode="REAL_RAZORPAY",
            status="SUCCESS",
            external_reference="https://rzp.io/l/rec",
            detail=None,
            reported_at=attempt_time.isoformat(),
            payment_link_id="plink_rec_1",
        ),
    )
    insert_provider_payment_link_outcome(
        db_conn,
        payment_link_id="plink_rec_1",
        event_id="evt_plink_recovered",
        status="expired",
        outcome="NOT_RECOVERED",
        observed_at=(evaluation_time - timedelta(minutes=15)).isoformat(),
    )
    insert_webhook_recovery_outcome(
        db_conn,
        delivery_id="del_rec_1",
        payment_link_id="plink_rec_1",
        referenced_event_id="evt_plink_recovered",
        amount_paid_paise=75000,
        currency="INR",
        payment_id="pay_rec_1",
        recovered_at=(evaluation_time - timedelta(minutes=10)).isoformat(),
    )

    history = get_policy_history(db_conn, event, evaluation_time)
    assert history.has_successful_intervention is True


def test_multiple_payment_links_require_all_expired_for_reengagement(
    db_conn,
) -> None:
    event = make_event(event_id="evt_plink_multi")
    insert_payment_event(db_conn, event)

    evaluation_time = BASE_TS
    attempt_time_1 = evaluation_time - timedelta(minutes=60)
    attempt_time_2 = evaluation_time - timedelta(minutes=40)
    insert_intervention_attempt(
        db_conn,
        attempt(
            event_id="evt_plink_multi",
            intervention="payment_link",
            status="successful",
            attempted_at=attempt_time_1.isoformat(),
        ),
    )
    insert_execution_outcome(
        db_conn,
        ExecutionOutcome(
            event_id="evt_plink_multi",
            intervention="payment_link",
            execution_mode="REAL_RAZORPAY",
            status="SUCCESS",
            external_reference="https://rzp.io/l/multi1",
            detail=None,
            reported_at=attempt_time_1.isoformat(),
            payment_link_id="plink_multi_1",
        ),
    )
    insert_execution_outcome(
        db_conn,
        ExecutionOutcome(
            event_id="evt_plink_multi",
            intervention="payment_link",
            execution_mode="REAL_RAZORPAY",
            status="SUCCESS",
            external_reference="https://rzp.io/l/multi2",
            detail=None,
            reported_at=attempt_time_2.isoformat(),
            payment_link_id="plink_multi_2",
        ),
    )
    # Only link 1 is expired; link 2 is still pending
    insert_provider_payment_link_outcome(
        db_conn,
        payment_link_id="plink_multi_1",
        event_id="evt_plink_multi",
        status="expired",
        outcome="NOT_RECOVERED",
        observed_at=(evaluation_time - timedelta(minutes=20)).isoformat(),
    )

    history = get_policy_history(db_conn, event, evaluation_time)
    assert history.has_successful_intervention is True

    # When link 2 also expires NOT_RECOVERED, re-engagement is allowed
    insert_provider_payment_link_outcome(
        db_conn,
        payment_link_id="plink_multi_2",
        event_id="evt_plink_multi",
        status="expired",
        outcome="NOT_RECOVERED",
        observed_at=(evaluation_time - timedelta(minutes=10)).isoformat(),
    )
    history_after = get_policy_history(db_conn, event, evaluation_time)
    assert history_after.has_successful_intervention is False

"""Phase 22 outcome feedback domain tests.

These tests pin the two properties the whole intelligence layer rests on:
execution is never mistaken for recovery, and uncertainty is never converted
into failure.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from app.db import (
    get_execution_outcomes_for_events,
    get_optimizer_decisions_for_events,
    get_webhook_recovery_outcomes_for_payment_links,
    insert_execution_outcome,
    insert_optimizer_decision,
    insert_payment_event,
    insert_webhook_recovery_outcome,
)
from app.economics import CandidateEvaluation
from app.executor import ExecutionOutcome
from app.models import PaymentEvent
from app.optimizer_audit import OptimizerDecisionRecord
from app.outcome_feedback import (
    OUTCOME_NOT_RECOVERED,
    OUTCOME_PENDING,
    OUTCOME_RECOVERED,
    OUTCOME_UNKNOWN,
    REASON_AMBIGUOUS_PROVIDER_RESULT,
    REASON_AWAITING_OUTCOME,
    REASON_CALIBRATION_ELIGIBLE,
    REASON_EXECUTION_FAILED,
    REASON_MISSING_PAYMENT_LINK_ID,
    REASON_MISSING_PREDICTION,
    REASON_SIMULATED_EXECUTION,
    build_observation,
    build_observation_population,
    build_observations,
    build_observations_for_event,
    calibration_observations,
    find_prediction,
    ineligibility_counts,
    verified_recoveries,
)
from app.razorpay_client import PROVIDER_RESULT_UNKNOWN
from app.recovery_intelligence import (
    INSUFFICIENT_OBSERVATIONS,
    build_recovery_intelligence,
)

NOW = datetime(2026, 8, 30, 9, 0, tzinfo=timezone.utc)


def _event(event_id: str = "evt_1", **overrides) -> dict:
    data = {
        "event_id": event_id,
        "order_id": f"order_{event_id}",
        "payment_id": f"pay_{event_id}",
        "customer_id": f"cust_{event_id}",
        "amount_paise": 100_000,
        "currency": "INR",
        "payment_method": "upi",
        "failure_reason": "bank_timeout",
        "bank": "HDFC",
        "risk_flag": "normal",
        "customer_history": {
            "prior_successful_payments": 2,
            "prior_failed_payments": 1,
            "has_active_subscription": True,
        },
        "timestamp": NOW.isoformat(),
    }
    data.update(overrides)
    return data


def _execution(
    *,
    event_id: str = "evt_1",
    intervention: str = "payment_link",
    mode: str = "REAL_RAZORPAY",
    status: str = "SUCCESS",
    payment_link_id: str | None = "plink_1",
    detail: str | None = None,
    reported_at: datetime = NOW,
) -> dict:
    return {
        "event_id": event_id,
        "intervention": intervention,
        "execution_mode": mode,
        "status": status,
        "external_reference": "https://rzp.io/i/abc",
        "detail": detail,
        "payment_link_id": payment_link_id,
        "reported_at": reported_at.isoformat(),
    }


def _decision(
    *,
    event_id: str = "evt_1",
    intervention: str = "payment_link",
    probability_bps: int = 6_000,
    decided_at: datetime | None = None,
    amount_paise: int = 100_000,
) -> dict:
    decided = decided_at or (NOW - timedelta(minutes=1))
    return {
        "event_id": event_id,
        "decided_at": decided.isoformat(),
        "selected_intervention": intervention,
        "selection_reason": "highest expected value",
        "candidates_considered": [intervention],
        "allowed_candidates": [intervention],
        "evaluations": [
            {
                "intervention": intervention,
                "estimated_probability_bps": probability_bps,
                "amount_paise": amount_paise,
                "expected_recovered_value_paise": amount_paise
                * probability_bps
                // 10_000,
                "intervention_cost_paise": 100,
                "friction_cost_paise": 100,
                "expected_value_paise": amount_paise * probability_bps // 10_000 - 200,
            }
        ],
    }


def _recovery(
    *,
    payment_link_id: str = "plink_1",
    delivery_id: str = "delivery_1",
    amount_paid_paise: int | None = 100_000,
) -> dict:
    return {
        "delivery_id": delivery_id,
        "payment_link_id": payment_link_id,
        "referenced_event_id": "evt_1",
        "amount_paid_paise": amount_paid_paise,
        "currency": "INR",
        "payment_id": "pay_verified",
        "recovered_at": (NOW + timedelta(minutes=30)).isoformat(),
    }


# ---------------------------------------------------------------------------
# Outcome mapping
# ---------------------------------------------------------------------------


def test_verified_payment_is_recovered_and_eligible():
    observation = build_observation(
        _event(), _execution(), [_decision()], {"plink_1": _recovery()}
    )
    assert observation.outcome == OUTCOME_RECOVERED
    assert observation.calibration_eligible is True
    assert observation.reason == REASON_CALIBRATION_ELIGIBLE
    assert observation.recovered is True
    assert observation.recovered_amount_paise == 100_000
    assert observation.evidence_id == "delivery_1"
    assert observation.predicted_probability_bps == 6_000


def test_successful_real_execution_without_verified_payment_is_pending():
    observation = build_observation(_event(), _execution(), [_decision()], {})
    assert observation.outcome == OUTCOME_PENDING
    assert observation.reason == REASON_AWAITING_OUTCOME
    assert observation.calibration_eligible is False
    assert observation.recovered is None
    assert observation.recovered_amount_paise is None


def test_failed_execution_is_never_recovered_and_is_not_a_payment_failure():
    observation = build_observation(
        _event(),
        _execution(status="FAILED", payment_link_id=None, detail="gateway refused"),
        [_decision()],
        {"plink_1": _recovery()},
    )
    assert observation.outcome == OUTCOME_UNKNOWN
    assert observation.reason == REASON_EXECUTION_FAILED
    assert observation.calibration_eligible is False
    assert observation.recovered is None


def test_ambiguous_provider_result_is_unknown_and_ineligible():
    observation = build_observation(
        _event(),
        _execution(
            status="FAILED",
            payment_link_id=None,
            detail=f"{PROVIDER_RESULT_UNKNOWN}: connection reset",
        ),
        [_decision()],
        {},
    )
    assert observation.outcome == OUTCOME_UNKNOWN
    assert observation.reason == REASON_AMBIGUOUS_PROVIDER_RESULT
    assert observation.calibration_eligible is False


def test_simulated_execution_is_never_an_operational_observation():
    observation = build_observation(
        _event(),
        _execution(mode="SIMULATED", intervention="retry_delayed", payment_link_id=None),
        [_decision(intervention="retry_delayed")],
        {},
    )
    assert observation.calibration_eligible is False
    assert observation.reason == REASON_SIMULATED_EXECUTION
    assert observation.recovered is None
    assert observation.recovered_amount_paise is None


def test_real_success_without_payment_link_id_is_unknown():
    observation = build_observation(
        _event(), _execution(payment_link_id=None), [_decision()], {}
    )
    assert observation.outcome == OUTCOME_UNKNOWN
    assert observation.reason == REASON_MISSING_PAYMENT_LINK_ID


def test_unmatched_recovery_evidence_fabricates_nothing():
    # A verified recovery exists, but for a different Payment Link.
    observation = build_observation(
        _event(),
        _execution(),
        [_decision()],
        {"plink_other": _recovery(payment_link_id="plink_other")},
    )
    assert observation.outcome == OUTCOME_PENDING
    assert observation.recovered is None


def test_missing_recovered_amount_is_recorded_as_missing_not_inferred():
    observation = build_observation(
        _event(),
        _execution(),
        [_decision()],
        {"plink_1": _recovery(amount_paid_paise=None)},
    )
    assert observation.outcome == OUTCOME_RECOVERED
    assert observation.calibration_eligible is True
    assert observation.recovered_amount_paise is None
    # The original event amount must never be substituted.
    assert observation.amount_paise == 100_000
    assert "never inferred" in observation.note


def test_late_verified_outcome_is_included_deterministically():
    late = _recovery()
    late["recovered_at"] = (NOW + timedelta(days=9)).isoformat()
    observation = build_observation(
        _event(), _execution(), [_decision()], {"plink_1": late}
    )
    assert observation.outcome == OUTCOME_RECOVERED
    assert observation.calibration_eligible is True
    assert observation.observed_at == late["recovered_at"]


def test_verified_recovery_without_a_persisted_prediction_is_ineligible():
    observation = build_observation(
        _event(), _execution(), [], {"plink_1": _recovery()}
    )
    assert observation.outcome == OUTCOME_RECOVERED
    assert observation.calibration_eligible is False
    assert observation.reason == REASON_MISSING_PREDICTION
    assert observation.predicted_probability_bps is None


# ---------------------------------------------------------------------------
# Prediction / outcome join
# ---------------------------------------------------------------------------


def test_prediction_join_selects_the_decision_that_drove_the_execution():
    older = _decision(probability_bps=4_000, decided_at=NOW - timedelta(hours=2))
    newer = _decision(probability_bps=7_000, decided_at=NOW - timedelta(minutes=5))
    prediction = find_prediction([older, newer], "payment_link", NOW.isoformat())
    assert prediction is not None
    assert prediction["decided_at"] == newer["decided_at"]


def test_prediction_join_ignores_decisions_made_after_the_execution():
    before = _decision(probability_bps=4_000, decided_at=NOW - timedelta(hours=1))
    after = _decision(probability_bps=9_900, decided_at=NOW + timedelta(hours=1))
    prediction = find_prediction([before, after], "payment_link", NOW.isoformat())
    assert prediction is not None
    assert prediction["decided_at"] == before["decided_at"]


def test_prediction_join_matches_the_same_instant_across_offsets():
    """+05:30 and the equivalent UTC text denote one instant, not two."""
    # 2026-08-30T09:00:00+00:00 == 2026-08-30T14:30:00+05:30.
    decision = _decision(decided_at=None)
    decision["decided_at"] = "2026-08-30T14:30:00+05:30"
    prediction = find_prediction(
        [decision], "payment_link", "2026-08-30T09:00:00+00:00"
    )
    assert prediction is not None
    # Text-wise "14:30" sorts after "09:00", so a string comparison would have
    # rejected this decision as being after the execution.
    assert prediction["decided_at"] > "2026-08-30T09:00:00+00:00"


def test_prediction_join_respects_offsets_when_ordering_candidates():
    earlier = _decision(probability_bps=4_000)
    earlier["decided_at"] = "2026-08-30T08:00:00+00:00"
    # Later instant (08:30 UTC) written in a different offset, and its text
    # sorts BEFORE the earlier decision's text.
    later = _decision(probability_bps=7_000)
    later["decided_at"] = "2026-08-30T05:00:00-03:30"
    prediction = find_prediction(
        [earlier, later], "payment_link", "2026-08-30T09:00:00+00:00"
    )
    assert prediction is not None
    assert prediction["evaluations"][0]["estimated_probability_bps"] == 7_000


def test_prediction_join_rejects_a_decision_made_after_in_another_offset():
    # 16:00+05:30 is 10:30 UTC, which is after the 09:00 UTC execution, so the
    # decision cannot have driven it however the timestamp is written.
    late = _decision()
    late["decided_at"] = "2026-08-30T16:00:00+05:30"
    assert find_prediction([late], "payment_link", "2026-08-30T09:00:00+00:00") is None


def test_prediction_join_skips_unparseable_and_naive_timestamps():
    naive = _decision(probability_bps=9_000)
    naive["decided_at"] = "2026-08-30T08:00:00"
    garbage = _decision(probability_bps=9_900)
    garbage["decided_at"] = "not-a-timestamp"
    usable = _decision(probability_bps=4_000)
    usable["decided_at"] = "2026-08-30T08:00:00+00:00"
    prediction = find_prediction(
        [naive, garbage, usable], "payment_link", "2026-08-30T09:00:00+00:00"
    )
    assert prediction is not None
    assert prediction["evaluations"][0]["estimated_probability_bps"] == 4_000


def test_an_unusable_execution_timestamp_joins_to_nothing():
    assert find_prediction([_decision()], "payment_link", "not-a-timestamp") is None


def test_prediction_join_never_crosses_interventions():
    other = _decision(intervention="reminder", probability_bps=9_000)
    assert find_prediction([other], "payment_link", NOW.isoformat()) is None


def test_repeated_interventions_produce_one_observation_each():
    first = _execution(
        intervention="reminder",
        mode="SIMULATED",
        payment_link_id=None,
        reported_at=NOW - timedelta(hours=1),
    )
    second = _execution(reported_at=NOW)
    observations = build_observations_for_event(
        _event(),
        [second, first],
        [
            _decision(intervention="reminder", decided_at=NOW - timedelta(hours=2)),
            _decision(),
        ],
        {"plink_1": _recovery()},
    )
    assert [o.intervention for o in observations] == ["reminder", "payment_link"]
    assert [o.calibration_eligible for o in observations] == [False, True]


def test_projection_is_deterministic_over_persisted_records(db_conn):
    insert_payment_event(db_conn, PaymentEvent.from_dict(_event()))
    insert_payment_event(db_conn, PaymentEvent.from_dict(_event("evt_2")))
    insert_optimizer_decision(
        db_conn,
        OptimizerDecisionRecord(
            event_id="evt_1",
            decided_at=(NOW - timedelta(minutes=1)).isoformat(),
            selected_intervention="payment_link",
            selection_reason="highest expected value",
            candidates_considered=("payment_link",),
            allowed_candidates=("payment_link",),
            evaluations=(
                CandidateEvaluation(
                    intervention="payment_link",
                    estimated_probability_bps=6_000,
                    amount_paise=100_000,
                    expected_recovered_value_paise=60_000,
                    intervention_cost_paise=100,
                    friction_cost_paise=100,
                    expected_value_paise=59_800,
                ),
            ),
        ),
    )
    insert_execution_outcome(
        db_conn,
        ExecutionOutcome(
            event_id="evt_1",
            intervention="payment_link",
            execution_mode="REAL_RAZORPAY",
            status="SUCCESS",
            external_reference="https://rzp.io/i/abc",
            detail=None,
            reported_at=NOW.isoformat(),
            payment_link_id="plink_1",
        ),
    )
    insert_execution_outcome(
        db_conn,
        ExecutionOutcome(
            event_id="evt_2",
            intervention="retry_delayed",
            execution_mode="SIMULATED",
            status="SUCCESS",
            external_reference=None,
            detail=None,
            reported_at=NOW.isoformat(),
            payment_link_id=None,
        ),
    )
    insert_webhook_recovery_outcome(
        db_conn,
        delivery_id="delivery_1",
        payment_link_id="plink_1",
        referenced_event_id="evt_1",
        amount_paid_paise=100_000,
        currency="INR",
        payment_id="pay_verified",
        recovered_at=(NOW + timedelta(minutes=30)).isoformat(),
    )

    first = build_observations(db_conn)
    second = build_observations(db_conn)
    assert [o.to_dict() for o in first] == [o.to_dict() for o in second]
    assert [o.event_id for o in first] == ["evt_1", "evt_2"]
    assert len(calibration_observations(first)) == 1
    assert ineligibility_counts(first)[REASON_SIMULATED_EXECUTION] == 1


def test_duplicate_webhook_delivery_yields_one_logical_observation(db_conn):
    insert_webhook_recovery_outcome(
        db_conn,
        delivery_id="delivery_1",
        payment_link_id="plink_1",
        referenced_event_id="evt_1",
        amount_paid_paise=100_000,
        currency="INR",
        payment_id="pay_verified",
        recovered_at=NOW.isoformat(),
    )
    inserted_again = insert_webhook_recovery_outcome(
        db_conn,
        delivery_id="delivery_1",
        payment_link_id="plink_1",
        referenced_event_id="evt_1",
        amount_paid_paise=100_000,
        currency="INR",
        payment_id="pay_verified",
        recovered_at=NOW.isoformat(),
    )
    assert inserted_again is False
    recoveries = get_webhook_recovery_outcomes_for_payment_links(db_conn, ["plink_1"])
    observation = build_observation(_event(), _execution(), [_decision()], recoveries)
    assert observation.outcome == OUTCOME_RECOVERED
    assert observation.recovered is True


def test_no_cross_event_join_in_the_persisted_projection(db_conn):
    """Another event's verified recovery must not attach to this execution."""
    insert_payment_event(db_conn, PaymentEvent.from_dict(_event("evt_a")))
    insert_payment_event(db_conn, PaymentEvent.from_dict(_event("evt_b")))
    for event_id, link in (("evt_a", "plink_a"), ("evt_b", "plink_b")):
        insert_execution_outcome(
            db_conn,
            ExecutionOutcome(
                event_id=event_id,
                intervention="payment_link",
                execution_mode="REAL_RAZORPAY",
                status="SUCCESS",
                external_reference="https://rzp.io/i/x",
                detail=None,
                reported_at=NOW.isoformat(),
                payment_link_id=link,
            ),
        )
    insert_webhook_recovery_outcome(
        db_conn,
        delivery_id="delivery_a",
        payment_link_id="plink_a",
        referenced_event_id="evt_a",
        amount_paid_paise=100_000,
        currency="INR",
        payment_id="pay_a",
        recovered_at=NOW.isoformat(),
    )
    by_event = {o.event_id: o for o in build_observations(db_conn)}
    assert by_event["evt_a"].outcome == OUTCOME_RECOVERED
    assert by_event["evt_b"].outcome == OUTCOME_PENDING


# ---------------------------------------------------------------------------
# The production projection path, not synthetic aggregation objects.
#
# The observation BUILDER can only ever emit RECOVERED, PENDING or UNKNOWN
# from the current provider contract. These tests exercise that real path and
# prove the calibration layer refuses to turn positive-only evidence into a
# recovery rate.
# ---------------------------------------------------------------------------


def _seed_verified_recovery(conn, index: int) -> None:
    """Persist one full decision -> execution -> verified recovery chain."""
    event_id = f"evt_r{index:03d}"
    insert_payment_event(conn, PaymentEvent.from_dict(_event(event_id)))
    insert_optimizer_decision(
        conn,
        OptimizerDecisionRecord(
            event_id=event_id,
            decided_at=(NOW - timedelta(minutes=1)).isoformat(),
            selected_intervention="payment_link",
            selection_reason="highest expected value",
            candidates_considered=("payment_link",),
            allowed_candidates=("payment_link",),
            evaluations=(
                CandidateEvaluation(
                    intervention="payment_link",
                    estimated_probability_bps=6_000,
                    amount_paise=100_000,
                    expected_recovered_value_paise=60_000,
                    intervention_cost_paise=100,
                    friction_cost_paise=100,
                    expected_value_paise=59_800,
                ),
            ),
        ),
    )
    insert_execution_outcome(
        conn,
        ExecutionOutcome(
            event_id=event_id,
            intervention="payment_link",
            execution_mode="REAL_RAZORPAY",
            status="SUCCESS",
            external_reference="https://rzp.io/i/x",
            detail=None,
            reported_at=NOW.isoformat(),
            payment_link_id=f"plink_r{index:03d}",
        ),
    )
    insert_webhook_recovery_outcome(
        conn,
        delivery_id=f"delivery_r{index:03d}",
        payment_link_id=f"plink_r{index:03d}",
        referenced_event_id=event_id,
        amount_paid_paise=100_000,
        currency="INR",
        payment_id=f"pay_v{index:03d}",
        recovered_at=(NOW + timedelta(minutes=30)).isoformat(),
    )


def test_builder_never_emits_not_recovered_from_real_evidence(db_conn):
    """No sequence of persisted provider evidence produces NOT_RECOVERED."""
    for index in range(3):
        _seed_verified_recovery(db_conn, index)
    insert_payment_event(db_conn, PaymentEvent.from_dict(_event("evt_failed")))
    insert_execution_outcome(
        db_conn,
        ExecutionOutcome(
            event_id="evt_failed",
            intervention="payment_link",
            execution_mode="REAL_RAZORPAY",
            status="FAILED",
            external_reference=None,
            detail="gateway refused",
            reported_at=NOW.isoformat(),
            payment_link_id=None,
        ),
    )
    insert_payment_event(db_conn, PaymentEvent.from_dict(_event("evt_pending")))
    insert_execution_outcome(
        db_conn,
        ExecutionOutcome(
            event_id="evt_pending",
            intervention="payment_link",
            execution_mode="REAL_RAZORPAY",
            status="SUCCESS",
            external_reference="https://rzp.io/i/y",
            detail=None,
            reported_at=NOW.isoformat(),
            payment_link_id="plink_pending",
        ),
    )

    observations = build_observations(db_conn)
    outcomes = {observation.outcome for observation in observations}
    assert OUTCOME_NOT_RECOVERED not in outcomes
    assert outcomes == {OUTCOME_RECOVERED, OUTCOME_PENDING, OUTCOME_UNKNOWN}
    assert all(
        observation.recovered is not False for observation in observations
    ), "a failed or pending execution must never be an observed non-payment"


def test_ten_verified_recoveries_do_not_produce_a_recovery_rate(db_conn):
    """The audit case: verified evidence is reported, no rate is claimed."""
    for index in range(10):
        _seed_verified_recovery(db_conn, index)

    observations = build_observations(db_conn)
    assert len(verified_recoveries(observations)) == 10
    assert len(calibration_observations(observations)) == 10

    payload = build_recovery_intelligence(db_conn)
    stats = payload["calibration"]
    assert stats["verified_recoveries"] == 10
    assert stats["recovered_observations"] == 10
    assert stats["not_recovered_observations"] == 0
    assert stats["has_terminal_negative_evidence"] is False
    assert stats["status"] == INSUFFICIENT_OBSERVATIONS
    assert stats["observed_recovery_rate_bps"] is None
    assert stats["calibration_gap_bps"] is None
    # And the intervention row must not claim one either.
    row = payload["interventions"][0]
    assert row["verified_recoveries"] == 10
    assert row["observed_recovery_rate_bps"] is None


def test_verified_recovery_value_evidence_survives_without_calibration(db_conn):
    """Real money observed must stay visible when no rate can be computed."""
    for index in range(3):
        _seed_verified_recovery(db_conn, index)
    payload = build_recovery_intelligence(db_conn)
    assert payload["calibration"]["status"] == INSUFFICIENT_OBSERVATIONS
    value = payload["expected_vs_realized"]
    assert value["compared_observations"] == 3
    assert value["realized_recovered_amount_paise"] == 300_000
    assert value["expected_recovered_value_paise"] == 180_000
    assert payload["interventions"][0]["average_recovered_amount_paise"] == 100_000


# ---------------------------------------------------------------------------
# Population semantics: the projection must never present a bounded prefix as
# though it were the whole history.
# ---------------------------------------------------------------------------


def test_population_reports_completeness_when_everything_is_projected(db_conn):
    for index in range(3):
        _seed_verified_recovery(db_conn, index)
    population = build_observation_population(db_conn)
    assert population.total_executions == 3
    assert population.projected_executions == 3
    assert population.complete is True
    assert population.to_dict()["complete"] is True


def test_population_declares_itself_incomplete_at_the_limit(db_conn):
    for index in range(5):
        _seed_verified_recovery(db_conn, index)
    population = build_observation_population(db_conn, limit=2)
    assert population.total_executions == 5
    assert population.projected_executions == 2
    assert population.complete is False
    assert len(population.observations) == 2


def test_bounded_population_takes_a_deterministic_prefix(db_conn):
    for index in range(5):
        _seed_verified_recovery(db_conn, index)
    first = build_observation_population(db_conn, limit=3)
    second = build_observation_population(db_conn, limit=3)
    assert [o.to_dict() for o in first.observations] == [
        o.to_dict() for o in second.observations
    ]


def test_old_executions_are_measured_regardless_of_event_recency(db_conn):
    """An execution outside any recent-events window is still projected."""
    insert_payment_event(
        db_conn,
        PaymentEvent.from_dict(
            _event("evt_ancient", timestamp=(NOW - timedelta(days=900)).isoformat())
        ),
    )
    insert_execution_outcome(
        db_conn,
        ExecutionOutcome(
            event_id="evt_ancient",
            intervention="payment_link",
            execution_mode="REAL_RAZORPAY",
            status="SUCCESS",
            external_reference="https://rzp.io/i/old",
            detail=None,
            reported_at=(NOW - timedelta(days=900)).isoformat(),
            payment_link_id="plink_ancient",
        ),
    )
    insert_webhook_recovery_outcome(
        db_conn,
        delivery_id="delivery_ancient",
        payment_link_id="plink_ancient",
        referenced_event_id="evt_ancient",
        amount_paid_paise=100_000,
        currency="INR",
        payment_id="pay_ancient",
        recovered_at=(NOW - timedelta(days=899)).isoformat(),
    )
    for index in range(3):
        _seed_verified_recovery(db_conn, index)

    observations = build_observations(db_conn)
    assert "evt_ancient" in {observation.event_id for observation in observations}
    assert len(verified_recoveries(observations)) == 4


def test_an_execution_without_a_persisted_event_is_skipped_not_invented(db_conn):
    insert_execution_outcome(
        db_conn,
        ExecutionOutcome(
            event_id="evt_orphan",
            intervention="payment_link",
            execution_mode="REAL_RAZORPAY",
            status="SUCCESS",
            external_reference="https://rzp.io/i/z",
            detail=None,
            reported_at=NOW.isoformat(),
            payment_link_id="plink_orphan",
        ),
    )
    population = build_observation_population(db_conn)
    assert population.total_executions == 1
    assert population.observations == ()


@pytest.mark.parametrize("mode", ["SIMULATED", "REAL_RAZORPAY"])
def test_observation_always_carries_a_reason(mode):
    observation = build_observation(
        _event(), _execution(mode=mode), [_decision()], {}
    )
    assert observation.reason

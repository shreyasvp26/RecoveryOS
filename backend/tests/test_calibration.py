"""Phase 23 tests — the calibration layer.

This is the evidence-calibrated layer that turns terminal REAL_RAZORPAY
payment_link outcomes into gated, versioned posteriors. These tests pin:

  * the terminal contract (provider status -> calibration outcome),
  * the exact integer posterior arithmetic and prior derivation,
  * the evidence gate (an intervention only becomes active with its OWN
    sufficient terminal evidence),
  * that non-terminal / unknown evidence is never a sample,
  * that SIMULATED or foreign interventions can never seed calibration.
"""

from __future__ import annotations

import pytest

from app import calibration as cal
from app.calibration import (
    OUTCOME_NOT_RECOVERED,
    OUTCOME_PENDING,
    OUTCOME_RECOVERED,
    OUTCOME_UNKNOWN,
    STATUS_BASELINE,
    STATUS_CALIBRATED,
    CalibrationError,
    CalibrationObservation,
    SegmentCalibration,
    calibrate,
    calibrate_intervention,
    map_provider_status,
    posterior_bps,
    prior_failures,
    prior_successes,
    segment_key,
    segment_posterior_bps,
    segment_prior_failures,
    segment_prior_successes,
)

# payment_link baseline in bps from the frozen taxonomy (BASE_RECOVERY_BPS).
INTERVENTION = "payment_link"
OTHER_INTERVENTION = "reminder"
PAYMENT_LINK_BASELINE_BPS = cal.BASE_RECOVERY_BPS[INTERVENTION]


def _obs(outcome: str, *, event_id: str = "e1") -> CalibrationObservation:
    return CalibrationObservation(
        event_id=event_id,
        intervention=INTERVENTION,
        outcome=outcome,
        terminal=(outcome in cal.TERMINAL_OUTCOMES),
        amount_paid_paise=1_000_00,
        observed_at="2026-01-01T00:00:00+00:00",
        evidence_id="del_1",
        evidence_source="webhook",
    )


# ---------------------------------------------------------------------------
# Terminal contract
# ---------------------------------------------------------------------------


def test_provider_status_terminal_contract_exact():
    assert map_provider_status("paid") == OUTCOME_RECOVERED
    assert map_provider_status("expired") == OUTCOME_NOT_RECOVERED
    assert map_provider_status("created") == OUTCOME_PENDING
    assert map_provider_status("partially_paid") == OUTCOME_PENDING


def test_provider_status_unknown_is_never_negative():
    for status in ("cancelled", "authorized", None, "garbage"):
        assert map_provider_status(status) == OUTCOME_UNKNOWN


def test_cancelled_never_maps_to_not_recovered():
    assert map_provider_status("cancelled") == OUTCOME_UNKNOWN
    assert map_provider_status("cancelled") != OUTCOME_NOT_RECOVERED


# ---------------------------------------------------------------------------
# Prior derivation (integer-exact)
# ---------------------------------------------------------------------------


def test_prior_split_is_integer_exact_and_sums_to_strength():
    s = prior_successes(PAYMENT_LINK_BASELINE_BPS)
    f = prior_failures(PAYMENT_LINK_BASELINE_BPS)
    assert s + f == cal.PRIOR_STRENGTH
    assert s == PAYMENT_LINK_BASELINE_BPS * cal.PRIOR_STRENGTH // 10000
    assert type(s) is int


def test_prior_successes_bounds():
    assert prior_successes(0) == 0
    assert prior_successes(10000) == cal.PRIOR_STRENGTH
    with pytest.raises(CalibrationError):
        prior_successes(-1)
    with pytest.raises(CalibrationError):
        prior_successes(10001)


# ---------------------------------------------------------------------------
# Posterior arithmetic
# ---------------------------------------------------------------------------


def test_posterior_uses_integer_floor_division():
    # 10 terminals, all recovered -> posterior = (10+prior_s)*10000//(10+strength)
    expected = (10 + prior_successes(PAYMENT_LINK_BASELINE_BPS)) * 10000 // (
        10 + cal.PRIOR_STRENGTH
    )
    assert posterior_bps(10, 10, PAYMENT_LINK_BASELINE_BPS) == expected
    assert type(posterior_bps(10, 10, PAYMENT_LINK_BASELINE_BPS)) is int


def test_posterior_is_bounded_and_validates():
    assert posterior_bps(5, 10, PAYMENT_LINK_BASELINE_BPS) >= 0
    assert posterior_bps(5, 10, PAYMENT_LINK_BASELINE_BPS) <= 10000
    with pytest.raises(CalibrationError):
        posterior_bps(11, 10, PAYMENT_LINK_BASELINE_BPS)  # recovered > total
    with pytest.raises(CalibrationError):
        posterior_bps(-1, 10, PAYMENT_LINK_BASELINE_BPS)


# ---------------------------------------------------------------------------
# Observation validation
# ---------------------------------------------------------------------------


def test_unknown_outcome_is_rejected_at_construction():
    with pytest.raises(CalibrationError):
        _obs("NOPE")


def test_terminal_flag_is_recomputed_from_outcome():
    assert _obs(OUTCOME_RECOVERED).terminal is True
    assert _obs(OUTCOME_NOT_RECOVERED).terminal is True
    assert _obs(OUTCOME_PENDING).terminal is False


# ---------------------------------------------------------------------------
# Gate + calibration
# ---------------------------------------------------------------------------


def test_below_gate_stays_on_baseline():
    # 9 terminals: below MIN_TOTAL_OBSERVATIONS.
    obs = [_obs(OUTCOME_RECOVERED, event_id=f"e{i}") for i in range(9)]
    row = calibrate_intervention(INTERVENTION, obs)
    assert row.active is False
    assert row.status == STATUS_BASELINE
    assert row.posterior_bps == row.baseline_bps


def test_gate_requires_negative_evidence_even_with_many_positives():
    # 20 terminals but only recovered: MIN_NEGATIVE not met -> baseline.
    obs = [_obs(OUTCOME_RECOVERED, event_id=f"e{i}") for i in range(20)]
    row = calibrate_intervention(INTERVENTION, obs)
    assert row.active is False
    assert row.status == STATUS_BASELINE


def test_gate_requires_positive_evidence_even_with_many_negatives():
    obs = [_obs(OUTCOME_NOT_RECOVERED, event_id=f"e{i}") for i in range(20)]
    row = calibrate_intervention(INTERVENTION, obs)
    assert row.active is False
    assert row.status == STATUS_BASELINE


def test_meeting_gate_activates_calibration():
    ob = [ _obs(OUTCOME_RECOVERED, event_id=f"e{i}") for i in range(6) ] + [
        _obs(OUTCOME_NOT_RECOVERED, event_id=f"n{i}") for i in range(4)
    ]
    row = calibrate_intervention(INTERVENTION, ob)
    assert row.active is True
    assert row.status == STATUS_CALIBRATED
    assert row.observed_total == 10
    assert row.observed_recovered == 6
    assert row.observed_not_recovered == 4
    assert row.posterior_bps == (6 + prior_successes(PAYMENT_LINK_BASELINE_BPS)) * 10000 // (
        10 + cal.PRIOR_STRENGTH
    )


def test_pending_and_unknown_never_count_as_samples():
    # A below-gate aggregate containing PENDING/UNKNOWN still meets nothing.
    terminals = [_obs(OUTCOME_RECOVERED, event_id=f"e{i}") for i in range(5)] + [
        _obs(OUTCOME_NOT_RECOVERED, event_id=f"n{i}") for i in range(5)
    ]
    nonterminal = [_obs(OUTCOME_PENDING, event_id=f"p{i}") for i in range(5)]
    row = calibrate_intervention(INTERVENTION, terminals + nonterminal)
    assert row.observed_total == 10  # PENDING not counted
    assert row.active is True


def test_calibrate_returns_a_row_for_every_frozen_intervention():
    result = calibrate([])
    for intervention in cal.BASE_RECOVERY_BPS:
        assert intervention in result


def test_calibrate_does_not_borrow_samples_across_interventions():
    own = [_obs(OUTCOME_RECOVERED, event_id=f"e{i}") for i in range(6)] + [
        _obs(OUTCOME_NOT_RECOVERED, event_id=f"n{i}") for i in range(4)
    ]
    # own samples reference payment_link only; OTHER_INTERVENTION has none.
    result = calibrate(own)
    assert result[INTERVENTION].active is True
    assert result[OTHER_INTERVENTION].observed_total == 0
    assert result[OTHER_INTERVENTION].active is False


def test_simulated_world_has_no_observations_and_never_calibrates():
    # If the only "evidence" were SIMULATED, the gate can never be met; the
    # projection never produces SIMULATED observations, so calibration treats
    # the intervention as baseline.
    row = calibrate_intervention(INTERVENTION, [])
    assert row.active is False
    assert row.status == STATUS_BASELINE


# ---------------------------------------------------------------------------
# Phase 38: Level-2 Contextual Segment Empirical-Bayes calibration
# ---------------------------------------------------------------------------


def test_segment_prior_split_is_integer_exact_and_sums_to_strength():
    parent_bps = 4000
    s = segment_prior_successes(parent_bps)
    f = segment_prior_failures(parent_bps)
    assert s + f == cal.SEGMENT_PRIOR_STRENGTH
    assert s == 4000 * cal.SEGMENT_PRIOR_STRENGTH // 10000
    assert s == 4
    assert f == 6


def test_segment_posterior_uses_integer_floor_division():
    # 5 total, 4 recovered, parent_bps = 3000 -> prior_s = 3000 * 10 // 10000 = 3
    # posterior = (4 + 3) * 10000 // (5 + 10) = 70000 // 15 = 4666
    parent_bps = 3000
    assert segment_posterior_bps(4, 5, parent_bps) == 4666
    assert type(segment_posterior_bps(4, 5, parent_bps)) is int


def test_segment_calibration_meets_gate_and_shrinks_to_level1_parent():
    obs = [
        CalibrationObservation(
            event_id=f"e{i}",
            intervention=INTERVENTION,
            outcome=OUTCOME_RECOVERED,
            terminal=True,
            amount_paid_paise=1000,
            observed_at=None,
            evidence_id=None,
            evidence_source="webhook",
            failure_reason="bank_timeout",
            payment_method="upi",
        )
        for i in range(4)
    ] + [
        CalibrationObservation(
            event_id=f"e_other_{i}",
            intervention=INTERVENTION,
            outcome=OUTCOME_RECOVERED,
            terminal=True,
            amount_paid_paise=1000,
            observed_at=None,
            evidence_id=None,
            evidence_source="webhook",
            failure_reason="insufficient_funds",
            payment_method="card",
        )
        for i in range(2)
    ] + [
        CalibrationObservation(
            event_id=f"n{i}",
            intervention=INTERVENTION,
            outcome=OUTCOME_NOT_RECOVERED,
            terminal=True,
            amount_paid_paise=None,
            observed_at=None,
            evidence_id=None,
            evidence_source="provider_poll",
            failure_reason="bank_timeout",
            payment_method="upi",
        )
        for i in range(2)
    ] + [
        CalibrationObservation(
            event_id=f"n_other_{i}",
            intervention=INTERVENTION,
            outcome=OUTCOME_NOT_RECOVERED,
            terminal=True,
            amount_paid_paise=None,
            observed_at=None,
            evidence_id=None,
            evidence_source="provider_poll",
            failure_reason="insufficient_funds",
            payment_method="card",
        )
        for i in range(2)
    ]
    row = calibrate_intervention(INTERVENTION, obs)
    assert row.active is True
    l1_posterior = row.posterior_bps
    assert "bank_timeout:upi" in row.segments
    seg = row.segments["bank_timeout:upi"]
    assert isinstance(seg, SegmentCalibration)
    assert seg.active is True
    assert seg.status == STATUS_CALIBRATED
    assert seg.observed_total == 6
    assert seg.observed_recovered == 4
    assert seg.observed_not_recovered == 2
    assert seg.parent_posterior_bps == l1_posterior
    expected_seg_posterior = (4 + segment_prior_successes(l1_posterior)) * 10000 // (
        6 + cal.SEGMENT_PRIOR_STRENGTH
    )
    assert seg.posterior_bps == expected_seg_posterior


def test_sparse_segment_below_gate_stays_inactive_and_falls_back_to_parent():
    obs = [
        CalibrationObservation(
            event_id=f"e{i}",
            intervention=INTERVENTION,
            outcome=OUTCOME_RECOVERED,
            terminal=True,
            amount_paid_paise=1000,
            observed_at=None,
            evidence_id=None,
            evidence_source="webhook",
            failure_reason="bank_timeout",
            payment_method="upi",
        )
        for i in range(2)
    ] + [
        CalibrationObservation(
            event_id=f"e_other_{i}",
            intervention=INTERVENTION,
            outcome=OUTCOME_RECOVERED,
            terminal=True,
            amount_paid_paise=1000,
            observed_at=None,
            evidence_id=None,
            evidence_source="webhook",
            failure_reason="insufficient_funds",
            payment_method="card",
        )
        for i in range(4)
    ] + [
        CalibrationObservation(
            event_id=f"n{i}",
            intervention=INTERVENTION,
            outcome=OUTCOME_NOT_RECOVERED,
            terminal=True,
            amount_paid_paise=None,
            observed_at=None,
            evidence_id=None,
            evidence_source="provider_poll",
            failure_reason="bank_timeout",
            payment_method="upi",
        )
        for i in range(1)
    ] + [
        CalibrationObservation(
            event_id=f"n_other_{i}",
            intervention=INTERVENTION,
            outcome=OUTCOME_NOT_RECOVERED,
            terminal=True,
            amount_paid_paise=None,
            observed_at=None,
            evidence_id=None,
            evidence_source="provider_poll",
            failure_reason="insufficient_funds",
            payment_method="card",
        )
        for i in range(3)
    ]
    row = calibrate_intervention(INTERVENTION, obs)
    assert row.active is True
    l1_posterior = row.posterior_bps
    seg = row.segments["bank_timeout:upi"]
    assert seg.active is False
    assert seg.status == STATUS_BASELINE
    assert seg.observed_total == 3
    assert seg.posterior_bps == l1_posterior

"""Phase 23 tests — the snapshot-driven adaptive estimator.

The wrapper must:
  * preserve the frozen baseline contract exactly (estimate -> RecoveryProbability),
  * use a gated posterior when an active snapshot provides one for the
    intervention, and fall back to the baseline otherwise,
  * be a pure function of (event, classification, intervention) + snapshot,
  * validate its immutable snapshot so no invented probability can enter it,
  * report provenance read-only, without ever rewriting a decision.
"""

from __future__ import annotations

import pytest

from app.adaptive_estimation import (
    CalibratedRecoveryProbabilityEstimator,
    CalibrationSnapshot,
    PROVENANCE_BASELINE,
    PROVENANCE_CALIBRATED,
    PROVENANCE_LEGACY_BASELINE,
    REASON_CALIBRATION_UNAVAILABLE,
    REASON_CALIBRATED_ACTIVE,
    REASON_LEGACY,
    REASON_NO_CALIBRATION,
    REASON_THRESHOLD_NOT_MET,
)
from app.classification import ClassificationResult
from app.economics import PROBABILITY_SCALE, RecoveryProbability
from app.estimator import (
    BASE_RECOVERY_BPS,
    EstimationError,
    RecoveryProbabilityEstimator,
)
from app.models import CustomerHistory, PaymentEvent


class _StubEstimator(RecoveryProbabilityEstimator):
    """A deterministic stand-in for the frozen baseline."""

    def estimate(self, event, classification, intervention):
        return RecoveryProbability(basis_points=BASE_RECOVERY_BPS.get(intervention, 0))


def _stub_event():
    return PaymentEvent(
        event_id="evt_1",
        order_id="order_1",
        payment_id="pay_1",
        customer_id="cust_1",
        amount_paise=100_00_00,
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


def _snapshot(active_bps=None, evidenced=None, version=1):
    return CalibrationSnapshot(
        version=version,
        built_at="2026-01-01T00:00:00+00:00",
        active_bps=active_bps or {},
        evidenced=evidenced or {},
    )


def _estimator(snapshot=None):
    return CalibratedRecoveryProbabilityEstimator(
        baseline=_StubEstimator(), snapshot=snapshot
    )


def test_no_snapshot_behaves_exactly_like_baseline():
    est = _estimator(snapshot=None)
    for intervention, bps in BASE_RECOVERY_BPS.items():
        assert est.estimate(_stub_event(), None, intervention).basis_points == bps
        assert est.provenance(intervention)["status"] == "BASELINE"


def test_return_type_is_recovery_probability():
    est = _estimator(_snapshot())
    assert isinstance(
        est.estimate(_stub_event(), None, "payment_link"), RecoveryProbability
    )


def test_uses_active_posterior_when_present():
    est = _estimator(_snapshot(active_bps={"payment_link": 4400}))
    out = est.estimate(_stub_event(), None, "payment_link")
    assert out.basis_points == 4400


def test_falls_back_to_baseline_when_intervention_not_active():
    est = _estimator(_snapshot(active_bps={"payment_link": 4400}))
    other = next(k for k in BASE_RECOVERY_BPS if k != "payment_link")
    assert (
        est.estimate(_stub_event(), None, other).basis_points
        == BASE_RECOVERY_BPS[other]
    )


def test_posterior_is_bounded_integers():
    est = _estimator(_snapshot(active_bps={"payment_link": PROBABILITY_SCALE}))
    assert est.estimate(_stub_event(), None, "payment_link").basis_points == PROBABILITY_SCALE


def test_snapshot_rejects_out_of_range_posterior():
    with pytest.raises(ValueError):
        _snapshot(active_bps={"payment_link": PROBABILITY_SCALE + 1})
    with pytest.raises(ValueError):
        _snapshot(active_bps={"payment_link": -1})


def test_snapshot_rejects_non_positive_version():
    with pytest.raises(ValueError):
        CalibrationSnapshot(
            version=0,
            built_at="2026-01-01T00:00:00+00:00",
            active_bps={},
            evidenced={},
        )


def test_provenance_reports_calibrated_with_evidence():
    est = _estimator(
        _snapshot(
            active_bps={"payment_link": 4400},
            evidenced={
                "payment_link": {
                    "observed_total": 12,
                    "observed_recovered": 7,
                    "observed_not_recovered": 5,
                    "baseline_bps": 3800,
                }
            },
        )
    )
    prov = est.provenance("payment_link")
    assert prov["status"] == "CALIBRATED"
    assert prov["version"] == 1
    assert prov["posterior_bps"] == 4400
    assert prov["observed_total"] == 12


def test_provenance_is_read_only_never_writes():
    est = _estimator(_snapshot(active_bps={"payment_link": 4400}))
    before = est.snapshot.active_bps
    est.provenance("payment_link")
    assert est.snapshot.active_bps == before


# ---------------------------------------------------------------------------
# Hardening: fallback observability (Issue #3) & decision provenance (Issue #1)
# ---------------------------------------------------------------------------

def test_no_snapshot_is_a_normal_baseline_not_unavailable():
    est = _estimator(snapshot=None)
    assert est.available is True
    prov = est.provenance("payment_link")
    assert prov["status"] == PROVENANCE_BASELINE
    assert prov["reason"] == REASON_NO_CALIBRATION


def test_inactive_snapshot_is_threshold_not_met_baseline():
    est = _estimator(_snapshot(active_bps={}))  # gate unmet / inactive
    assert est.available is True
    prov = est.provenance("payment_link")
    assert prov["status"] == PROVENANCE_BASELINE
    assert prov["reason"] == REASON_THRESHOLD_NOT_MET


def test_unavailable_estimator_is_calibration_unavailable():
    est = CalibratedRecoveryProbabilityEstimator(
        baseline=_StubEstimator(), snapshot=None, available=False
    )
    assert est.available is False
    prov = est.provenance("payment_link")
    assert prov["status"] == PROVENANCE_BASELINE
    assert prov["reason"] == REASON_CALIBRATION_UNAVAILABLE


def test_calibrated_provenance_reports_active_reason():
    est = _estimator(_snapshot(active_bps={"payment_link": 4400}))
    prov = est.provenance("payment_link")
    assert prov["status"] == PROVENANCE_CALIBRATED
    assert prov["reason"] == REASON_CALIBRATED_ACTIVE


def test_decision_provenance_calibrated():
    est = _estimator(
        _snapshot(
            active_bps={"payment_link": 4400},
            evidenced={
                "payment_link": {
                    "observed_total": 12,
                    "observed_recovered": 7,
                    "observed_not_recovered": 5,
                    "baseline_bps": 3800,
                }
            },
        )
    )
    dp = est.decision_provenance("payment_link")
    assert dp["estimator_mode"] == PROVENANCE_CALIBRATED
    assert dp["estimator_reason"] == REASON_CALIBRATED_ACTIVE
    assert dp["estimator_version"] == 1


def test_decision_provenance_baseline_reason_no_calibration():
    est = _estimator(snapshot=None)
    dp = est.decision_provenance("payment_link")
    assert dp["estimator_mode"] == PROVENANCE_BASELINE
    assert dp["estimator_reason"] == REASON_NO_CALIBRATION
    assert dp["estimator_version"] is None


def test_decision_provenance_baseline_reason_unavailable():
    est = CalibratedRecoveryProbabilityEstimator(
        baseline=_StubEstimator(), snapshot=None, available=False
    )
    dp = est.decision_provenance("payment_link")
    assert dp["estimator_mode"] == PROVENANCE_BASELINE
    assert dp["estimator_reason"] == REASON_CALIBRATION_UNAVAILABLE


def test_legacy_baseline_constant_is_distinct():
    assert PROVENANCE_LEGACY_BASELINE != PROVENANCE_BASELINE
    assert REASON_LEGACY == "legacy_decision"


def test_calibrated_estimator_validates_event_classification_event_id_match():
    """DEF-05: Calibrated estimator validates event.event_id == classification.event_id."""
    est = CalibratedRecoveryProbabilityEstimator(
        baseline=RecoveryProbabilityEstimator(),
        snapshot=_snapshot(active_bps={"payment_link": 4400}),
    )
    event = _stub_event()
    mismatched_cls = ClassificationResult(
        event_id="evt_different",
        root_cause_category="transient",
        confidence=0.9,
        reasoning="advisory",
        candidate_interventions=["payment_link"],
    )
    with pytest.raises(EstimationError):
        est.estimate(event, mismatched_cls, "payment_link")


def test_calibrated_estimator_preserves_event_feature_adjustments():
    """DEF-05: Calibrated estimator preserves feature adjustments with empirical delta."""
    baseline = RecoveryProbabilityEstimator()
    est = CalibratedRecoveryProbabilityEstimator(
        baseline=baseline,
        snapshot=_snapshot(active_bps={"payment_link": 4400}),
    )
    event = _stub_event()
    cls = ClassificationResult(
        event_id=event.event_id,
        root_cause_category="transient",
        confidence=0.9,
        reasoning="advisory",
        candidate_interventions=["payment_link"],
    )
    base_prob = baseline.estimate(event, cls, "payment_link")
    cal_prob = est.estimate(event, cls, "payment_link")
    delta_bps = 4400 - BASE_RECOVERY_BPS["payment_link"]
    expected_bps = max(0, min(PROBABILITY_SCALE, base_prob.basis_points + delta_bps))
    assert cal_prob.basis_points == expected_bps
    # Feature adjustments mean base_prob != BASE_RECOVERY_BPS["payment_link"],
    # and cal_prob != 4400 (it is shifted by delta_bps relative to base_prob).
    assert base_prob.basis_points != BASE_RECOVERY_BPS["payment_link"]
    assert cal_prob.basis_points != 4400


def test_hierarchical_contextual_segment_estimation_and_fallback():
    """Phase 38: Level-2 segment posterior is used when active, falls back to Level-1."""
    baseline = RecoveryProbabilityEstimator()
    level1_posterior = 4400
    segment_posterior = 5200

    snapshot = _snapshot(
        active_bps={"payment_link": level1_posterior},
        evidenced={
            "payment_link": {
                "observed_total": 20,
                "observed_recovered": 12,
                "observed_not_recovered": 8,
                "baseline_bps": BASE_RECOVERY_BPS["payment_link"],
                "active_segments": {
                    "bank_timeout:upi": segment_posterior,
                },
            }
        },
    )
    est = CalibratedRecoveryProbabilityEstimator(
        baseline=baseline,
        snapshot=snapshot,
    )

    # Event matching active segment: bank_timeout + upi
    event_seg = PaymentEvent(
        event_id="evt_seg",
        order_id="ord_seg",
        payment_id="pay_seg",
        customer_id="cust_seg",
        amount_paise=100_000,
        currency="INR",
        payment_method="upi",
        failure_reason="bank_timeout",
        bank="HDFC",
        risk_flag="normal",
        customer_history=CustomerHistory(4, 1, True),
        timestamp="2026-08-27T12:00:00+00:00",
    )
    cls_seg = ClassificationResult(
        event_id="evt_seg",
        root_cause_category="transient",
        confidence=0.9,
        reasoning="advisory",
        candidate_interventions=["payment_link"],
    )

    base_prob_seg = baseline.estimate(event_seg, cls_seg, "payment_link")
    cal_prob_seg = est.estimate(event_seg, cls_seg, "payment_link")
    delta_seg = segment_posterior - BASE_RECOVERY_BPS["payment_link"]
    expected_seg_bps = max(0, min(PROBABILITY_SCALE, base_prob_seg.basis_points + delta_seg))
    assert cal_prob_seg.basis_points == expected_seg_bps

    prov_seg = est.provenance("payment_link", failure_reason="bank_timeout", payment_method="upi")
    assert prov_seg["status"] == "CALIBRATED"
    assert prov_seg["posterior_bps"] == segment_posterior
    assert prov_seg["hierarchy_level"] == "segment"

    # Event with sparse/unmodelled segment: insufficient_funds + card
    event_fallback = PaymentEvent(
        event_id="evt_fb",
        order_id="ord_fb",
        payment_id="pay_fb",
        customer_id="cust_fb",
        amount_paise=100_000,
        currency="INR",
        payment_method="card",
        failure_reason="insufficient_funds",
        bank="HDFC",
        risk_flag="normal",
        customer_history=CustomerHistory(4, 1, True),
        timestamp="2026-08-27T12:00:00+00:00",
    )
    cls_fb = ClassificationResult(
        event_id="evt_fb",
        root_cause_category="customer_action_needed",
        confidence=0.9,
        reasoning="advisory",
        candidate_interventions=["payment_link"],
    )

    base_prob_fb = baseline.estimate(event_fallback, cls_fb, "payment_link")
    cal_prob_fb = est.estimate(event_fallback, cls_fb, "payment_link")
    delta_l1 = level1_posterior - BASE_RECOVERY_BPS["payment_link"]
    expected_l1_bps = max(0, min(PROBABILITY_SCALE, base_prob_fb.basis_points + delta_l1))
    assert cal_prob_fb.basis_points == expected_l1_bps

    prov_fb = est.provenance("payment_link", failure_reason="insufficient_funds", payment_method="card")
    assert prov_fb["status"] == "CALIBRATED"
    assert prov_fb["posterior_bps"] == level1_posterior
    assert prov_fb["hierarchy_level"] == "intervention"


def test_decision_estimator_provenance_passes_segment_context():
    """Phase 42: _decision_estimator_provenance passes event failure_reason and payment_method."""
    from app.execution_service import _decision_estimator_provenance

    # Create a snapshot where Level-1 is uncalibrated but Level-2 segment is active
    snap = _snapshot(
        version=3,
        active_bps={},  # No Level-1 active
        evidenced={
            "payment_link": {
                "observed_total": 2,
                "observed_recovered": 1,
                "observed_not_recovered": 1,
                "baseline_bps": 3800,
                "active_segments": {
                    "bank_timeout:upi": 5200,
                },
                "evidenced_segments": {
                    "bank_timeout:upi": {
                        "observed_total": 6,
                        "observed_recovered": 4,
                        "observed_not_recovered": 2,
                        "baseline_bps": 3800,
                    }
                },
            }
        },
    )
    est = CalibratedRecoveryProbabilityEstimator(
        baseline=_StubEstimator(), snapshot=snap
    )

    # Event matching active segment: bank_timeout + upi -> CALIBRATED
    event_seg = PaymentEvent(
        event_id="evt_seg",
        order_id="ord_seg",
        payment_id="pay_seg",
        customer_id="cust_seg",
        amount_paise=100000,
        currency="INR",
        payment_method="upi",
        failure_reason="bank_timeout",
        bank="HDFC",
        risk_flag="normal",
        customer_history=CustomerHistory(4, 1, True),
        timestamp="2026-08-27T12:00:00+00:00",
    )
    prov = _decision_estimator_provenance(est, "payment_link", event=event_seg)
    assert prov["estimator_mode"] == PROVENANCE_CALIBRATED
    assert prov["estimator_version"] == 3
    assert prov["estimator_reason"] == REASON_CALIBRATED_ACTIVE

    # Event not matching segment and no Level-1 -> BASELINE
    event_diff = PaymentEvent(
        event_id="evt_diff",
        order_id="ord_diff",
        payment_id="pay_diff",
        customer_id="cust_diff",
        amount_paise=100000,
        currency="INR",
        payment_method="card",
        failure_reason="insufficient_funds",
        bank="HDFC",
        risk_flag="normal",
        customer_history=CustomerHistory(4, 1, True),
        timestamp="2026-08-27T12:00:00+00:00",
    )
    prov_diff = _decision_estimator_provenance(est, "payment_link", event=event_diff)
    assert prov_diff["estimator_mode"] == PROVENANCE_BASELINE
    assert prov_diff["estimator_version"] == 3

    # Fallback to single-arg stub decision_provenance
    class _SingleArgStub:
        def decision_provenance(self, intervention):
            return {
                "estimator_mode": "STUB_MODE",
                "estimator_version": 99,
                "estimator_reason": "stub_reason",
            }

    stub_prov = _decision_estimator_provenance(_SingleArgStub(), "payment_link", event=event_seg)
    assert stub_prov["estimator_mode"] == "STUB_MODE"
    assert stub_prov["estimator_version"] == 99


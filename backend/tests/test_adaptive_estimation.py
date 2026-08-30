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
)
from app.economics import PROBABILITY_SCALE, RecoveryProbability
from app.estimator import BASE_RECOVERY_BPS, RecoveryProbabilityEstimator
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

"""Operator authentication boundary regression tests (hardening).

The operator/data routers are gated by a single shared bearer credential whose
contract is: fail-closed when unconfigured (503), refuse missing/invalid
credentials (401), and leave the genuinely public boundaries — health probes
and the signature-verified Razorpay webhook — reachable without an operator
key.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.config import OPERATOR_API_KEY_ENV
from app.main import app

# Deliberately NO default credentials: this file asserts the unauthenticated
# and wrong-credential behavior of the operator gate, so every request says
# exactly what it presents.
client = TestClient(app)


def test_operator_endpoint_without_credential_is_refused() -> None:
    response = client.get("/recovery/queue")
    assert response.status_code == 401
    assert "missing operator credentials" in response.json()["detail"]
    assert response.headers.get("www-authenticate") == "Bearer"


def test_operator_endpoint_with_wrong_credential_is_refused() -> None:
    response = client.get(
        "/recovery/queue", headers={"Authorization": "Bearer wrong-key"}
    )
    assert response.status_code == 401
    assert "invalid operator credentials" in response.json()["detail"]


def test_non_bearer_scheme_is_refused() -> None:
    response = client.get(
        "/recovery/queue", headers={"Authorization": "Token something"}
    )
    assert response.status_code == 401
    assert "Bearer scheme" in response.json()["detail"]


def test_malformed_authorization_value_is_refused() -> None:
    response = client.get(
        "/recovery/queue", headers={"Authorization": "basic"}
    )
    assert response.status_code == 401


def test_valid_credential_is_accepted() -> None:
    response = client.get(
        "/recovery/queue",
        headers={"Authorization": "Bearer test-operator-api-key"},
    )
    assert response.status_code == 200


def test_key_read_at_request_time_so_rotation_takes_effect_immediately(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(OPERATOR_API_KEY_ENV, "rotated-key")
    try:
        old = client.get(
            "/recovery/queue", headers={"Authorization": "Bearer test-operator-api-key"}
        )
        assert old.status_code == 401
        new = client.get(
            "/recovery/queue", headers={"Authorization": "Bearer rotated-key"}
        )
        assert new.status_code == 200
    finally:
        monkeypatch.delenv(OPERATOR_API_KEY_ENV, raising=False)


def test_unconfigured_key_fails_closed_with_503(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv(OPERATOR_API_KEY_ENV, raising=False)
    response = client.get(
        "/recovery/queue", headers={"Authorization": "Bearer anything"}
    )
    assert response.status_code == 503
    assert "not configured" in response.json()["detail"]


def test_health_probes_stay_public_without_credential() -> None:
    assert client.get("/health").status_code == 200
    assert client.get("/health/ready").status_code == 200


def test_webhook_reaches_its_own_gate_not_the_operator_gate() -> None:
    """The webhook must not 401 on a missing operator bearer.

    A body without a Razorpay signature is rejected by the webhook's own
    signature verification (400 missing_signature) — proving the request got
    PAST the operator gate to the boundary that legitimately authenticates it.
    """
    response = client.post(
        "/webhook/razorpay",
        content=b'{"event": "payment_link.paid"}',
        headers={"Content-Type": "application/json"},
    )
    assert response.status_code == 400
    assert response.json()["status"] == "missing_signature"
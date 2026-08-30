"""Health/readiness endpoint tests for the RecoveryOS backend foundation."""

import sqlite3

import pytest
from fastapi.testclient import TestClient

from app.main import HEALTH_RESPONSE, app

client = TestClient(app)


def test_health_endpoint_returns_deterministic_response() -> None:
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == HEALTH_RESPONSE


def test_health_ready_reports_ready_over_a_usable_database() -> None:
    response = client.get("/health/ready")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ready"
    assert body["database_usable"] is True
    # Configuration is reported as booleans only — never any secret value.
    config = body["configuration"]
    assert isinstance(config["razorpay_test_mode"]["configured"], bool)
    assert isinstance(config["razorpay_webhook"]["configured"], bool)
    assert isinstance(config["omniroute"]["configured"], bool)
    assert "policy" in config


def test_health_ready_never_exposes_secret_values() -> None:
    body = client.get("/health/ready").json()
    raw = str(body)
    # Even when configured, no credential material may appear in the response.
    for secret in (
        "RAZORPAY_KEY_ID",
        "RAZORPAY_KEY_SECRET",
        "RAZORPAY_WEBHOOK_SECRET",
        "OMNIROUTE_API_KEY",
        "rzp_test_",
        "rzp_live_",
    ):
        assert secret not in raw


def test_health_ready_degraded_when_database_unusable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def boom() -> sqlite3.Connection:
        raise sqlite3.OperationalError("unable to open database file")

    monkeypatch.setattr("app.main.connect_database", boom)
    response = client.get("/health/ready")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "degraded"
    assert body["database_usable"] is False

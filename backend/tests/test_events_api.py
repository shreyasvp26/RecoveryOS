"""Phase 4 API tests for the minimal POST /events ingestion endpoint."""

from __future__ import annotations

from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)


def _set_test_db(monkeypatch, tmp_path) -> None:
    db_path = tmp_path / "api_events.db"
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{db_path}")


def payload(**overrides) -> dict:
    base = {
        "event_id": "evt_100",
        "order_id": "order_100",
        "payment_id": "pay_100",
        "customer_id": "cust_100",
        "amount_paise": 75000,
        "currency": "INR",
        "payment_method": "card",
        "failure_reason": "bank_timeout",
        "bank": "HDFC",
        "risk_flag": "normal",
        "customer_history": {
            "prior_successful_payments": 4,
            "prior_failed_payments": 1,
            "has_active_subscription": True,
        },
        "timestamp": "2026-08-27T12:00:00+00:00",
    }
    base.update(overrides)
    return base


def test_valid_request_returns_success(monkeypatch, tmp_path) -> None:
    _set_test_db(monkeypatch, tmp_path)
    response = client.post("/events", json=payload())
    assert response.status_code == 201
    body = response.json()
    assert body["status"] == "success"
    assert body["event_id"] == "evt_100"


def test_invalid_request_returns_validation_error(monkeypatch, tmp_path) -> None:
    _set_test_db(monkeypatch, tmp_path)
    response = client.post("/events", json=payload(payment_method="crypto"))
    assert response.status_code == 422
    body = response.json()
    assert body["status"] == "invalid"


def test_missing_required_field_returns_validation_error(
    monkeypatch, tmp_path
) -> None:
    _set_test_db(monkeypatch, tmp_path)
    response = client.post("/events", json={"event_id": "evt_200"})
    assert response.status_code == 422
    assert response.json()["status"] == "invalid"


def test_non_object_body_returns_validation_error(monkeypatch, tmp_path) -> None:
    _set_test_db(monkeypatch, tmp_path)
    response = client.post("/events", json=[1, 2, 3])
    assert response.status_code == 422


def test_duplicate_request_returns_deterministic_response(
    monkeypatch, tmp_path
) -> None:
    _set_test_db(monkeypatch, tmp_path)
    first = client.post("/events", json=payload())
    second = client.post("/events", json=payload())
    assert first.status_code == 201
    assert second.status_code == 409
    body = second.json()
    assert body["status"] == "duplicate"
    assert body["event_id"] == "evt_100"


def test_health_endpoint_still_available() -> None:
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
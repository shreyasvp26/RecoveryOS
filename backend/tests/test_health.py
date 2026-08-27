"""Health endpoint smoke test for the RecoveryOS backend foundation."""

from fastapi.testclient import TestClient

from app.main import HEALTH_RESPONSE, app

client = TestClient(app)


def test_health_endpoint_returns_deterministic_response() -> None:
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == HEALTH_RESPONSE

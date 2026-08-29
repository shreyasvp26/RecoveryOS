"""Phase 19: the Policy Lab HTTP boundary.

Validation must happen server-side, results must be labelled simulated, and no
route may leak hidden ground truth, touch the database, or reach a provider.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.policy_scenario import (
    BUILT_IN_SCENARIO_IDS,
    CUSTOM_MAX_MAX_INTERVENTIONS,
    IMMUTABLE_PROTECTIONS,
    current_scenario,
)
from app.routes.replay import MAX_SCENARIOS_PER_COMPARISON


@pytest.fixture(scope="module")
def client() -> TestClient:
    return TestClient(app)


def valid_custom(**overrides) -> dict:
    parameters = dict(current_scenario().parameters)
    parameters.update(overrides)
    return parameters


@pytest.fixture(scope="module")
def comparison(client: TestClient) -> dict:
    """One real canonical comparison, reused by the read-only assertions."""
    response = client.post(
        "/replay/compare",
        json={
            "scenarios": [
                {"scenario_id": "current"},
                {"scenario_id": "conservative"},
                {"scenario_id": "aggressive"},
            ],
            "reference_scenario_id": "current",
        },
    )
    assert response.status_code == 200
    return response.json()


# ---------------------------------------------------------------------------
# GET /replay/scenarios
# ---------------------------------------------------------------------------


def test_scenarios_endpoint_lists_the_built_in_scenarios(client: TestClient):
    payload = client.get("/replay/scenarios").json()

    assert [s["scenario_id"] for s in payload["scenarios"]] == list(
        BUILT_IN_SCENARIO_IDS
    )
    assert payload["reference_scenario_id"] == "current"


def test_scenarios_endpoint_publishes_the_real_current_policy(client: TestClient):
    payload = client.get("/replay/scenarios").json()
    current = next(
        s for s in payload["scenarios"] if s["scenario_id"] == "current"
    )

    assert current["parameters"] == current_scenario().parameters


def test_scenarios_endpoint_publishes_the_locked_protections(client: TestClient):
    payload = client.get("/replay/scenarios").json()

    assert payload["immutable_protections"] == list(IMMUTABLE_PROTECTIONS)
    for scenario in payload["scenarios"]:
        assert scenario["immutable_protections"] == list(IMMUTABLE_PROTECTIONS)


def test_scenarios_endpoint_publishes_custom_bounds(client: TestClient):
    bounds = client.get("/replay/scenarios").json()["custom"]["bounds"]

    assert set(bounds) == {
        "max_interventions_per_customer_24h",
        "event_cooldown_minutes",
        "daily_spend_cap_paise",
    }
    for entry in bounds.values():
        assert entry["minimum"] <= entry["maximum"]


def test_scenarios_endpoint_offers_no_bound_for_a_locked_protection(
    client: TestClient,
):
    bounds = client.get("/replay/scenarios").json()["custom"]["bounds"]
    assert not (set(bounds) & set(IMMUTABLE_PROTECTIONS))


def test_scenarios_endpoint_is_deterministic(client: TestClient):
    assert client.get("/replay/scenarios").json() == (
        client.get("/replay/scenarios").json()
    )


# ---------------------------------------------------------------------------
# POST /replay/compare — success
# ---------------------------------------------------------------------------


def test_compare_returns_a_result_for_every_requested_scenario(comparison):
    assert comparison["status"] == "replay_success"
    assert [s["scenario"]["scenario_id"] for s in comparison["scenarios"]] == [
        "current",
        "conservative",
        "aggressive",
    ]


def test_compare_labels_every_result_as_simulated(comparison):
    assert comparison["replay_mode"] == "SIMULATED"
    assert comparison["result_type"] == "simulated_policy_replay"
    assert "not production revenue forecasts" in comparison["disclaimer"]
    for entry in comparison["scenarios"]:
        assert entry["metrics"]["replay_mode"] == "SIMULATED"


def test_compare_never_calls_replay_revenue_actual(comparison):
    import json

    payload = json.dumps(comparison).lower()
    assert "actual_recovered_revenue" not in payload
    assert "actual recovered revenue" not in payload
    assert "simulated_recovered_revenue_paise" in payload


def test_compare_publishes_passing_fairness_checks(comparison):
    assert comparison["fairness"]
    assert all(comparison["fairness"].values())


def test_compare_reports_real_metrics_not_placeholders(comparison):
    for entry in comparison["scenarios"]:
        financial = entry["metrics"]["financial"]
        assert financial["simulated_recovered_revenue_paise"] > 0
        assert financial["recoverable_revenue_paise"] > 0
        assert entry["metrics"]["intervention"]["total_interventions"] > 0
        assert entry["metrics"]["event_count"] == 500


def test_compare_reports_a_genuine_difference_for_a_stricter_policy(comparison):
    conservative = next(
        s
        for s in comparison["scenarios"]
        if s["scenario"]["scenario_id"] == "conservative"
    )

    assert conservative["vs_reference"]["incremental_recovered_revenue_paise"] < 0
    assert conservative["vs_reference"]["incremental_blocked_interventions"] > 0
    assert conservative["decision_deltas"]


def test_compare_exposes_event_level_decision_deltas(comparison):
    conservative = next(
        s
        for s in comparison["scenarios"]
        if s["scenario"]["scenario_id"] == "conservative"
    )
    delta = conservative["decision_deltas"][0]

    assert delta["event_id"].startswith("evt_")
    assert delta["reference"]["selected_intervention"]
    assert delta["candidate"]["selected_intervention"]
    assert delta["delta_type"]


def test_compare_reports_no_safety_regression_under_any_scenario(comparison):
    for entry in comparison["scenarios"]:
        safety = entry["metrics"]["safety"]
        assert safety["fraud_interventions"] == 0
        assert safety["terminal_interventions"] == 0
        assert safety["unauthorized_attempts"] == 0


def test_compare_reports_failures_explicitly(comparison):
    for entry in comparison["scenarios"]:
        assert entry["metrics"]["failures"] == 0
        assert "failures_by_category" in entry["metrics"]


def test_compare_marks_exactly_one_reference(comparison):
    references = [s for s in comparison["scenarios"] if s["is_reference"]]
    assert len(references) == 1
    assert references[0]["scenario"]["scenario_id"] == "current"


def test_compare_is_deterministic_across_requests(client: TestClient):
    body = {
        "scenarios": [
            {"scenario_id": "current"},
            {"scenario_id": "conservative"},
        ],
        "reference_scenario_id": "current",
    }
    first = client.post("/replay/compare", json=body).json()
    second = client.post("/replay/compare", json=body).json()

    assert first == second


def test_compare_accepts_a_custom_scenario(client: TestClient):
    response = client.post(
        "/replay/compare",
        json={
            "scenarios": [
                {"scenario_id": "current"},
                {
                    "scenario_id": "custom",
                    "name": "Tighter limit",
                    "parameters": valid_custom(
                        max_interventions_per_customer_24h=1
                    ),
                },
            ],
            "reference_scenario_id": "current",
        },
    )
    payload = response.json()

    assert response.status_code == 200
    custom = next(
        s for s in payload["scenarios"] if s["scenario"]["scenario_id"] == "custom"
    )
    assert custom["scenario"]["name"] == "Tighter limit"
    assert custom["scenario"]["parameters"][
        "max_interventions_per_customer_24h"
    ] == 1


def test_compare_defaults_the_reference_to_the_current_policy(
    client: TestClient,
):
    response = client.post(
        "/replay/compare",
        json={
            "scenarios": [
                {"scenario_id": "current"},
                {"scenario_id": "aggressive"},
            ]
        },
    )

    assert response.status_code == 200
    assert response.json()["reference_scenario_id"] == "current"


# ---------------------------------------------------------------------------
# POST /replay/compare — server-side validation
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "overrides",
    [
        {"max_interventions_per_customer_24h": 0},
        {"max_interventions_per_customer_24h": -5},
        {"max_interventions_per_customer_24h": CUSTOM_MAX_MAX_INTERVENTIONS + 1},
        {"event_cooldown_minutes": -1},
        {"daily_spend_cap_paise": -1},
        {"max_interventions_per_customer_24h": "two"},
        {"max_interventions_per_customer_24h": 2.5},
        {"max_interventions_per_customer_24h": None},
    ],
)
def test_compare_rejects_an_invalid_custom_policy(
    client: TestClient, overrides: dict
):
    response = client.post(
        "/replay/compare",
        json={
            "scenarios": [
                {"scenario_id": "current"},
                {"scenario_id": "custom", "parameters": valid_custom(**overrides)},
            ]
        },
    )

    assert response.status_code == 422
    assert response.json()["status"] == "invalid_scenario"


@pytest.mark.parametrize(
    "key",
    ["fraud_protection", "terminal_protection", "duplicate_protection"],
)
def test_compare_refuses_an_attempt_to_disable_a_protection(
    client: TestClient, key: str
):
    parameters = valid_custom()
    parameters[key] = False
    response = client.post(
        "/replay/compare",
        json={
            "scenarios": [
                {"scenario_id": "current"},
                {"scenario_id": "custom", "parameters": parameters},
            ]
        },
    )

    assert response.status_code == 422
    assert "cannot be configured" in response.json()["detail"]


def test_compare_rejects_an_unknown_scenario(client: TestClient):
    response = client.post(
        "/replay/compare", json={"scenarios": [{"scenario_id": "reckless"}]}
    )

    assert response.status_code == 422
    assert "unknown scenario" in response.json()["detail"]


def test_compare_rejects_parameters_on_a_built_in_scenario(client: TestClient):
    """'current' must always mean the actual current policy."""
    response = client.post(
        "/replay/compare",
        json={
            "scenarios": [
                {"scenario_id": "current", "parameters": valid_custom()}
            ]
        },
    )

    assert response.status_code == 422
    assert "fixed parameters" in response.json()["detail"]


def test_compare_reports_which_scenario_was_invalid(client: TestClient):
    response = client.post(
        "/replay/compare",
        json={
            "scenarios": [
                {"scenario_id": "current"},
                {"scenario_id": "conservative"},
                {"scenario_id": "custom", "parameters": valid_custom(
                    daily_spend_cap_paise=-1
                )},
            ]
        },
    )

    assert response.status_code == 422
    assert response.json()["scenario_index"] == 2


@pytest.mark.parametrize(
    "body",
    [
        {},
        {"scenarios": []},
        {"scenarios": "current"},
        {"scenarios": [{"scenario_id": "current"}], "reference_scenario_id": ""},
        {"scenarios": [{"scenario_id": ""}]},
        {"scenarios": [None]},
    ],
)
def test_compare_rejects_a_malformed_request(client: TestClient, body: dict):
    assert client.post("/replay/compare", json=body).status_code == 422


def test_compare_rejects_a_duplicate_scenario(client: TestClient):
    response = client.post(
        "/replay/compare",
        json={
            "scenarios": [
                {"scenario_id": "current"},
                {"scenario_id": "current"},
            ]
        },
    )

    assert response.status_code == 422
    assert "at most once" in response.json()["detail"]


def test_compare_rejects_a_reference_that_was_not_replayed(client: TestClient):
    response = client.post(
        "/replay/compare",
        json={
            "scenarios": [{"scenario_id": "current"}],
            "reference_scenario_id": "aggressive",
        },
    )

    assert response.status_code == 422
    assert "must be one of the requested scenarios" in response.json()["detail"]


def test_compare_bounds_the_number_of_scenarios(client: TestClient):
    scenarios = [
        {
            "scenario_id": "custom",
            "parameters": valid_custom(max_interventions_per_customer_24h=n),
        }
        for n in range(1, MAX_SCENARIOS_PER_COMPARISON + 2)
    ]
    response = client.post("/replay/compare", json={"scenarios": scenarios})

    assert response.status_code == 422
    assert "at most" in response.json()["detail"]


def test_a_rejected_request_evaluates_nothing(client: TestClient, monkeypatch):
    from app.routes import replay as replay_route

    def forbidden(*args, **kwargs):
        raise AssertionError("a rejected request must never replay anything")

    monkeypatch.setattr(replay_route, "replay_scenarios", forbidden)
    response = client.post(
        "/replay/compare",
        json={
            "scenarios": [
                {"scenario_id": "custom", "parameters": valid_custom(
                    max_interventions_per_customer_24h=-1
                )}
            ]
        },
    )

    assert response.status_code == 422


# ---------------------------------------------------------------------------
# Leakage, isolation and safety at the boundary
# ---------------------------------------------------------------------------


def test_the_api_never_exposes_hidden_ground_truth(comparison):
    import json

    payload = json.dumps(comparison).lower()
    for leak in (
        "true_probability",
        "true_ev",
        "draw_bps",
        "oracle",
        "hidden_world",
    ):
        assert leak not in payload, leak


def test_the_api_never_exposes_a_secret(comparison, client: TestClient):
    import json

    payload = (
        json.dumps(comparison) + json.dumps(client.get("/replay/scenarios").json())
    ).lower()
    for secret in ("rzp_", "key_secret", "api_key", "webhook_secret", "password"):
        assert secret not in payload, secret


def test_the_replay_api_never_calls_razorpay(client: TestClient, monkeypatch):
    calls: list = []
    monkeypatch.setattr(
        "app.razorpay_client.RazorpayPaymentLinkClient.create_payment_link",
        lambda self, *a, **k: calls.append((a, k)),
    )
    monkeypatch.setattr(
        "app.config.build_razorpay_client",
        lambda: (_ for _ in ()).throw(
            AssertionError("the Policy Lab must never build a Razorpay client")
        ),
    )
    response = client.post(
        "/replay/compare", json={"scenarios": [{"scenario_id": "current"}]}
    )

    assert response.status_code == 200
    assert calls == []


def test_the_replay_api_never_writes_to_the_database(client: TestClient, monkeypatch):
    for name in (
        "insert_intervention_attempt",
        "insert_execution_outcome",
        "insert_policy_decision",
        "insert_optimizer_decision",
    ):
        monkeypatch.setattr(
            f"app.db.{name}",
            lambda *a, **k: (_ for _ in ()).throw(
                AssertionError("the Policy Lab must never persist anything")
            ),
        )
    response = client.post(
        "/replay/compare",
        json={
            "scenarios": [
                {"scenario_id": "current"},
                {"scenario_id": "aggressive"},
            ]
        },
    )

    assert response.status_code == 200


def test_the_replay_api_does_not_mutate_the_active_policy(client: TestClient):
    from app import config as config_module

    before = config_module.build_policy_config()
    client.post(
        "/replay/compare",
        json={
            "scenarios": [
                {"scenario_id": "current"},
                {
                    "scenario_id": "custom",
                    "parameters": valid_custom(
                        max_interventions_per_customer_24h=CUSTOM_MAX_MAX_INTERVENTIONS
                    ),
                },
            ]
        },
    )

    assert config_module.build_policy_config() == before


# ---------------------------------------------------------------------------
# The existing API is untouched
# ---------------------------------------------------------------------------


def test_the_health_endpoint_still_works(client: TestClient):
    assert client.get("/health").json() == {"status": "ok"}


def test_the_replay_routes_do_not_shadow_an_existing_route(client: TestClient):
    paths = set(client.get("/openapi.json").json()["paths"])

    assert "/replay/scenarios" in paths
    assert "/replay/compare" in paths
    for existing in (
        "/health",
        "/events",
        "/dashboard/summary",
        "/decisions/blocked",
    ):
        assert existing in paths

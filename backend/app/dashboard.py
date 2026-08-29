"""Phase 10 operator dashboard responders (read-only).

These functions assemble the Recovery Command Center, the Event Decision
Trace, and the Policy & Blocked Actions payloads from persisted state. They
only READ the database and reuse the locked Phase 9 metric readers; they never
make, change, or fabricate a decision, never recompute policy or benchmark
logic, and never expose hidden outcome ground truth. Anything the repository
cannot substantiate is reported as explicitly unavailable rather than invented.
"""

from __future__ import annotations

from typing import Any

from . import db
from .benchmark_metrics import (
    incremental_over_no_action,
    recovery_efficiency,
    recovery_rate,
    recoveryos_vs_naive_retry,
    strategy_result,
)
from .policy import (
    RULE_COOLDOWN,
    RULE_CUSTOMER_LIMIT,
    RULE_DUPLICATE,
    RULE_FRAUD,
    RULE_SPEND_CAP,
    RULE_TERMINAL,
)

# Human-readable labels for the deterministic policy gate's blocked rules.
POLICY_RULE_LABELS: dict[str, str] = {
    RULE_FRAUD: "Fraud protection",
    RULE_TERMINAL: "Terminal failure",
    RULE_DUPLICATE: "Duplicate intervention",
    RULE_CUSTOMER_LIMIT: "Customer intervention limit exceeded",
    RULE_COOLDOWN: "Event cooldown active",
    RULE_SPEND_CAP: "Spend cap exceeded",
}

# Group blocked decisions into the Policy & Blocks screen categories.
BLOCK_CATEGORY_ORDER: tuple[str, ...] = (
    "fraud",
    "retry_limit",
    "cooldown",
    "terminal",
    "duplicate",
    "spend_cap",
)
BLOCK_CATEGORY_LABELS: dict[str, str] = {
    "fraud": "Fraud",
    "retry_limit": "Retry limit",
    "cooldown": "Cooldown",
    "terminal": "Terminal failure",
    "duplicate": "Duplicate",
    "spend_cap": "Spend cap",
}
_BLOCK_RULE_TO_CATEGORY: dict[str, str] = {
    RULE_FRAUD: "fraud",
    RULE_CUSTOMER_LIMIT: "retry_limit",
    RULE_COOLDOWN: "cooldown",
    RULE_TERMINAL: "terminal",
    RULE_DUPLICATE: "duplicate",
    RULE_SPEND_CAP: "spend_cap",
}

# Explicitness when the repository defines no canonical value.
RECOVERABLE_REVENUE_UNAVAILABLE_NOTE = (
    "Recoverable revenue has no canonical definition in the repository; the "
    "hidden outcome model is evaluation ground truth and is intentionally not "
    "exposed to the operator dashboard."
)
NOT_RECOVERED_NOTE = (
    "The durable pipeline records execution, not per-event simulated recovery. "
    "Money not acted on is grouped here from persisted state; recovery "
    "effectiveness belongs to the separate simulated benchmark."
)


def rule_label(rule_id: str | None) -> str | None:
    """Return the human-readable label for a policy rule id."""
    if not rule_id:
        return None
    return POLICY_RULE_LABELS.get(rule_id, rule_id)


def block_category(rule_id: str | None) -> str:
    """Return the Policy & Blocks category key for a denial rule id."""
    if rule_id is None:
        return "other"
    return _BLOCK_RULE_TO_CATEGORY.get(rule_id, "other")


def block_category_label(category: str) -> str:
    """Return the display label for a block category key."""
    return BLOCK_CATEGORY_LABELS.get(category, category)


def _strategy_with_metrics(result: Any) -> dict[str, Any]:
    """Format one persisted strategy result with its Phase 9-derived metrics.

    recovery_rate and efficiency use the exact frozen Phase 9 definitions
    (recovered_events / event_count; recovered_amount / interventions, None
    when no interventions). The formulas are applied by the backend and the
    frontend never recomputes them.
    """
    efficiency = recovery_efficiency(result)
    return {
        "strategy": result.strategy,
        "interventions_attempted": result.interventions_attempted,
        "successful_interventions": result.successful_interventions,
        "recovered_events": result.recovered_events,
        "recovered_amount_paise": result.recovered_amount_paise,
        "recovery_rate": recovery_rate(result),
        "efficiency_paise_per_intervention": efficiency,
    }


def _phase17_benchmark_payload(
    latest: dict[str, Any], summary: dict[str, Any]
) -> dict[str, Any]:
    """Shape a persisted Phase 17 summary for the Command Center.

    Purely a projection of figures the backend already computed: the frontend
    never recomputes a metric, and no per-event hidden probability, draw or
    oracle option table is included — only the aggregate evaluation results the
    benchmark view exists to show.
    """
    strategies = summary["strategies"]
    v1 = strategies["recoveryos_v1"]
    v2 = strategies["recoveryos_v2"]
    return {
        "available": True,
        "methodology": summary["config"]["methodology"],
        "run_id": latest["run_id"],
        "seed": latest["seed"],
        "event_count": latest["event_count"],
        "evaluation_mode": latest["evaluation_mode"],
        "saved_at": latest["saved_at"],
        "strategies": [
            {
                "strategy": strategy,
                "label": label,
                "recovered_amount_paise": strategies[strategy][
                    "recovered_revenue_paise"
                ],
                "incremental_vs_no_action_paise": strategies[strategy][
                    "incremental_vs_no_action_paise"
                ],
                "interventions_attempted": strategies[strategy][
                    "interventions_attempted"
                ],
                "efficiency_paise_per_intervention": strategies[strategy][
                    "recovery_efficiency_paise"
                ],
                "total_regret_paise": strategies[strategy]["total_regret_paise"],
                "optimal_selection_rate": strategies[strategy][
                    "optimal_selection_rate"
                ],
                "unauthorized_attempts": strategies[strategy][
                    "unauthorized_attempts"
                ],
                "fraud_intervention_rate": strategies[strategy][
                    "fraud_intervention_rate"
                ],
                "exceptions": strategies[strategy]["exceptions"],
            }
            for strategy, label in (
                ("no_action", "No Action"),
                ("naive_retry", "Naive Retry"),
                ("recoveryos_v1", "RecoveryOS V1"),
                ("recoveryos_v2", "RecoveryOS V2"),
                ("oracle", "Oracle"),
            )
        ],
        "recovery_os_recovered_amount_paise": v2["recovered_revenue_paise"],
        # The same frozen Phase 9 recovery-rate definition (recovered events
        # over the shared event set), applied to the V2 arm.
        "recovery_os_recovery_rate": (
            v2["recovered_events"] / v2["event_count"] if v2["event_count"] else None
        ),
        "incremental_over_no_action_paise": v2["incremental_vs_no_action_paise"],
        "v2_vs_v1_paise": v2["incremental_vs_v1_paise"],
        "v2_oracle_value_capture": v2["incremental_oracle_value_capture"],
        "v1_total_regret_paise": v1["total_regret_paise"],
        "v2_total_regret_paise": v2["total_regret_paise"],
        "verdict": summary["result"]["verdict"],
        "fairness": summary.get("fairness"),
    }


def _benchmark_payload(conn) -> dict[str, Any]:
    """Assemble the benchmark comparison from the latest persisted run.

    The store holds whichever methodology was last persisted, so the reader
    dispatches on it rather than assuming Phase 9's three-strategy shape.
    """
    latest = db.get_latest_benchmark_run(conn)
    if latest is None:
        return {"available": False}
    summary = latest["summary"]
    methodology = ""
    if isinstance(summary, dict) and isinstance(summary.get("config"), dict):
        methodology = str(summary["config"].get("methodology", ""))
    if methodology.startswith("phase17"):
        try:
            return _phase17_benchmark_payload(latest, summary)
        except (KeyError, TypeError):
            return {"available": False, "error": "corrupt persisted benchmark summary"}
    try:
        run = _benchmark_run_from_summary(summary)
    except Exception:
        return {"available": False, "error": "corrupt persisted benchmark summary"}
    strategies = [
        _strategy_with_metrics(strategy_result(run, s))
        for s in ("no_action", "naive_retry", "recovery_os")
    ]
    recovery_os = strategy_result(run, "recovery_os")
    return {
        "available": True,
        "run_id": latest["run_id"],
        "seed": latest["seed"],
        "event_count": latest["event_count"],
        "evaluation_mode": latest["evaluation_mode"],
        "saved_at": latest["saved_at"],
        "strategies": strategies,
        # The canonical Phase 9 RecoveryOS recovery rate and simulated recovered
        # amount, surfaced as primary KPIs. Both are the frozen metric readers
        # applied to the persisted run; the frontend only renders them.
        "recovery_os_recovery_rate": recovery_rate(recovery_os),
        "recovery_os_recovered_amount_paise": recovery_os.recovered_amount_paise,
        "incremental_over_no_action_paise": incremental_over_no_action(run),
        "recoveryos_vs_naive_retry_paise": recoveryos_vs_naive_retry(run),
    }


def _benchmark_run_from_summary(summary: dict[str, Any]):
    """Reconstruct a BenchmarkRunResult from a persisted run summary dict."""
    from .benchmark import BenchmarkRunResult, BenchmarkStrategyResult

    strategy_results = tuple(
        BenchmarkStrategyResult(**item) for item in summary["strategy_results"]
    )
    return BenchmarkRunResult(
        run_id=summary["run_id"],
        seed=summary["seed"],
        event_count=summary["event_count"],
        model_seed=summary["model_seed"],
        evaluation_time=summary["evaluation_time"],
        evaluation_mode=summary["evaluation_mode"],
        strategy_results=strategy_results,
    )


def build_dashboard_summary(conn) -> dict[str, Any]:
    """Assemble the Recovery Command Center payload from persisted state."""
    decision_stats = db.get_policy_decision_stats(conn)
    execution_stats = db.get_execution_outcome_stats(conn)
    blocked = db.get_policy_blocked_event_amounts(conn)
    unclassified = db.get_unclassified_event_amounts(conn)

    return {
        "generated_at": _now_iso(),
        "operational": {
            "event_count": db.count_payment_events(conn),
            "revenue_at_risk_paise": db.sum_event_amount_paise(conn),
            "revenue_at_risk_source": "sum of ingested failed-payment amounts",
            "interventions_executed": execution_stats["total"],
            "interventions_executed_success": execution_stats["success"],
            "blocked_interventions": decision_stats["denied"],
            "policy_decisions_total": decision_stats["total"],
            "fraud_actions_blocked": db.count_denied_on_fraud_events(conn),
        },
        "recoverable_revenue": {
            "defined": False,
            "note": RECOVERABLE_REVENUE_UNAVAILABLE_NOTE,
        },
        "benchmark": _benchmark_payload(conn),
        "not_recovered": {
            "available": True,
            "note": NOT_RECOVERED_NOTE,
            "categories": [
                {
                    "key": "policy_blocked",
                    "label": "Policy blocked",
                    "count": blocked["count"],
                    "amount_paise": blocked["amount_paise"],
                },
                {
                    "key": "unclassified",
                    "label": "No AI classification",
                    "count": unclassified["count"],
                    "amount_paise": unclassified["amount_paise"],
                },
            ],
        },
    }


def build_event_trace(conn, event_id: str) -> dict[str, Any] | None:
    """Assemble the historical decision chain for one event, or None if absent."""
    from .db import get_classification_result, get_payment_event

    event = get_payment_event(conn, event_id)
    if event is None:
        return None

    stored = get_classification_result(conn, event_id)
    classification = stored.to_dict() if stored is not None else None

    decisions = db.get_policy_decisions_for_event(conn, event_id)
    executions = db.get_execution_outcomes_for_event(conn, event_id)
    attempts = db.get_intervention_attempts_for_event(conn, event_id)
    # Phase 18: the economic stage between policy and execution. These are the
    # optimizer's own persisted MODEL ESTIMATES — never benchmark ground truth,
    # and never recomputed here. An empty list means no V2 economic decision is
    # recorded for this event, which is reported as such rather than invented.
    optimizer_decisions = db.get_optimizer_decisions_for_event(conn, event_id)

    return {
        "event": event.to_dict(),
        "classification": classification,
        "policy_decisions": decisions,
        "optimizer_decisions": optimizer_decisions,
        "executions": executions,
        "attempts": attempts,
        "phase12": _summarize_phase12(conn, executions),
        "summary": _summarize_trace(classification, decisions, executions),
    }


def _summarize_phase12(
    conn, executions: list[dict[str, Any]]
) -> dict[str, Any]:
    """Derive the honest Phase 12 closed-loop status from persisted executions.

    Only REAL_RAZORPAY payment_link SUCCESS executions carry a genuine Payment
    Link id; each is reported as ``waiting`` until a verified webhook recovery
    outcome exists for that link, at which point it is ``recovered`` with the
    trusted amount_paid. A recovered link is never counted twice. This is
    read-only evidence; it never fabricates a recovery and never re-executes.
    """
    links = []
    for ex in executions:
        if (
            ex.get("intervention") != "payment_link"
            or ex.get("execution_mode") != "REAL_RAZORPAY"
            or ex.get("status") != "SUCCESS"
            or not ex.get("payment_link_id")
        ):
            continue
        payment_link_id = ex["payment_link_id"]
        recovery = db.get_webhook_recovery_outcome_by_payment_link_id(
            conn, payment_link_id
        )
        if recovery is not None:
            links.append(
                {
                    "payment_link_id": payment_link_id,
                    "status": "recovered",
                    "recovered_amount_paise": recovery.get("amount_paid_paise"),
                    "recovered_at": recovery.get("recovered_at"),
                    "payment_id": recovery.get("payment_id"),
                }
            )
        else:
            links.append(
                {
                    "payment_link_id": payment_link_id,
                    "status": "waiting",
                    "recovered_amount_paise": None,
                    "recovered_at": None,
                    "payment_id": None,
                }
            )
    return {
        "closed_loop": bool(links),
        "payment_links": links,
    }


def _summarize_trace(
    classification: dict[str, Any] | None,
    decisions: list[dict[str, Any]],
    executions: list[dict[str, Any]],
) -> dict[str, Any]:
    """Derive the concise final-decision summary from persisted facts.

    ``execution_state`` is an honest, persisted-evidence-only label so the
    frontend never overgeneralizes "no execution". It is NOT inferred: it
    reflects exactly which records exist.
    """
    summary: dict[str, Any] = {}
    if not classification:
        summary["final_decision"] = "not_classified"
        summary["execution_state"] = "NOT_CLASSIFIED"
        summary["selected_intervention"] = None
        summary["execution_mode"] = None
        summary["execution_status"] = None
        return summary

    if executions:
        last = executions[-1]
        summary["final_decision"] = "ALLOW"
        summary["execution_state"] = "EXECUTED"
        summary["selected_intervention"] = last["intervention"]
        summary["execution_mode"] = last["execution_mode"]
        summary["execution_status"] = last["status"]
        return summary

    denied = [d for d in decisions if not d["allowed"]]
    if denied:
        summary["final_decision"] = "DENY"
        summary["execution_state"] = "POLICY_BLOCKED"
        summary["selected_intervention"] = None
        summary["denial_reasons"] = [d["denial_reason"] for d in denied]
        summary["execution_mode"] = None
        summary["execution_status"] = None
        return summary

    # Classified but no execution and no denial: we cannot prove WHY there was
    # no execution (possibly no actionable intervention was available, or the
    # event was never run through the executor). Report a generic, honest state
    # rather than asserting "policy denied".
    summary["final_decision"] = "no_action"
    summary["execution_state"] = "NO_EXECUTION_RECORDED"
    summary["selected_intervention"] = None
    summary["execution_mode"] = None
    summary["execution_status"] = None
    return summary


def build_blocked_decisions(conn, *, limit: int = 100) -> dict[str, Any]:
    """Assemble the Policy & Blocked Actions payload."""
    rows = db.get_blocked_policy_decisions(conn, limit=limit)
    event_ids = [row["event_id"] for row in rows]
    attempt_summary = db.get_intervention_attempt_summary(conn, event_ids)
    blocked = []
    for row in rows:
        category = block_category(row["denial_reason"])
        evidence = attempt_summary.get(row["event_id"], {})
        blocked.append(
            {
                "event_id": row["event_id"],
                "customer_id": row["customer_id"],
                "amount_paise": row["amount_paise"],
                "currency": row["currency"],
                "risk_flag": row["risk_flag"],
                "proposed_intervention": row["proposed_intervention"],
                "denial_reason": row["denial_reason"],
                "rule_label": rule_label(row["denial_reason"]),
                "category": category,
                "category_label": block_category_label(category),
                "policy_rules_applied": row["policy_rules_applied"],
                "evaluated_at": row["evaluated_at"],
                # Evidence for the "why wasn't this recovered?" detail, from
                # persisted intervention_attempts only. Always present (even
                # when zero attempts) so the UI never needs a fallback value.
                "evidence": {
                    "previous_attempts": evidence.get("previous_attempts", 0),
                    "last_intervention": evidence.get("last_intervention"),
                    "last_attempt_status": evidence.get("last_attempt_status"),
                    "last_attempted_at": evidence.get("last_attempted_at"),
                },
            }
        )
    category_counts: dict[str, int] = {}
    for item in blocked:
        category_counts[item["category"]] = category_counts.get(item["category"], 0) + 1
    categories = [
        {
            "key": key,
            "label": BLOCK_CATEGORY_LABELS[key],
            "count": category_counts.get(key, 0),
        }
        for key in BLOCK_CATEGORY_ORDER
    ]
    return {"count": len(blocked), "blocked": blocked, "categories": categories}


def _now_iso() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat()

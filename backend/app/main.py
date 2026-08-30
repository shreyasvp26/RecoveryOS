"""RecoveryOS backend application entrypoint.

Phase 1 foundation: a single deterministic health endpoint. Phase 4: a
minimal payment event ingestion endpoint wired through the ingestion service.
Phase 10: read-only dashboard endpoints (Command Center, Event Decision Trace,
Policy & Blocked Actions) that reflect persisted state. Phase 19: the Policy
Lab endpoints, which replay policy scenarios in simulation and persist nothing.
Phase 20: the Revenue Health endpoints, which derive revenue-degradation
incidents from the persisted workload and persist nothing. Phase 21: the
Recovery Operations endpoints, which project the persisted decision records
into an operational queue and expose the operator execution entry point.
Phase 22: the read-only Recovery Intelligence endpoint, which measures the
persisted predictions against verified outcomes and changes nothing.
No RecoveryOS business logic beyond event ingestion; the dashboard routes hold
no decision logic and only READ persisted state.
"""

import sqlite3

from fastapi import FastAPI

from .config import (
    build_policy_config,
    get_omniroute_api_key,
    get_razorpay_key_id,
    get_razorpay_key_secret,
    get_razorpay_webhook_secret,
)
from .db import connect_database, init_db
from .routes.dashboard import router as dashboard_router
from .routes.estimation import router as estimation_router
from .routes.events import router as events_router
from .routes.incidents import router as incidents_router
from .routes.intelligence import router as intelligence_router
from .routes.recovery import router as recovery_router
from .routes.replay import router as replay_router
from .routes.webhook import router as webhook_router

app = FastAPI(title="RecoveryOS API", version="0.1.0")
app.include_router(events_router)
app.include_router(dashboard_router)
app.include_router(webhook_router)
app.include_router(replay_router)
app.include_router(incidents_router)
app.include_router(recovery_router)
app.include_router(intelligence_router)
app.include_router(estimation_router)

HEALTH_RESPONSE = {"status": "ok"}


@app.get("/health")
def health() -> dict[str, str]:
    """Return the deterministic liveness response for the foundation smoke test."""
    return HEALTH_RESPONSE


@app.get("/health/ready")
def health_ready() -> dict:
    """Report backend + database readiness and configuration state.

    This is a lightweight operator readiness check, not monitoring
    infrastructure. It reports:
      * database_usable — whether the configured SQLite database connects and
        initializes (a usable database makes the read/decision endpoints work);
      * status — ``ready`` when the database is usable, ``degraded`` when it is
        not (the API stays alive but cannot serve persisted state);
      * configured — whether each optional external integration is configured,
        as a boolean only. Credential/secret VALUES are never returned, and no
        API key, secret, or token is ever exposed here.
    """
    database_usable = False
    database_error: str | None = None
    try:
        conn = connect_database()
        try:
            init_db(conn)
            database_usable = True
        finally:
            conn.close()
    except (sqlite3.Error, ValueError) as exc:
        database_error = str(exc) or exc.__class__.__name__

    razorpay_configured = bool(get_razorpay_key_id() and get_razorpay_key_secret())
    webhook_configured = bool(get_razorpay_webhook_secret())
    omniroute_configured = bool(get_omniroute_api_key())

    return {
        "status": "ready" if database_usable else "degraded",
        "database_usable": database_usable,
        "database_error": database_error,
        "configuration": {
            "razorpay_test_mode": {
                "configured": razorpay_configured,
                "note": (
                    "Test Mode credentials present — REAL_RAZORPAY payment_link "
                    "execution is available."
                    if razorpay_configured
                    else (
                        "absent — payment_link execution reports an explicit "
                        "configuration_missing failure. Test Mode only; live keys "
                        "are structurally rejected."
                    )
                ),
            },
            "razorpay_webhook": {
                "configured": webhook_configured,
                "note": (
                    "webhook secret present — incoming payment_link.paid "
                    "deliveries are signature-verified."
                    if webhook_configured
                    else (
                        "absent — incoming webhooks fail verification (fail-closed)."
                    )
                ),
            },
            "omniroute": {
                "configured": omniroute_configured,
                "note": (
                    "API key present — the advisory AI classifier is available."
                    if omniroute_configured
                    else (
                        "absent — classification fails explicitly; the advisory "
                        "classifier will not fabricate output."
                    )
                ),
            },
            "policy": {
                "rules": {
                    "max_interventions_per_customer_24h": (
                        build_policy_config().max_interventions_per_customer_24h
                    ),
                    "event_cooldown_minutes": (
                        build_policy_config().event_cooldown_minutes
                    ),
                    "daily_spend_cap_paise": (
                        build_policy_config().daily_spend_cap_paise
                    ),
                },
                "note": "deterministic policy configuration; values are non-secret.",
            },
        },
    }

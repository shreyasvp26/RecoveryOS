"""RecoveryOS backend application entrypoint.

Phase 1 foundation: a single deterministic health endpoint. Phase 4: a
minimal payment event ingestion endpoint wired through the ingestion service.
Phase 10: read-only dashboard endpoints (Command Center, Event Decision Trace,
Policy & Blocked Actions) that reflect persisted state. Phase 19: the Policy
Lab endpoints, which replay policy scenarios in simulation and persist nothing.
Phase 20: the Revenue Health endpoints, which derive revenue-degradation
incidents from the persisted workload and persist nothing.
No RecoveryOS business logic beyond event ingestion; the dashboard routes hold
no decision logic and only READ persisted state.
"""

from fastapi import FastAPI

from .routes.dashboard import router as dashboard_router
from .routes.events import router as events_router
from .routes.incidents import router as incidents_router
from .routes.replay import router as replay_router
from .routes.webhook import router as webhook_router

app = FastAPI(title="RecoveryOS API", version="0.1.0")
app.include_router(events_router)
app.include_router(dashboard_router)
app.include_router(webhook_router)
app.include_router(replay_router)
app.include_router(incidents_router)

HEALTH_RESPONSE = {"status": "ok"}


@app.get("/health")
def health() -> dict[str, str]:
    """Return the deterministic health response for the foundation smoke test."""
    return HEALTH_RESPONSE

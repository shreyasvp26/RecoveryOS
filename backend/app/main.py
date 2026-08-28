"""RecoveryOS backend application entrypoint.

Phase 1 foundation: a single deterministic health endpoint. Phase 4: a
minimal payment event ingestion endpoint wired through the ingestion service.
Phase 10: read-only dashboard endpoints (Command Center, Event Decision Trace,
Policy & Blocked Actions) that reflect persisted state.
No RecoveryOS business logic beyond event ingestion; the dashboard routes hold
no decision logic and only READ persisted state.
"""

from fastapi import FastAPI

from .routes.dashboard import router as dashboard_router
from .routes.events import router as events_router
from .routes.webhook import router as webhook_router

app = FastAPI(title="RecoveryOS API", version="0.1.0")
app.include_router(events_router)
app.include_router(dashboard_router)
app.include_router(webhook_router)

HEALTH_RESPONSE = {"status": "ok"}


@app.get("/health")
def health() -> dict[str, str]:
    """Return the deterministic health response for the foundation smoke test."""
    return HEALTH_RESPONSE

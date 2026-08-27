"""RecoveryOS backend application entrypoint.

Phase 1 foundation: a single deterministic health endpoint. Phase 4: a
minimal payment event ingestion endpoint wired through the ingestion service.
No RecoveryOS business logic beyond event ingestion.
"""

from fastapi import FastAPI

from .routes.events import router as events_router

app = FastAPI(title="RecoveryOS API", version="0.1.0")
app.include_router(events_router)

HEALTH_RESPONSE = {"status": "ok"}


@app.get("/health")
def health() -> dict[str, str]:
    """Return the deterministic health response for the foundation smoke test."""
    return HEALTH_RESPONSE

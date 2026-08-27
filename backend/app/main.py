"""RecoveryOS backend application entrypoint.

Phase 1 foundation only: exposes a single deterministic health endpoint
as a smoke test for the application foundation. No RecoveryOS business logic.
"""

from fastapi import FastAPI

app = FastAPI(title="RecoveryOS API", version="0.1.0")

HEALTH_RESPONSE = {"status": "ok"}


@app.get("/health")
def health() -> dict[str, str]:
    """Return the deterministic health response for the foundation smoke test."""
    return HEALTH_RESPONSE

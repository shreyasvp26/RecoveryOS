"""RecoveryOS application configuration.

Phase 3: resolves the SQLite database path from the DATABASE_URL environment
variable, defaulting to a local development database. No business logic.

The database URL follows the SQLAlchemy-style scheme used in .env.example,
e.g.  DATABASE_URL=sqlite:///./recoveryos.db
Only the sqlite scheme is supported; SQLite remains the sole database.
"""

from __future__ import annotations

import os

DEFAULT_DATABASE_URL = "sqlite:///./recoveryos.db"
_DATABASE_URL_PREFIX = "sqlite:///"

DEFAULT_OMNIROUTE_BASE_URL = "https://api.omniroute.ai/v1"
DEFAULT_OMNIROUTE_MODEL = "omniroute-v1"


def get_database_url() -> str:
    """Return the configured database URL, or the development default."""
    return os.environ.get("DATABASE_URL", DEFAULT_DATABASE_URL)


def get_database_path() -> str:
    """Resolve the SQLite file path from the configured database URL."""
    url = get_database_url()
    if not url.startswith(_DATABASE_URL_PREFIX):
        raise ValueError(
            f"Unsupported database URL {url!r}; expected sqlite:///<path>"
        )
    return url[len(_DATABASE_URL_PREFIX):]


def get_omniroute_api_key() -> str:
    """Return the configured OmniRoute API key, or an empty string when unset."""
    return os.environ.get("OMNIROUTE_API_KEY", "")


def get_omniroute_model() -> str:
    """Return the configured OmniRoute model identifier."""
    return os.environ.get("OMNIROUTE_MODEL", DEFAULT_OMNIROUTE_MODEL)


def get_omniroute_base_url() -> str:
    """Return the configured OmniRoute base URL."""
    return os.environ.get("OMNIROUTE_BASE_URL", DEFAULT_OMNIROUTE_BASE_URL)

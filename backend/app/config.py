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

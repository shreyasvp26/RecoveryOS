"""Shared pytest fixtures for isolated, temporary SQLite test state.

Also establishes the operator-auth test environment: a known API key is set in
the process environment (module scope, before any test module imports, and read
at request time by ``require_operator``) and exported as a header constant, so
every ``TestClient(app)`` in the suite authenticates by default.
"""

from __future__ import annotations

import os
import sqlite3

import pytest

from app.db import connect, init_db

# A single shared test credential. Tests exercise REAL fail-closed behavior via
# explicitly unauthenticated clients in test_operator_auth.py; the rest of the
# suite authenticates by default so its assertions stay about business logic,
# not about the new gate.
TEST_OPERATOR_API_KEY = "test-operator-api-key"
TEST_OPERATOR_HEADERS = {"Authorization": f"Bearer {TEST_OPERATOR_API_KEY}"}

os.environ.setdefault("RECOVERYOS_OPERATOR_API_KEY", TEST_OPERATOR_API_KEY)


@pytest.fixture
def db_conn(tmp_path) -> sqlite3.Connection:
    """Provide a fresh, isolated SQLite connection backed by a temp file."""
    db_path = tmp_path / "test_recoveryos.db"
    conn = connect(str(db_path))
    init_db(conn)
    yield conn
    conn.close()

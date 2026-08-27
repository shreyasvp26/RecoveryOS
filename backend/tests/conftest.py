"""Shared pytest fixtures for isolated, temporary SQLite test state."""

from __future__ import annotations

import sqlite3

import pytest

from app.db import connect, init_db


@pytest.fixture
def db_conn(tmp_path) -> sqlite3.Connection:
    """Provide a fresh, isolated SQLite connection backed by a temp file."""
    db_path = tmp_path / "test_recoveryos.db"
    conn = connect(str(db_path))
    init_db(conn)
    yield conn
    conn.close()

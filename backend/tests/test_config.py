"""Phase 3 tests for configurable SQLite database path resolution."""

from __future__ import annotations

import pytest

from app.config import DEFAULT_DATABASE_URL, get_database_path, get_database_url
from app.db import connect_database, init_db


def test_default_database_url_is_used_when_unset(monkeypatch) -> None:
    monkeypatch.delenv("DATABASE_URL", raising=False)
    assert get_database_url() == DEFAULT_DATABASE_URL


def test_database_path_can_be_controlled_without_editing_source(monkeypatch) -> None:
    monkeypatch.setenv("DATABASE_URL", "sqlite:///./custom_dev.db")
    assert get_database_path() == "./custom_dev.db"


def test_unsupported_database_url_is_rejected(monkeypatch) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgres://localhost/x")
    with pytest.raises(ValueError):
        get_database_path()


def test_connect_database_uses_configured_path(monkeypatch, tmp_path) -> None:
    db_path = tmp_path / "configured.db"
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{db_path}")
    conn = connect_database()
    try:
        init_db(conn)
        tables = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='payment_events'"
        ).fetchall()
        assert [row[0] for row in tables] == ["payment_events"]
    finally:
        conn.close()
    assert db_path.exists()

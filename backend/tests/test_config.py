"""Phase 3 tests for configurable SQLite database path resolution."""

from __future__ import annotations

import pytest

from app.config import (
    DEFAULT_DATABASE_URL,
    DEFAULT_OMNIROUTE_BASE_URL,
    DEFAULT_OMNIROUTE_MODEL,
    get_database_path,
    get_database_url,
    get_omniroute_api_key,
    get_omniroute_base_url,
    get_omniroute_model,
)
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


def test_omniroute_model_and_base_url_defaults(monkeypatch) -> None:
    monkeypatch.delenv("OMNIROUTE_MODEL", raising=False)
    monkeypatch.delenv("OMNIROUTE_BASE_URL", raising=False)
    assert get_omniroute_model() == DEFAULT_OMNIROUTE_MODEL
    assert get_omniroute_base_url() == DEFAULT_OMNIROUTE_BASE_URL


def test_omniroute_env_overrides(monkeypatch) -> None:
    monkeypatch.setenv("OMNIROUTE_MODEL", "my-model")
    monkeypatch.setenv("OMNIROUTE_BASE_URL", "https://omniroute.example/v1")
    monkeypatch.setenv("OMNIROUTE_API_KEY", "sk-test")
    assert get_omniroute_model() == "my-model"
    assert get_omniroute_base_url() == "https://omniroute.example/v1"
    assert get_omniroute_api_key() == "sk-test"


def test_omniroute_api_key_defaults_to_empty(monkeypatch) -> None:
    monkeypatch.delenv("OMNIROUTE_API_KEY", raising=False)
    assert get_omniroute_api_key() == ""

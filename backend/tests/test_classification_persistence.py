"""Phase 5 tests for classification result persistence."""

from __future__ import annotations

import sqlite3

import pytest

from app.classification import ClassificationResult
from app.db import get_classification_result, insert_classification_result


def valid_result(**overrides) -> dict:
    base = {
        "event_id": "evt_000001",
        "root_cause_category": "transient",
        "confidence": 0.91,
        "reasoning": "Payments from this bank frequently recover on retry.",
        "candidate_interventions": ["retry_delayed", "payment_link"],
    }
    base.update(overrides)
    return base


def test_database_initialization_creates_classification_table(db_conn) -> None:
    rows = db_conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='classification_results'"
    ).fetchall()
    assert [row[0] for row in rows] == ["classification_results"]


def test_classification_persists_and_is_retrievable(db_conn) -> None:
    result = ClassificationResult.from_dict(valid_result())
    insert_classification_result(db_conn, result)
    retrieved = get_classification_result(db_conn, "evt_000001")
    assert retrieved is not None
    assert retrieved == result


def test_retrieved_classification_matches_input_dict(db_conn) -> None:
    data = valid_result()
    insert_classification_result(db_conn, ClassificationResult.from_dict(data))
    assert get_classification_result(db_conn, data["event_id"]).to_dict() == data


def test_get_missing_classification_returns_none(db_conn) -> None:
    assert get_classification_result(db_conn, "does_not_exist") is None


def test_duplicate_classification_event_id_is_rejected(db_conn) -> None:
    result = ClassificationResult.from_dict(valid_result())
    insert_classification_result(db_conn, result)
    with pytest.raises(sqlite3.IntegrityError):
        insert_classification_result(db_conn, result)


def test_multiple_classifications_persist_independently(db_conn) -> None:
    first = ClassificationResult.from_dict(valid_result())
    second = ClassificationResult.from_dict(
        valid_result(
            event_id="evt_000002",
            root_cause_category="terminal",
            candidate_interventions=["no_action"],
        )
    )
    insert_classification_result(db_conn, first)
    insert_classification_result(db_conn, second)
    assert get_classification_result(db_conn, "evt_000001") == first
    assert get_classification_result(db_conn, "evt_000002") == second

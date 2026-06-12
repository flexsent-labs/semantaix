"""Unit tests for UsageHitlEventRepository.record() — Story 14.04."""
from __future__ import annotations

import sqlite3

import pytest

from services.api.app.usage.migrations import bootstrap_usage_db
from services.api.app.usage.repositories import (
    HITL_EVENT_TYPES,
    UsageHitlEventRepository,
    UsageHitlEventRow,
)


@pytest.fixture
def db_path(tmp_path) -> str:
    path = str(tmp_path / "usage.sqlite3")
    bootstrap_usage_db(path)
    return path


@pytest.fixture
def repo(db_path) -> UsageHitlEventRepository:
    return UsageHitlEventRepository(db_path=db_path)


def _fetch_all(db_path: str) -> list[dict]:
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute("SELECT * FROM usage_hitl_events ORDER BY id").fetchall()
        return [dict(r) for r in rows]


def _make_row(**kwargs) -> UsageHitlEventRow:
    defaults = {
        "id": 0,
        "project_id": 3,
        "event_type": "created",
        "ticket_id": 42,
        "trace_id": "trace-xyz",
        "created_at": "2026-06-12T10:00:00Z",
    }
    return UsageHitlEventRow(**{**defaults, **kwargs})


class TestUsageHitlEventRepositoryRecord:
    def test_insert_created_event(self, repo, db_path):
        row = _make_row(event_type="created")
        repo.record(row)
        rows = _fetch_all(db_path)
        assert len(rows) == 1
        r = rows[0]
        assert r["project_id"] == 3
        assert r["event_type"] == "created"
        assert r["ticket_id"] == 42
        assert r["trace_id"] == "trace-xyz"
        assert r["created_at"] == "2026-06-12T10:00:00Z"

    def test_all_four_event_types_insert_successfully(self, repo, db_path):
        for event_type in ("created", "assigned", "replied", "resolved"):
            repo.record(_make_row(event_type=event_type))
        rows = _fetch_all(db_path)
        assert len(rows) == 4
        assert [r["event_type"] for r in rows] == ["created", "assigned", "replied", "resolved"]

    def test_trace_id_nullable(self, repo, db_path):
        repo.record(_make_row(trace_id=None))
        rows = _fetch_all(db_path)
        assert rows[0]["trace_id"] is None

    def test_multiple_rows_auto_increment_id(self, repo, db_path):
        repo.record(_make_row(event_type="created"))
        repo.record(_make_row(event_type="assigned"))
        rows = _fetch_all(db_path)
        assert len(rows) == 2
        assert rows[0]["id"] == 1
        assert rows[1]["id"] == 2

    def test_invalid_event_type_raises_before_db(self, repo, db_path):
        row = _make_row(event_type="archived")
        with pytest.raises(ValueError, match="event_type"):
            repo.record(row)
        assert _fetch_all(db_path) == []

    def test_invalid_event_type_does_not_touch_db(self, repo, db_path):
        """Validation fires BEFORE the DB write — no partial writes."""
        row = _make_row(event_type="unknown")
        try:
            repo.record(row)
        except ValueError:
            pass
        assert _fetch_all(db_path) == []


class TestHitlEventTypesConstant:
    def test_frozenset_contains_all_four(self):
        assert HITL_EVENT_TYPES == frozenset({"created", "assigned", "replied", "resolved"})

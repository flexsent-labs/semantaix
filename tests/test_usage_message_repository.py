"""Unit tests for UsageMessageRepository.record() — Story 14.03."""
from __future__ import annotations

import sqlite3

import pytest

from services.api.app.usage.migrations import bootstrap_usage_db
from services.api.app.usage.repositories import (
    DIRECTIONS,
    PARTICIPANT_ROLES,
    UsageMessageRepository,
    UsageMessageRow,
)


@pytest.fixture
def db_path(tmp_path) -> str:
    path = str(tmp_path / "usage.sqlite3")
    bootstrap_usage_db(path)
    return path


@pytest.fixture
def repo(db_path) -> UsageMessageRepository:
    return UsageMessageRepository(db_path=db_path)


def _fetch_all(db_path: str) -> list[dict]:
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute("SELECT * FROM usage_messages ORDER BY id").fetchall()
        return [dict(r) for r in rows]


def _make_row(**kwargs) -> UsageMessageRow:
    defaults = {
        "id": 0,
        "project_id": 7,
        "direction": "in",
        "participant_role": "customer",
        "trace_id": "trace-abc",
        "created_at": "2026-06-12T10:00:00Z",
    }
    return UsageMessageRow(**{**defaults, **kwargs})


class TestUsageMessageRepositoryRecord:
    def test_insert_inbound_customer_row(self, repo, db_path):
        row = _make_row(direction="in", participant_role="customer")
        repo.record(row)
        rows = _fetch_all(db_path)
        assert len(rows) == 1
        r = rows[0]
        assert r["project_id"] == 7
        assert r["direction"] == "in"
        assert r["participant_role"] == "customer"
        assert r["trace_id"] == "trace-abc"
        assert r["created_at"] == "2026-06-12T10:00:00Z"

    def test_insert_outbound_customer_row(self, repo, db_path):
        row = _make_row(direction="out", participant_role="customer", trace_id=None)
        repo.record(row)
        rows = _fetch_all(db_path)
        assert len(rows) == 1
        assert rows[0]["direction"] == "out"
        assert rows[0]["trace_id"] is None

    def test_insert_inbound_operator_row(self, repo, db_path):
        row = _make_row(direction="in", participant_role="operator", trace_id=None)
        repo.record(row)
        rows = _fetch_all(db_path)
        assert rows[0]["participant_role"] == "operator"

    def test_multiple_rows_auto_increment_id(self, repo, db_path):
        repo.record(_make_row(direction="in", participant_role="customer"))
        repo.record(_make_row(direction="out", participant_role="customer"))
        rows = _fetch_all(db_path)
        assert len(rows) == 2
        assert rows[0]["id"] == 1
        assert rows[1]["id"] == 2

    def test_invalid_direction_raises_before_db(self, repo, db_path):
        row = _make_row(direction="sideways")
        with pytest.raises(ValueError, match="direction"):
            repo.record(row)
        assert _fetch_all(db_path) == []

    def test_invalid_participant_role_raises_before_db(self, repo, db_path):
        row = _make_row(participant_role="robot")
        with pytest.raises(ValueError, match="participant_role"):
            repo.record(row)
        assert _fetch_all(db_path) == []


class TestConstants:
    def test_directions_frozenset(self):
        assert DIRECTIONS == frozenset({"in", "out"})

    def test_participant_roles_frozenset(self):
        assert PARTICIPANT_ROLES == frozenset({"customer", "operator"})

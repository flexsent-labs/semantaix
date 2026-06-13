"""Tests for UsageIncidentRepository.list_for_window (Story 14.07)."""
from __future__ import annotations

import sqlite3

from services.api.app.usage.migrations import bootstrap_usage_db
from services.api.app.usage.repositories import (
    UsageIncidentRepository,
    UsageIncidentRow,
)


def _db(tmp_path) -> str:
    db = str(tmp_path / "usage.db")
    bootstrap_usage_db(db)
    return db


def _insert_incident(
    db: str,
    *,
    project_id: int,
    started_at: str,
    ended_at: str | None = None,
    breached_trackers: str = '["llm"]',
    peak_pct: float | None = None,
    total_excess_cost_usd: float | None = None,
) -> int:
    with sqlite3.connect(db) as conn:
        cur = conn.execute(
            """
            INSERT INTO usage_incidents
                (project_id, started_at, ended_at, breached_trackers,
                 peak_pct, total_excess_cost_usd)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (project_id, started_at, ended_at, breached_trackers,
             peak_pct, total_excess_cost_usd),
        )
        return cur.lastrowid  # type: ignore[return-value]


def test_list_for_window_empty(tmp_path):
    db = _db(tmp_path)
    repo = UsageIncidentRepository(db_path=db)
    assert repo.list_for_window(
        project_id=1,
        from_ts="2026-06-01T00:00:00Z",
        to_ts="2026-06-11T00:00:00Z",
    ) == []


def test_list_for_window_returns_incident(tmp_path):
    db = _db(tmp_path)
    _insert_incident(db, project_id=1, started_at="2026-06-05T10:00:00Z",
                     ended_at="2026-06-05T12:00:00Z", peak_pct=120.0,
                     total_excess_cost_usd=3.50)
    repo = UsageIncidentRepository(db_path=db)
    rows = repo.list_for_window(
        project_id=1,
        from_ts="2026-06-01T00:00:00Z",
        to_ts="2026-06-11T00:00:00Z",
    )
    assert len(rows) == 1
    assert rows[0].project_id == 1
    assert rows[0].started_at == "2026-06-05T10:00:00Z"
    assert rows[0].ended_at == "2026-06-05T12:00:00Z"
    assert rows[0].peak_pct == 120.0
    assert rows[0].total_excess_cost_usd == 3.50


def test_list_for_window_active_incident(tmp_path):
    db = _db(tmp_path)
    _insert_incident(db, project_id=1, started_at="2026-06-10T08:00:00Z")
    repo = UsageIncidentRepository(db_path=db)
    rows = repo.list_for_window(
        project_id=1,
        from_ts="2026-06-01T00:00:00Z",
        to_ts="2026-06-11T00:00:00Z",
    )
    assert len(rows) == 1
    assert rows[0].ended_at is None


def test_list_for_window_project_isolation(tmp_path):
    db = _db(tmp_path)
    _insert_incident(db, project_id=1, started_at="2026-06-05T00:00:00Z")
    _insert_incident(db, project_id=2, started_at="2026-06-05T00:00:00Z")
    repo = UsageIncidentRepository(db_path=db)
    rows = repo.list_for_window(
        project_id=1,
        from_ts="2026-06-01T00:00:00Z",
        to_ts="2026-06-11T00:00:00Z",
    )
    assert len(rows) == 1
    assert rows[0].project_id == 1


def test_list_for_window_boundary_inclusive(tmp_path):
    db = _db(tmp_path)
    _insert_incident(db, project_id=1, started_at="2026-06-01T00:00:00Z")
    _insert_incident(db, project_id=1, started_at="2026-06-11T00:00:00Z")
    _insert_incident(db, project_id=1, started_at="2026-06-12T00:00:00Z")
    repo = UsageIncidentRepository(db_path=db)
    rows = repo.list_for_window(
        project_id=1,
        from_ts="2026-06-01T00:00:00Z",
        to_ts="2026-06-11T00:00:00Z",
    )
    starts = {r.started_at for r in rows}
    assert "2026-06-01T00:00:00Z" in starts
    assert "2026-06-11T00:00:00Z" in starts
    assert "2026-06-12T00:00:00Z" not in starts


def test_list_for_window_ordered_by_started_at(tmp_path):
    db = _db(tmp_path)
    _insert_incident(db, project_id=1, started_at="2026-06-10T00:00:00Z")
    _insert_incident(db, project_id=1, started_at="2026-06-05T00:00:00Z")
    _insert_incident(db, project_id=1, started_at="2026-06-08T00:00:00Z")
    repo = UsageIncidentRepository(db_path=db)
    rows = repo.list_for_window(
        project_id=1,
        from_ts="2026-06-01T00:00:00Z",
        to_ts="2026-06-11T00:00:00Z",
    )
    assert [r.started_at for r in rows] == [
        "2026-06-05T00:00:00Z",
        "2026-06-08T00:00:00Z",
        "2026-06-10T00:00:00Z",
    ]


def test_list_for_window_returns_correct_type(tmp_path):
    db = _db(tmp_path)
    _insert_incident(db, project_id=1, started_at="2026-06-05T00:00:00Z")
    repo = UsageIncidentRepository(db_path=db)
    rows = repo.list_for_window(
        project_id=1,
        from_ts="2026-06-01T00:00:00Z",
        to_ts="2026-06-11T00:00:00Z",
    )
    assert all(isinstance(r, UsageIncidentRow) for r in rows)

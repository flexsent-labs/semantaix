"""Tests for UsageDailySummaryRepository.query_wasted (Story 14.07)."""
from __future__ import annotations

import sqlite3

from services.api.app.usage.migrations import bootstrap_usage_db
from services.api.app.usage.repositories import (
    UsageDailySummaryRepository,
    UsageDailySummaryRow,
)


def _db(tmp_path) -> str:
    db = str(tmp_path / "usage.db")
    bootstrap_usage_db(db)
    return db


def _insert_summary(db: str, *, project_id: int, day_utc: str,
                     tracker_type: str, model_name: str = "",
                     cost_usd_total: float | None = None,
                     wasted_cost_usd: float | None = None,
                     call_count: int = 0) -> None:
    with sqlite3.connect(db) as conn:
        conn.execute(
            """
            INSERT INTO usage_daily_summary
                (project_id, day_utc, tracker_type, model_name,
                 prompt_tokens_total, completion_tokens_total,
                 cost_usd_total, wasted_cost_usd, call_count,
                 in_count, out_count,
                 hitl_created_count, hitl_assigned_count,
                 hitl_replied_count, hitl_resolved_count)
            VALUES (?, ?, ?, ?, 0, 0, ?, ?, ?, 0, 0, 0, 0, 0, 0)
            """,
            (project_id, day_utc, tracker_type, model_name,
             cost_usd_total, wasted_cost_usd, call_count),
        )


def test_query_wasted_empty(tmp_path):
    db = _db(tmp_path)
    repo = UsageDailySummaryRepository(db_path=db)
    assert repo.query_wasted(
        project_id=1, from_day_utc="2026-06-01", to_day_utc="2026-06-11"
    ) == []


def test_query_wasted_returns_llm_rows(tmp_path):
    db = _db(tmp_path)
    _insert_summary(db, project_id=1, day_utc="2026-06-10",
                    tracker_type="llm", model_name="gpt-4o",
                    cost_usd_total=5.0, wasted_cost_usd=1.5, call_count=10)
    repo = UsageDailySummaryRepository(db_path=db)
    rows = repo.query_wasted(
        project_id=1, from_day_utc="2026-06-01", to_day_utc="2026-06-11"
    )
    assert len(rows) == 1
    assert rows[0].tracker_type == "llm"
    assert rows[0].model_name == "gpt-4o"
    assert rows[0].cost_usd_total == 5.0
    assert rows[0].wasted_cost_usd == 1.5


def test_query_wasted_excludes_non_llm_rows(tmp_path):
    db = _db(tmp_path)
    _insert_summary(db, project_id=1, day_utc="2026-06-10",
                    tracker_type="messages", model_name="")
    _insert_summary(db, project_id=1, day_utc="2026-06-10",
                    tracker_type="hitl", model_name="")
    _insert_summary(db, project_id=1, day_utc="2026-06-10",
                    tracker_type="llm", model_name="gpt-4o")
    repo = UsageDailySummaryRepository(db_path=db)
    rows = repo.query_wasted(
        project_id=1, from_day_utc="2026-06-01", to_day_utc="2026-06-11"
    )
    assert len(rows) == 1
    assert rows[0].tracker_type == "llm"


def test_query_wasted_project_isolation(tmp_path):
    db = _db(tmp_path)
    _insert_summary(db, project_id=1, day_utc="2026-06-10",
                    tracker_type="llm", model_name="gpt-4o")
    _insert_summary(db, project_id=2, day_utc="2026-06-10",
                    tracker_type="llm", model_name="gpt-4o")
    repo = UsageDailySummaryRepository(db_path=db)
    rows = repo.query_wasted(
        project_id=1, from_day_utc="2026-06-01", to_day_utc="2026-06-11"
    )
    assert all(r.project_id == 1 for r in rows)
    assert len(rows) == 1


def test_query_wasted_date_boundary(tmp_path):
    db = _db(tmp_path)
    _insert_summary(db, project_id=1, day_utc="2026-06-01",
                    tracker_type="llm", model_name="m1")
    _insert_summary(db, project_id=1, day_utc="2026-06-11",
                    tracker_type="llm", model_name="m2")
    _insert_summary(db, project_id=1, day_utc="2026-06-12",
                    tracker_type="llm", model_name="m3")
    repo = UsageDailySummaryRepository(db_path=db)
    rows = repo.query_wasted(
        project_id=1, from_day_utc="2026-06-01", to_day_utc="2026-06-11"
    )
    days = {r.day_utc for r in rows}
    assert "2026-06-01" in days
    assert "2026-06-11" in days
    assert "2026-06-12" not in days


def test_query_wasted_ordered_by_day_model(tmp_path):
    db = _db(tmp_path)
    _insert_summary(db, project_id=1, day_utc="2026-06-10",
                    tracker_type="llm", model_name="z-model")
    _insert_summary(db, project_id=1, day_utc="2026-06-10",
                    tracker_type="llm", model_name="a-model")
    _insert_summary(db, project_id=1, day_utc="2026-06-09",
                    tracker_type="llm", model_name="b-model")
    repo = UsageDailySummaryRepository(db_path=db)
    rows = repo.query_wasted(
        project_id=1, from_day_utc="2026-06-01", to_day_utc="2026-06-11"
    )
    assert rows[0].day_utc == "2026-06-09"
    assert rows[1].model_name == "a-model"
    assert rows[2].model_name == "z-model"


def test_query_wasted_returns_correct_type(tmp_path):
    db = _db(tmp_path)
    _insert_summary(db, project_id=1, day_utc="2026-06-10",
                    tracker_type="llm", model_name="gpt-4o")
    repo = UsageDailySummaryRepository(db_path=db)
    rows = repo.query_wasted(
        project_id=1, from_day_utc="2026-06-01", to_day_utc="2026-06-11"
    )
    assert all(isinstance(r, UsageDailySummaryRow) for r in rows)

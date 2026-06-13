"""Tests for list_for_day() on the three raw repos — Story 14.06.

These paginated row-listing methods feed the drill-down panel on the
usage dashboard.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

from services.api.app.usage.migrations import bootstrap_usage_db
from services.api.app.usage.repositories import (
    UsageHitlEventRepository,
    UsageLlmCallRepository,
    UsageMessageRepository,
)


def _db(tmp_path: Path) -> str:
    db = str(tmp_path / "usage.db")
    bootstrap_usage_db(db)
    return db


def _insert_llm(db: str, *, day: str, outcome: str = "customer_visible_answer") -> None:
    with sqlite3.connect(db) as conn:
        conn.execute(
            "INSERT INTO usage_llm_calls"
            " (project_id, model_name, prompt_tokens, completion_tokens,"
            "  cost_usd, call_outcome, trace_id, created_at)"
            " VALUES (1, 'haiku', 100, 50, 0.01, ?, NULL, ?)",
            (outcome, f"{day}T12:00:00Z"),
        )


def _insert_msg(db: str, *, day: str, direction: str = "in") -> None:
    with sqlite3.connect(db) as conn:
        conn.execute(
            "INSERT INTO usage_messages"
            " (project_id, direction, participant_role, trace_id, created_at)"
            " VALUES (1, ?, 'customer', NULL, ?)",
            (direction, f"{day}T12:00:00Z"),
        )


def _insert_hitl(db: str, *, day: str, event_type: str = "created") -> None:
    with sqlite3.connect(db) as conn:
        conn.execute(
            "INSERT INTO usage_hitl_events"
            " (project_id, event_type, ticket_id, trace_id, created_at)"
            " VALUES (1, ?, 1, NULL, ?)",
            (event_type, f"{day}T12:00:00Z"),
        )


# ---------------------------------------------------------------------------
# LLM list_for_day
# ---------------------------------------------------------------------------

def test_llm_list_for_day_returns_rows(tmp_path):
    db = _db(tmp_path)
    _insert_llm(db, day="2026-05-25")
    _insert_llm(db, day="2026-05-25", outcome="verifier_rejected")
    _insert_llm(db, day="2026-05-26")  # different day — excluded

    repo = UsageLlmCallRepository(db_path=db)
    rows = repo.list_for_day(project_id=1, day_utc="2026-05-25")
    assert len(rows) == 2
    assert all(r.project_id == 1 for r in rows)
    assert all("2026-05-25" in r.created_at for r in rows)


def test_llm_list_for_day_empty(tmp_path):
    db = _db(tmp_path)
    repo = UsageLlmCallRepository(db_path=db)
    assert repo.list_for_day(project_id=1, day_utc="2026-05-25") == []


def test_llm_list_for_day_pagination(tmp_path):
    db = _db(tmp_path)
    for _ in range(5):
        _insert_llm(db, day="2026-05-25")

    repo = UsageLlmCallRepository(db_path=db)
    page1 = repo.list_for_day(project_id=1, day_utc="2026-05-25", page=1, page_size=3)
    page2 = repo.list_for_day(project_id=1, day_utc="2026-05-25", page=2, page_size=3)
    assert len(page1) == 3
    assert len(page2) == 2
    ids1 = {r.id for r in page1}
    ids2 = {r.id for r in page2}
    assert ids1.isdisjoint(ids2)


def test_llm_list_for_day_project_isolation(tmp_path):
    db = _db(tmp_path)
    _insert_llm(db, day="2026-05-25")
    with sqlite3.connect(db) as conn:
        conn.execute(
            "INSERT INTO usage_llm_calls"
            " (project_id, model_name, prompt_tokens, completion_tokens,"
            "  cost_usd, call_outcome, trace_id, created_at)"
            " VALUES (2, 'haiku', 10, 5, 0.001,"
            " 'customer_visible_answer', NULL, '2026-05-25T12:00:00Z')"
        )
    repo = UsageLlmCallRepository(db_path=db)
    assert len(repo.list_for_day(project_id=1, day_utc="2026-05-25")) == 1
    assert len(repo.list_for_day(project_id=2, day_utc="2026-05-25")) == 1


# ---------------------------------------------------------------------------
# Messages list_for_day
# ---------------------------------------------------------------------------

def test_messages_list_for_day_returns_rows(tmp_path):
    db = _db(tmp_path)
    _insert_msg(db, day="2026-05-25", direction="in")
    _insert_msg(db, day="2026-05-25", direction="out")
    _insert_msg(db, day="2026-05-26")  # different day

    repo = UsageMessageRepository(db_path=db)
    rows = repo.list_for_day(project_id=1, day_utc="2026-05-25")
    assert len(rows) == 2
    directions = {r.direction for r in rows}
    assert directions == {"in", "out"}


def test_messages_list_for_day_pagination(tmp_path):
    db = _db(tmp_path)
    for _ in range(4):
        _insert_msg(db, day="2026-05-25")

    repo = UsageMessageRepository(db_path=db)
    page1 = repo.list_for_day(project_id=1, day_utc="2026-05-25", page=1, page_size=3)
    page2 = repo.list_for_day(project_id=1, day_utc="2026-05-25", page=2, page_size=3)
    assert len(page1) == 3
    assert len(page2) == 1


# ---------------------------------------------------------------------------
# HITL list_for_day
# ---------------------------------------------------------------------------

def test_hitl_list_for_day_returns_rows(tmp_path):
    db = _db(tmp_path)
    for et in ("created", "assigned", "replied", "resolved"):
        _insert_hitl(db, day="2026-05-25", event_type=et)
    _insert_hitl(db, day="2026-05-26")  # different day

    repo = UsageHitlEventRepository(db_path=db)
    rows = repo.list_for_day(project_id=1, day_utc="2026-05-25")
    assert len(rows) == 4
    event_types = {r.event_type for r in rows}
    assert event_types == {"created", "assigned", "replied", "resolved"}


def test_hitl_list_for_day_empty(tmp_path):
    db = _db(tmp_path)
    repo = UsageHitlEventRepository(db_path=db)
    assert repo.list_for_day(project_id=1, day_utc="2026-05-25") == []

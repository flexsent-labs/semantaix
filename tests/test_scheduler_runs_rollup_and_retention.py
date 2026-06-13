"""Integration: seed raw rows → rollup → assert summaries → retention → assert purge.

Story 14.05: end-to-end flow through run_rollup + run_retention using real
repos and real SQLite; no mocks.
"""
from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from pathlib import Path

import pytest

from services.api.app.usage.migrations import bootstrap_usage_db
from services.api.app.usage.repositories import (
    UsageDailySummaryRepository,
    UsageHitlEventRepository,
    UsageLlmCallRepository,
    UsageMessageRepository,
)
from services.scheduler.app.usage_retention import run_retention
from services.scheduler.app.usage_rollup import RollupRepos, run_rollup


def _make_repos(tmp_path: Path) -> tuple[str, RollupRepos]:
    db = str(tmp_path / "usage.db")
    bootstrap_usage_db(db)
    repos = RollupRepos(
        llm=UsageLlmCallRepository(db_path=db),
        messages=UsageMessageRepository(db_path=db),
        hitl=UsageHitlEventRepository(db_path=db),
        summary=UsageDailySummaryRepository(db_path=db),
        db_path=db,
    )
    return db, repos


def _insert_raw(db: str, *, day: str, days_ago_ts: str | None = None) -> None:
    ts = days_ago_ts or f"{day}T12:00:00Z"
    with sqlite3.connect(db) as conn:
        conn.execute(
            "INSERT INTO usage_llm_calls"
            " (project_id, model_name, prompt_tokens, completion_tokens,"
            "  cost_usd, call_outcome, trace_id, created_at)"
            " VALUES (1, 'm', 100, 50, 0.01, 'customer_visible_answer', NULL, ?)",
            (ts,),
        )
        conn.execute(
            "INSERT INTO usage_messages"
            " (project_id, direction, participant_role, trace_id, created_at)"
            " VALUES (1, 'in', 'customer', NULL, ?)",
            (ts,),
        )
        conn.execute(
            "INSERT INTO usage_hitl_events"
            " (project_id, event_type, ticket_id, trace_id, created_at)"
            " VALUES (1, 'created', 1, NULL, ?)",
            (ts,),
        )


def _count(db: str, table: str) -> int:
    with sqlite3.connect(db) as conn:
        return conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]


@pytest.mark.asyncio
async def test_rollup_then_retention_full_lifecycle(tmp_path):
    db, repos = _make_repos(tmp_path)

    # Seed: yesterday's data (will be rolled up, kept by retention)
    _insert_raw(db, day="2026-05-25")
    # Old data (35 days ago — will be purged by retention)
    _insert_raw(db, day="2026-04-21", days_ago_ts="2026-04-21T12:00:00Z")

    # --- rollup ---
    await run_rollup(clock=lambda: datetime(2026, 5, 26, 12, 0, 0, tzinfo=UTC), repos=repos)

    # Summaries created for yesterday (3 tracker types)
    rows = repos.summary.query(
        project_id=1, from_day_utc="2026-05-25", to_day_utc="2026-05-25"
    )
    assert len(rows) == 3  # llm + messages + hitl

    # Also for the old day (it's within 30 days window: 35 > 30, so NOT rolled up)
    old_rows = repos.summary.query(
        project_id=1, from_day_utc="2026-04-21", to_day_utc="2026-04-21"
    )
    assert old_rows == []  # beyond 30-day cap

    # --- retention ---
    await run_retention(
        clock=lambda: datetime(2026, 5, 26, 12, 0, 0, tzinfo=UTC),
        repos=repos, retention_days=30,
    )

    # Old raw rows purged (2026-04-21 is 35 days before 2026-05-26)
    with sqlite3.connect(db) as conn:
        old_llm = conn.execute(
            "SELECT COUNT(*) FROM usage_llm_calls WHERE created_at LIKE '2026-04-21%'"
        ).fetchone()[0]
    assert old_llm == 0

    # Recent raw rows kept (2026-05-25 is 1 day before 2026-05-26)
    with sqlite3.connect(db) as conn:
        recent_llm = conn.execute(
            "SELECT COUNT(*) FROM usage_llm_calls WHERE created_at LIKE '2026-05-25%'"
        ).fetchone()[0]
    assert recent_llm == 1

    # Summary rows still intact
    rows_after = repos.summary.query(
        project_id=1, from_day_utc="2026-05-25", to_day_utc="2026-05-25"
    )
    assert len(rows_after) == 3


@pytest.mark.asyncio
async def test_rollup_idempotent_across_runs(tmp_path):
    db, repos = _make_repos(tmp_path)
    _insert_raw(db, day="2026-05-25")
    await run_rollup(clock=lambda: datetime(2026, 5, 26, 12, 0, 0, tzinfo=UTC), repos=repos)
    await run_rollup(clock=lambda: datetime(2026, 5, 26, 12, 0, 0, tzinfo=UTC), repos=repos)

    rows = repos.summary.query(
        project_id=1, from_day_utc="2026-05-25", to_day_utc="2026-05-25"
    )
    # Idempotent: still exactly 3 rows (not 6)
    assert len(rows) == 3
    assert rows[0].call_count == 1  # not doubled

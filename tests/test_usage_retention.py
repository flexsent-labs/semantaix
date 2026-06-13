"""Tests for usage_retention.run_retention — Story 14.05."""
from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta
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
from services.scheduler.app.usage_rollup import RollupRepos


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


def _ts(days_ago: int, base: datetime | None = None) -> str:
    if base is None:
        base = datetime(2026, 5, 26, 12, 0, 0, tzinfo=UTC)
    return (base - timedelta(days=days_ago)).strftime("%Y-%m-%dT%H:%M:%SZ")


def _insert_llm(db: str, *, days_ago: int) -> None:
    with sqlite3.connect(db) as conn:
        conn.execute(
            "INSERT INTO usage_llm_calls"
            " (project_id, model_name, prompt_tokens, completion_tokens,"
            "  cost_usd, call_outcome, trace_id, created_at)"
            " VALUES (1, 'm', 10, 5, 0.001, 'customer_visible_answer', NULL, ?)",
            (_ts(days_ago),),
        )


def _insert_msg(db: str, *, days_ago: int) -> None:
    with sqlite3.connect(db) as conn:
        conn.execute(
            "INSERT INTO usage_messages"
            " (project_id, direction, participant_role, trace_id, created_at)"
            " VALUES (1, 'in', 'customer', NULL, ?)",
            (_ts(days_ago),),
        )


def _insert_hitl(db: str, *, days_ago: int) -> None:
    with sqlite3.connect(db) as conn:
        conn.execute(
            "INSERT INTO usage_hitl_events"
            " (project_id, event_type, ticket_id, trace_id, created_at)"
            " VALUES (1, 'created', 1, NULL, ?)",
            (_ts(days_ago),),
        )


def _count(db: str, table: str) -> int:
    with sqlite3.connect(db) as conn:
        return conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]


def _clock_at(days_ago: int = 0) -> datetime:
    base = datetime(2026, 5, 26, 12, 0, 0, tzinfo=UTC)
    return base - timedelta(days=days_ago)


# ---------------------------------------------------------------------------
# Purge old rows, keep recent
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_retention_purges_old_llm_rows(tmp_path):
    db, repos = _make_repos(tmp_path)
    _insert_llm(db, days_ago=35)   # old — should be purged
    _insert_llm(db, days_ago=5)    # recent — should stay

    await run_retention(clock=lambda: _clock_at(), repos=repos, retention_days=30)

    assert _count(db, "usage_llm_calls") == 1


@pytest.mark.asyncio
async def test_retention_purges_old_message_rows(tmp_path):
    db, repos = _make_repos(tmp_path)
    _insert_msg(db, days_ago=31)
    _insert_msg(db, days_ago=1)

    await run_retention(clock=lambda: _clock_at(), repos=repos, retention_days=30)

    assert _count(db, "usage_messages") == 1


@pytest.mark.asyncio
async def test_retention_purges_old_hitl_rows(tmp_path):
    db, repos = _make_repos(tmp_path)
    _insert_hitl(db, days_ago=60)
    _insert_hitl(db, days_ago=2)

    await run_retention(clock=lambda: _clock_at(), repos=repos, retention_days=30)

    assert _count(db, "usage_hitl_events") == 1


@pytest.mark.asyncio
async def test_retention_boundary_row_stays(tmp_path):
    """A row exactly at the cutoff boundary stays (cutoff is exclusive <)."""
    db, repos = _make_repos(tmp_path)
    # row created exactly 30 days ago (= cutoff boundary) — should NOT be purged
    _insert_llm(db, days_ago=30)

    await run_retention(clock=lambda: _clock_at(), repos=repos, retention_days=30)

    # The cutoff is clock() - 30 days; a row from exactly 30 days ago lands on
    # the boundary and the condition is created_at < cutoff, so it is NOT deleted.
    assert _count(db, "usage_llm_calls") == 1


@pytest.mark.asyncio
async def test_retention_does_not_touch_summary_table(tmp_path):
    db, repos = _make_repos(tmp_path)
    # Insert old raw row and a summary row
    _insert_llm(db, days_ago=60)
    with sqlite3.connect(db) as conn:
        conn.execute(
            "INSERT INTO usage_daily_summary"
            " (project_id, day_utc, tracker_type, model_name,"
            "  prompt_tokens_total, completion_tokens_total, cost_usd_total,"
            "  wasted_cost_usd, call_count)"
            " VALUES (1, '2026-03-01', 'llm', 'm', 10, 5, 0.001, 0.0, 1)"
        )

    await run_retention(clock=lambda: _clock_at(), repos=repos, retention_days=30)

    # raw rows purged
    assert _count(db, "usage_llm_calls") == 0
    # summary row untouched
    assert _count(db, "usage_daily_summary") == 1


@pytest.mark.asyncio
async def test_retention_no_data_completes_without_error(tmp_path):
    _, repos = _make_repos(tmp_path)
    await run_retention(clock=lambda: _clock_at(), repos=repos)


@pytest.mark.asyncio
async def test_retention_custom_retention_days(tmp_path):
    db, repos = _make_repos(tmp_path)
    _insert_llm(db, days_ago=8)   # 8 days old — beyond 7-day window
    _insert_llm(db, days_ago=3)   # 3 days old — within 7-day window

    await run_retention(clock=lambda: _clock_at(), repos=repos, retention_days=7)

    assert _count(db, "usage_llm_calls") == 1


# ---------------------------------------------------------------------------
# Batch loop — purge_before exits loop when batch returns < batch_size
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_retention_batched_purge(tmp_path):
    """Purge works correctly with a small batch_size across multiple batches."""
    db, repos = _make_repos(tmp_path)
    # Insert 5 old rows and 2 new rows — purge with batch_size=2
    for i in range(5):
        _insert_llm(db, days_ago=40)
    for i in range(2):
        _insert_llm(db, days_ago=1)

    # Use batch_size=2: first two batches of 2, then one batch of 1 (< 2 → stops)
    await run_retention(
        clock=lambda: _clock_at(),
        repos=repos,
        retention_days=30,
        batch_size=2,
    )

    assert _count(db, "usage_llm_calls") == 2

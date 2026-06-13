"""Epic 14 — daily rollup + retention lifecycle e2e tests.

Story 14.05: job wrappers drive the full cycle:
  - due-check fires rollup and retention on the first tick after midnight UTC
  - day-by-day catchup fills multiple elapsed days
  - retention purges raw rows older than retention_days
  - summary rows survive retention

All tests use injected clocks to simulate day boundaries without sleeping.
No network calls; all deps are real SQLite repos.
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from pathlib import Path

import pytest

from services.api.app.usage.migrations import bootstrap_usage_db
from services.api.app.usage.repositories import (
    UsageDailySummaryRepository,
    UsageHitlEventRepository,
    UsageLlmCallRepository,
    UsageMessageRepository,
)
from services.scheduler.app.jobs.usage_retention_job import UsageRetentionJob
from services.scheduler.app.jobs.usage_rollup_job import UsageRollupJob
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


def _insert_llm(db: str, *, day: str) -> None:
    with sqlite3.connect(db) as conn:
        conn.execute(
            "INSERT INTO usage_llm_calls"
            " (project_id, model_name, prompt_tokens, completion_tokens,"
            "  cost_usd, call_outcome, trace_id, created_at)"
            " VALUES (1, 'haiku', 100, 50, 0.01, 'customer_visible_answer', NULL, ?)",
            (f"{day}T12:00:00Z",),
        )


def _clock(ts: str) -> datetime:
    return datetime.fromisoformat(ts.replace("Z", "+00:00"))


@pytest.mark.e2e
@pytest.mark.epic("14")
@pytest.mark.story("14-05")
@pytest.mark.asyncio
async def test_e2e_rollup_job_fires_after_midnight_and_persists_state(tmp_path):
    db, repos = _make_repos(tmp_path)
    state_file = str(tmp_path / "scheduler_runs.json")
    _insert_llm(db, day="2026-05-25")  # yesterday

    job = UsageRollupJob(
        repos=repos,
        clock=lambda: _clock("2026-05-26T00:10:00Z"),
        state_path=state_file,
        rollup_hour_utc=0,
    )
    await job.run()

    # Summary row written
    rows = repos.summary.query(
        project_id=1, from_day_utc="2026-05-25", to_day_utc="2026-05-25",
        trackers=["llm"],
    )
    assert len(rows) == 1
    assert rows[0].call_count == 1

    # State file persisted
    data = json.loads(Path(state_file).read_text())
    assert data["last_rollup_date_utc"] == "2026-05-26"


@pytest.mark.e2e
@pytest.mark.epic("14")
@pytest.mark.story("14-05")
@pytest.mark.asyncio
async def test_e2e_rollup_job_does_not_double_run_on_same_day(tmp_path):
    db, repos = _make_repos(tmp_path)
    state_file = str(tmp_path / "scheduler_runs.json")
    _insert_llm(db, day="2026-05-25")

    job = UsageRollupJob(
        repos=repos,
        clock=lambda: _clock("2026-05-26T00:10:00Z"),
        state_path=state_file,
        rollup_hour_utc=0,
    )
    await job.run()  # first tick
    await job.run()  # second tick (same day — should skip)

    rows = repos.summary.query(
        project_id=1, from_day_utc="2026-05-25", to_day_utc="2026-05-25",
    )
    # Idempotent: one row per tracker, not doubled
    assert rows[0].call_count == 1


@pytest.mark.e2e
@pytest.mark.epic("14")
@pytest.mark.story("14-05")
@pytest.mark.asyncio
async def test_e2e_catchup_across_multiple_days(tmp_path):
    db, repos = _make_repos(tmp_path)
    state_file = str(tmp_path / "scheduler_runs.json")

    for day in ("2026-05-23", "2026-05-24", "2026-05-25"):
        _insert_llm(db, day=day)

    job = UsageRollupJob(
        repos=repos,
        clock=lambda: _clock("2026-05-26T01:00:00Z"),
        state_path=state_file,
        rollup_hour_utc=0,
    )
    await job.run()

    for day in ("2026-05-23", "2026-05-24", "2026-05-25"):
        rows = repos.summary.query(
            project_id=1, from_day_utc=day, to_day_utc=day, trackers=["llm"]
        )
        assert len(rows) == 1, f"Expected summary for {day}"


@pytest.mark.e2e
@pytest.mark.epic("14")
@pytest.mark.story("14-05")
@pytest.mark.asyncio
async def test_e2e_retention_job_purges_old_rows_and_leaves_summaries(tmp_path):
    db, repos = _make_repos(tmp_path)
    state_file = str(tmp_path / "scheduler_runs.json")

    # Old raw row (40 days before 2026-05-26)
    with sqlite3.connect(db) as conn:
        conn.execute(
            "INSERT INTO usage_llm_calls"
            " (project_id, model_name, prompt_tokens, completion_tokens,"
            "  cost_usd, call_outcome, trace_id, created_at)"
            " VALUES (1, 'm', 10, 5, 0.001, 'customer_visible_answer', NULL, ?)",
            ("2026-04-16T12:00:00Z",),
        )

    # Insert a summary row (should survive retention)
    with sqlite3.connect(db) as conn:
        conn.execute(
            "INSERT INTO usage_daily_summary"
            " (project_id, day_utc, tracker_type, model_name,"
            "  prompt_tokens_total, completion_tokens_total, cost_usd_total,"
            "  wasted_cost_usd, call_count)"
            " VALUES (1, '2026-04-16', 'llm', 'm', 10, 5, 0.001, 0.0, 1)"
        )

    job = UsageRetentionJob(
        repos=repos,
        clock=lambda: _clock("2026-05-26T00:10:00Z"),
        state_path=state_file,
        rollup_hour_utc=0,
        retention_days=30,
    )
    await job.run()

    # Raw row gone
    with sqlite3.connect(db) as conn:
        raw_count = conn.execute("SELECT COUNT(*) FROM usage_llm_calls").fetchone()[0]
    assert raw_count == 0

    # Summary row intact
    with sqlite3.connect(db) as conn:
        summary_count = conn.execute(
            "SELECT COUNT(*) FROM usage_daily_summary"
        ).fetchone()[0]
    assert summary_count == 1

    # State file has retention date
    data = json.loads(Path(state_file).read_text())
    assert data["last_retention_date_utc"] == "2026-05-26"

"""Tests for usage_rollup.run_rollup aggregation logic — Story 14.05."""
from __future__ import annotations

import sqlite3
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest

from services.api.app.usage.migrations import bootstrap_usage_db
from services.api.app.usage.repositories import (
    UsageDailySummaryRepository,
    UsageHitlEventRepository,
    UsageLlmCallRepository,
    UsageMessageRepository,
)
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


def _clock(day_utc: str, hour: int = 1) -> datetime:
    d = date.fromisoformat(day_utc)
    return datetime(d.year, d.month, d.day, hour, 0, 0, tzinfo=UTC)


def _insert_llm(db: str, *, project_id: int, day: str, model: str,
                prompt: int, completion: int, cost: float | None,
                outcome: str) -> None:
    with sqlite3.connect(db) as conn:
        conn.execute(
            "INSERT INTO usage_llm_calls"
            " (project_id, model_name, prompt_tokens, completion_tokens,"
            "  cost_usd, call_outcome, trace_id, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?, NULL, ?)",
            (project_id, model, prompt, completion, cost, outcome,
             f"{day}T12:00:00Z"),
        )


def _insert_msg(db: str, *, project_id: int, day: str, direction: str) -> None:
    with sqlite3.connect(db) as conn:
        conn.execute(
            "INSERT INTO usage_messages"
            " (project_id, direction, participant_role, trace_id, created_at)"
            " VALUES (?, ?, 'customer', NULL, ?)",
            (project_id, direction, f"{day}T12:00:00Z"),
        )


def _insert_hitl(db: str, *, project_id: int, day: str, event_type: str) -> None:
    with sqlite3.connect(db) as conn:
        conn.execute(
            "INSERT INTO usage_hitl_events"
            " (project_id, event_type, ticket_id, trace_id, created_at)"
            " VALUES (?, ?, 1, NULL, ?)",
            (project_id, event_type, f"{day}T12:00:00Z"),
        )


# ---------------------------------------------------------------------------
# LLM aggregation
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_rollup_produces_one_llm_row_with_correct_totals(tmp_path):
    db, repos = _make_repos(tmp_path)
    day = "2026-05-25"
    _insert_llm(db, project_id=1, day=day, model="haiku", prompt=100, completion=50,
                cost=0.01, outcome="customer_visible_answer")
    _insert_llm(db, project_id=1, day=day, model="haiku", prompt=200, completion=80,
                cost=0.02, outcome="verifier_rejected")
    _insert_llm(db, project_id=1, day=day, model="haiku", prompt=50, completion=20,
                cost=0.005, outcome="guardrails_blocked")

    await run_rollup(clock=lambda: _clock("2026-05-26"), repos=repos)

    rows = repos.summary.query(project_id=1, from_day_utc=day, to_day_utc=day,
                               trackers=["llm"])
    assert len(rows) == 1
    r = rows[0]
    assert r.tracker_type == "llm"
    assert r.model_name == "haiku"
    assert r.prompt_tokens_total == 350
    assert r.completion_tokens_total == 150
    assert r.cost_usd_total == pytest.approx(0.035)
    assert r.wasted_cost_usd == pytest.approx(0.025)  # verifier_rejected + guardrails_blocked
    assert r.call_count == 3


@pytest.mark.asyncio
async def test_rollup_wasted_cost_counts_error_outcome(tmp_path):
    db, repos = _make_repos(tmp_path)
    day = "2026-05-25"
    _insert_llm(db, project_id=1, day=day, model="m", prompt=10, completion=5,
                cost=0.001, outcome="error")
    _insert_llm(db, project_id=1, day=day, model="m", prompt=10, completion=5,
                cost=0.001, outcome="customer_visible_answer")

    await run_rollup(clock=lambda: _clock("2026-05-26"), repos=repos)

    rows = repos.summary.query(project_id=1, from_day_utc=day, to_day_utc=day)
    assert rows[0].wasted_cost_usd == pytest.approx(0.001)


@pytest.mark.asyncio
async def test_rollup_multiple_models_same_day(tmp_path):
    db, repos = _make_repos(tmp_path)
    day = "2026-05-25"
    _insert_llm(db, project_id=1, day=day, model="haiku", prompt=100, completion=50,
                cost=0.01, outcome="customer_visible_answer")
    _insert_llm(db, project_id=1, day=day, model="gemini", prompt=200, completion=100,
                cost=0.02, outcome="customer_visible_answer")

    await run_rollup(clock=lambda: _clock("2026-05-26"), repos=repos)

    rows = repos.summary.query(project_id=1, from_day_utc=day, to_day_utc=day,
                               trackers=["llm"])
    assert len(rows) == 2
    model_names = {r.model_name for r in rows}
    assert model_names == {"haiku", "gemini"}


# ---------------------------------------------------------------------------
# Messages aggregation
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_rollup_messages_aggregation(tmp_path):
    db, repos = _make_repos(tmp_path)
    day = "2026-05-25"
    _insert_msg(db, project_id=1, day=day, direction="in")
    _insert_msg(db, project_id=1, day=day, direction="in")
    _insert_msg(db, project_id=1, day=day, direction="out")

    await run_rollup(clock=lambda: _clock("2026-05-26"), repos=repos)

    rows = repos.summary.query(project_id=1, from_day_utc=day, to_day_utc=day,
                               trackers=["messages"])
    assert len(rows) == 1
    r = rows[0]
    assert r.tracker_type == "messages"
    assert r.model_name == ""
    assert r.in_count == 2
    assert r.out_count == 1
    assert r.call_count == 3


# ---------------------------------------------------------------------------
# HITL aggregation
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_rollup_hitl_aggregation(tmp_path):
    db, repos = _make_repos(tmp_path)
    day = "2026-05-25"
    for event in ("created", "assigned", "replied", "resolved"):
        _insert_hitl(db, project_id=1, day=day, event_type=event)
    _insert_hitl(db, project_id=1, day=day, event_type="created")  # 2nd created

    await run_rollup(clock=lambda: _clock("2026-05-26"), repos=repos)

    rows = repos.summary.query(project_id=1, from_day_utc=day, to_day_utc=day,
                               trackers=["hitl"])
    assert len(rows) == 1
    r = rows[0]
    assert r.tracker_type == "hitl"
    assert r.model_name == ""
    assert r.hitl_created_count == 2
    assert r.hitl_assigned_count == 1
    assert r.hitl_replied_count == 1
    assert r.hitl_resolved_count == 1
    assert r.call_count == 5


# ---------------------------------------------------------------------------
# Empty day — no summary row produced
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_rollup_empty_day_produces_no_row(tmp_path):
    db, repos = _make_repos(tmp_path)
    day = "2026-05-25"
    # Insert data only for day 24; day 25 is empty
    _insert_llm(db, project_id=1, day="2026-05-24", model="m", prompt=10,
                completion=5, cost=0.001, outcome="customer_visible_answer")

    await run_rollup(clock=lambda: _clock("2026-05-26"), repos=repos)

    rows = repos.summary.query(project_id=1, from_day_utc=day, to_day_utc=day)
    assert rows == []


# ---------------------------------------------------------------------------
# Idempotency — running twice produces the same rows
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_rollup_idempotent(tmp_path):
    db, repos = _make_repos(tmp_path)
    day = "2026-05-25"
    _insert_llm(db, project_id=1, day=day, model="m", prompt=100, completion=50,
                cost=0.01, outcome="customer_visible_answer")

    await run_rollup(clock=lambda: _clock("2026-05-26"), repos=repos)
    await run_rollup(clock=lambda: _clock("2026-05-26"), repos=repos)

    rows = repos.summary.query(project_id=1, from_day_utc=day, to_day_utc=day)
    assert len(rows) == 1
    assert rows[0].call_count == 1  # still 1 — not doubled


# ---------------------------------------------------------------------------
# Day-by-day catchup
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_rollup_catchup_fills_missing_days(tmp_path):
    db, repos = _make_repos(tmp_path)
    # Seed data for days 21-25 of May 2026
    for day_n in range(21, 26):
        day = f"2026-05-{day_n:02d}"
        _insert_llm(db, project_id=1, day=day, model="m", prompt=10,
                    completion=5, cost=0.001, outcome="customer_visible_answer")

    # Most recent summary is 2026-05-20; today is 2026-05-26 → should roll up 21-25
    # Pre-insert a summary for 2026-05-20 to set the "last known" day
    repos.summary.upsert(_make_summary_row(project_id=1, day_utc="2026-05-20"))

    await run_rollup(clock=lambda: _clock("2026-05-26"), repos=repos)

    for day_n in range(21, 26):
        day = f"2026-05-{day_n:02d}"
        rows = repos.summary.query(project_id=1, from_day_utc=day, to_day_utc=day,
                                   trackers=["llm"])
        assert len(rows) == 1, f"Expected summary for {day}"


@pytest.mark.asyncio
async def test_rollup_catchup_does_not_roll_up_today(tmp_path):
    """Rollup only covers up to yesterday — today's data stays raw."""
    db, repos = _make_repos(tmp_path)
    today = "2026-05-26"
    _insert_llm(db, project_id=1, day=today, model="m", prompt=10,
                completion=5, cost=0.001, outcome="customer_visible_answer")

    await run_rollup(clock=lambda: _clock(today), repos=repos)

    rows = repos.summary.query(project_id=1, from_day_utc=today, to_day_utc=today)
    assert rows == []


@pytest.mark.asyncio
async def test_rollup_catchup_capped_at_30_days(tmp_path):
    db, repos = _make_repos(tmp_path)
    today_dt = date(2026, 5, 26)
    # Seed data for 40 days back
    for i in range(1, 41):
        day = (today_dt - timedelta(days=i)).isoformat()
        _insert_llm(db, project_id=1, day=day, model="m", prompt=10,
                    completion=5, cost=0.001, outcome="customer_visible_answer")

    await run_rollup(clock=lambda: _clock("2026-05-26"), repos=repos)

    # Days 31-40 back are beyond the 30-day cap — should have no summary
    for i in range(31, 41):
        day = (today_dt - timedelta(days=i)).isoformat()
        rows = repos.summary.query(project_id=1, from_day_utc=day, to_day_utc=day)
        assert rows == [], f"Should have no summary for day {day} (beyond 30-day cap)"

    # Days 1-30 back should each have a summary
    for i in range(1, 31):
        day = (today_dt - timedelta(days=i)).isoformat()
        rows = repos.summary.query(project_id=1, from_day_utc=day, to_day_utc=day)
        assert len(rows) >= 1, f"Expected summary for {day}"


# ---------------------------------------------------------------------------
# Multiple projects
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_rollup_handles_multiple_projects(tmp_path):
    db, repos = _make_repos(tmp_path)
    day = "2026-05-25"
    _insert_llm(db, project_id=1, day=day, model="m", prompt=10, completion=5,
                cost=0.001, outcome="customer_visible_answer")
    _insert_llm(db, project_id=2, day=day, model="m", prompt=20, completion=10,
                cost=0.002, outcome="customer_visible_answer")

    await run_rollup(clock=lambda: _clock("2026-05-26"), repos=repos)

    for pid in (1, 2):
        rows = repos.summary.query(project_id=pid, from_day_utc=day, to_day_utc=day)
        assert len(rows) == 1
        assert rows[0].project_id == pid


# ---------------------------------------------------------------------------
# No data at all — rollup runs without error
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_rollup_with_no_data_completes_without_error(tmp_path):
    _, repos = _make_repos(tmp_path)
    await run_rollup(clock=lambda: _clock("2026-05-26"), repos=repos)
    rows = repos.summary.query(project_id=1, from_day_utc="2026-05-01",
                               to_day_utc="2026-05-26")
    assert rows == []


# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------

def _make_summary_row(*, project_id: int, day_utc: str):
    from services.api.app.usage.repositories import UsageDailySummaryRow
    return UsageDailySummaryRow(
        project_id=project_id, day_utc=day_utc, tracker_type="llm",
        model_name="placeholder", prompt_tokens_total=0,
        completion_tokens_total=0, cost_usd_total=0.0,
        wasted_cost_usd=0.0, call_count=0,
        in_count=None, out_count=None,
        hitl_created_count=None, hitl_assigned_count=None,
        hitl_replied_count=None, hitl_resolved_count=None,
    )

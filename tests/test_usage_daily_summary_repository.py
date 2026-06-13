"""Tests for UsageDailySummaryRepository — Story 14.05."""
from __future__ import annotations

import pytest

from services.api.app.usage.migrations import bootstrap_usage_db
from services.api.app.usage.repositories import (
    UsageDailySummaryRepository,
    UsageDailySummaryRow,
)


def _row(
    *,
    project_id: int = 1,
    day_utc: str = "2026-05-25",
    tracker_type: str = "llm",
    model_name: str = "claude-haiku-4-5",
    prompt_tokens_total: int | None = 100,
    completion_tokens_total: int | None = 50,
    cost_usd_total: float | None = 0.01,
    wasted_cost_usd: float | None = 0.002,
    call_count: int | None = 5,
    in_count: int | None = None,
    out_count: int | None = None,
    hitl_created_count: int | None = None,
    hitl_assigned_count: int | None = None,
    hitl_replied_count: int | None = None,
    hitl_resolved_count: int | None = None,
) -> UsageDailySummaryRow:
    return UsageDailySummaryRow(
        project_id=project_id,
        day_utc=day_utc,
        tracker_type=tracker_type,
        model_name=model_name,
        prompt_tokens_total=prompt_tokens_total,
        completion_tokens_total=completion_tokens_total,
        cost_usd_total=cost_usd_total,
        wasted_cost_usd=wasted_cost_usd,
        call_count=call_count,
        in_count=in_count,
        out_count=out_count,
        hitl_created_count=hitl_created_count,
        hitl_assigned_count=hitl_assigned_count,
        hitl_replied_count=hitl_replied_count,
        hitl_resolved_count=hitl_resolved_count,
    )


@pytest.fixture
def repo(tmp_path):
    db = str(tmp_path / "usage.db")
    bootstrap_usage_db(db)
    return UsageDailySummaryRepository(db_path=db)


# ---------------------------------------------------------------------------
# upsert — insert path
# ---------------------------------------------------------------------------

def test_upsert_inserts_row(repo):
    row = _row()
    repo.upsert(row)
    rows = repo.query(project_id=1, from_day_utc="2026-05-25", to_day_utc="2026-05-25")
    assert len(rows) == 1
    assert rows[0].project_id == 1
    assert rows[0].day_utc == "2026-05-25"
    assert rows[0].tracker_type == "llm"
    assert rows[0].model_name == "claude-haiku-4-5"
    assert rows[0].prompt_tokens_total == 100
    assert rows[0].completion_tokens_total == 50
    assert rows[0].cost_usd_total == pytest.approx(0.01)
    assert rows[0].wasted_cost_usd == pytest.approx(0.002)
    assert rows[0].call_count == 5


# ---------------------------------------------------------------------------
# upsert — update path (idempotent UPSERT)
# ---------------------------------------------------------------------------

def test_upsert_updates_existing_row(repo):
    row1 = _row(call_count=5, cost_usd_total=0.01)
    repo.upsert(row1)
    row2 = _row(call_count=10, cost_usd_total=0.02)
    repo.upsert(row2)
    rows = repo.query(project_id=1, from_day_utc="2026-05-25", to_day_utc="2026-05-25")
    assert len(rows) == 1
    assert rows[0].call_count == 10
    assert rows[0].cost_usd_total == pytest.approx(0.02)


def test_upsert_same_row_twice_produces_one_row(repo):
    row = _row()
    repo.upsert(row)
    repo.upsert(row)
    rows = repo.query(project_id=1, from_day_utc="2026-05-25", to_day_utc="2026-05-25")
    assert len(rows) == 1


# ---------------------------------------------------------------------------
# upsert — messages tracker (empty model_name sentinel)
# ---------------------------------------------------------------------------

def test_upsert_messages_row(repo):
    row = _row(
        tracker_type="messages",
        model_name="",
        prompt_tokens_total=None,
        completion_tokens_total=None,
        cost_usd_total=None,
        wasted_cost_usd=None,
        call_count=20,
        in_count=12,
        out_count=8,
    )
    repo.upsert(row)
    rows = repo.query(project_id=1, from_day_utc="2026-05-25", to_day_utc="2026-05-25",
                      trackers=["messages"])
    assert len(rows) == 1
    assert rows[0].tracker_type == "messages"
    assert rows[0].model_name == ""
    assert rows[0].in_count == 12
    assert rows[0].out_count == 8


# ---------------------------------------------------------------------------
# upsert — hitl tracker
# ---------------------------------------------------------------------------

def test_upsert_hitl_row(repo):
    row = _row(
        tracker_type="hitl",
        model_name="",
        prompt_tokens_total=None,
        completion_tokens_total=None,
        cost_usd_total=None,
        wasted_cost_usd=None,
        call_count=4,
        hitl_created_count=1,
        hitl_assigned_count=1,
        hitl_replied_count=1,
        hitl_resolved_count=1,
    )
    repo.upsert(row)
    rows = repo.query(project_id=1, from_day_utc="2026-05-25", to_day_utc="2026-05-25",
                      trackers=["hitl"])
    assert len(rows) == 1
    assert rows[0].hitl_created_count == 1
    assert rows[0].hitl_resolved_count == 1


# ---------------------------------------------------------------------------
# query — tracker filter
# ---------------------------------------------------------------------------

def test_query_filters_by_tracker(repo):
    repo.upsert(_row(tracker_type="llm", model_name="model-a"))
    repo.upsert(_row(tracker_type="messages", model_name="", call_count=3,
                     prompt_tokens_total=None, completion_tokens_total=None,
                     cost_usd_total=None, wasted_cost_usd=None,
                     in_count=2, out_count=1))
    llm_rows = repo.query(project_id=1, from_day_utc="2026-05-25",
                          to_day_utc="2026-05-25", trackers=["llm"])
    assert len(llm_rows) == 1
    assert llm_rows[0].tracker_type == "llm"

    msg_rows = repo.query(project_id=1, from_day_utc="2026-05-25",
                          to_day_utc="2026-05-25", trackers=["messages"])
    assert len(msg_rows) == 1
    assert msg_rows[0].tracker_type == "messages"


def test_query_no_tracker_filter_returns_all(repo):
    repo.upsert(_row(tracker_type="llm", model_name="model-a"))
    repo.upsert(_row(tracker_type="messages", model_name="", call_count=3,
                     prompt_tokens_total=None, completion_tokens_total=None,
                     cost_usd_total=None, wasted_cost_usd=None,
                     in_count=2, out_count=1))
    rows = repo.query(project_id=1, from_day_utc="2026-05-25", to_day_utc="2026-05-25")
    assert len(rows) == 2


def test_query_empty_tracker_list_returns_all(repo):
    repo.upsert(_row(tracker_type="llm", model_name="model-a"))
    rows = repo.query(project_id=1, from_day_utc="2026-05-25", to_day_utc="2026-05-25",
                      trackers=[])
    assert len(rows) == 1


# ---------------------------------------------------------------------------
# query — date range filter
# ---------------------------------------------------------------------------

def test_query_date_range_exclusive(repo):
    repo.upsert(_row(day_utc="2026-05-24"))
    repo.upsert(_row(day_utc="2026-05-25", model_name="model-b"))
    repo.upsert(_row(day_utc="2026-05-26", model_name="model-c"))
    rows = repo.query(project_id=1, from_day_utc="2026-05-25", to_day_utc="2026-05-25")
    assert len(rows) == 1
    assert rows[0].day_utc == "2026-05-25"


def test_query_returns_empty_for_no_matching_rows(repo):
    rows = repo.query(project_id=1, from_day_utc="2026-05-25", to_day_utc="2026-05-25")
    assert rows == []


# ---------------------------------------------------------------------------
# query — ordering
# ---------------------------------------------------------------------------

def test_query_ordered_by_day_tracker_model(repo):
    repo.upsert(_row(day_utc="2026-05-26", tracker_type="llm", model_name="z-model"))
    repo.upsert(_row(day_utc="2026-05-25", tracker_type="llm", model_name="a-model"))
    repo.upsert(_row(day_utc="2026-05-25", tracker_type="messages", model_name="",
                     prompt_tokens_total=None, completion_tokens_total=None,
                     cost_usd_total=None, wasted_cost_usd=None,
                     call_count=1, in_count=1, out_count=0))
    rows = repo.query(project_id=1, from_day_utc="2026-05-25", to_day_utc="2026-05-26")
    days = [r.day_utc for r in rows]
    assert days == sorted(days)
    # day 25 should come before day 26
    assert rows[0].day_utc == "2026-05-25"
    assert rows[-1].day_utc == "2026-05-26"


# ---------------------------------------------------------------------------
# query — project_id isolation
# ---------------------------------------------------------------------------

def test_query_isolates_project_id(repo):
    repo.upsert(_row(project_id=1))
    repo.upsert(_row(project_id=2, model_name="other"))
    rows = repo.query(project_id=1, from_day_utc="2026-05-25", to_day_utc="2026-05-25")
    assert len(rows) == 1
    assert rows[0].project_id == 1


# ---------------------------------------------------------------------------
# multiple models on same day
# ---------------------------------------------------------------------------

def test_upsert_multiple_models_same_day(repo):
    repo.upsert(_row(model_name="model-a", call_count=3))
    repo.upsert(_row(model_name="model-b", call_count=7))
    rows = repo.query(project_id=1, from_day_utc="2026-05-25", to_day_utc="2026-05-25")
    assert len(rows) == 2
    model_names = {r.model_name for r in rows}
    assert model_names == {"model-a", "model-b"}

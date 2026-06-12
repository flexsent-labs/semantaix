"""Tests for usage repository skeletons (Story 14.01).

Verifies:
- Each repository can be constructed with ``db_path=":memory:"`` after bootstrap
- Every not-yet-implemented method raises ``NotImplementedError``
- Frozen dataclass row types are immutable
"""

from __future__ import annotations

import pytest

from services.api.app.usage.migrations import bootstrap_usage_db
from services.api.app.usage.repositories import (
    UsageDailySummaryRepository,
    UsageDailySummaryRow,
    UsageHitlEventRepository,
    UsageHitlEventRow,
    UsageIncidentRepository,
    UsageIncidentRow,
    UsageLlmCallRepository,
    UsageLlmCallRow,
    UsageMessageRepository,
    UsageMessageRow,
)

# ---------------------------------------------------------------------------
# Frozen dataclass immutability
# ---------------------------------------------------------------------------

def test_llm_call_row_is_frozen():
    row = UsageLlmCallRow(
        id=1, project_id=1, model_name="gpt-4o",
        prompt_tokens=100, completion_tokens=50,
        cost_usd=0.01, call_outcome="customer_visible_answer",
        trace_id="abc", created_at="2026-06-11T00:00:00Z",
    )
    with pytest.raises((AttributeError, TypeError)):
        row.id = 2  # type: ignore[misc]


def test_message_row_is_frozen():
    row = UsageMessageRow(
        id=1, project_id=1, direction="in", participant_role="customer",
        trace_id=None, created_at="2026-06-11T00:00:00Z",
    )
    with pytest.raises((AttributeError, TypeError)):
        row.id = 2  # type: ignore[misc]


def test_hitl_event_row_is_frozen():
    row = UsageHitlEventRow(
        id=1, project_id=1, event_type="created", ticket_id=42,
        trace_id=None, created_at="2026-06-11T00:00:00Z",
    )
    with pytest.raises((AttributeError, TypeError)):
        row.id = 2  # type: ignore[misc]


def test_daily_summary_row_is_frozen():
    row = UsageDailySummaryRow(
        project_id=1, day_utc="2026-06-11", tracker_type="llm",
        model_name="gpt-4o", prompt_tokens_total=1000,
        completion_tokens_total=500, cost_usd_total=0.10,
        wasted_cost_usd=0.0, call_count=5,
        in_count=None, out_count=None,
        hitl_created_count=None, hitl_assigned_count=None,
        hitl_replied_count=None, hitl_resolved_count=None,
    )
    with pytest.raises((AttributeError, TypeError)):
        row.project_id = 2  # type: ignore[misc]


def test_incident_row_is_frozen():
    row = UsageIncidentRow(
        id=1, project_id=1, started_at="2026-06-11T00:00:00Z",
        ended_at=None, breached_trackers="[]", peak_pct=None,
        total_excess_cost_usd=None,
    )
    with pytest.raises((AttributeError, TypeError)):
        row.id = 2  # type: ignore[misc]


# ---------------------------------------------------------------------------
# Repository construction succeeds after bootstrap
# ---------------------------------------------------------------------------

def test_llm_call_repo_constructs(tmp_path):
    db = str(tmp_path / "usage.db")
    bootstrap_usage_db(db)
    UsageLlmCallRepository(db_path=db)


def test_message_repo_constructs(tmp_path):
    db = str(tmp_path / "usage.db")
    bootstrap_usage_db(db)
    UsageMessageRepository(db_path=db)


def test_hitl_event_repo_constructs(tmp_path):
    db = str(tmp_path / "usage.db")
    bootstrap_usage_db(db)
    UsageHitlEventRepository(db_path=db)


def test_daily_summary_repo_constructs(tmp_path):
    db = str(tmp_path / "usage.db")
    bootstrap_usage_db(db)
    UsageDailySummaryRepository(db_path=db)


def test_incident_repo_constructs(tmp_path):
    db = str(tmp_path / "usage.db")
    bootstrap_usage_db(db)
    UsageIncidentRepository(db_path=db)


# ---------------------------------------------------------------------------
# Every method raises NotImplementedError
# ---------------------------------------------------------------------------

def test_llm_call_repo_record_is_implemented(tmp_path):
    """record() is implemented in Story 14.02 — no longer raises NotImplementedError."""
    db = str(tmp_path / "usage.db")
    bootstrap_usage_db(db)
    repo = UsageLlmCallRepository(db_path=db)
    row = UsageLlmCallRow(
        id=0,
        project_id=1,
        model_name="gpt-4o",
        prompt_tokens=10,
        completion_tokens=5,
        cost_usd=0.001,
        call_outcome="customer_visible_answer",
        trace_id="t-1",
        created_at="2026-06-11T00:00:00Z",
    )
    repo.record(row)  # must not raise


def test_llm_call_repo_list_for_day_not_implemented(tmp_path):
    db = str(tmp_path / "usage.db")
    bootstrap_usage_db(db)
    repo = UsageLlmCallRepository(db_path=db)
    with pytest.raises(NotImplementedError):
        repo.list_for_day(project_id=1, day_utc="2026-06-11")


def test_message_repo_record_is_implemented(tmp_path):
    """record() is implemented in Story 14.03 — no longer raises NotImplementedError."""
    db = str(tmp_path / "usage.db")
    bootstrap_usage_db(db)
    repo = UsageMessageRepository(db_path=db)
    row = UsageMessageRow(
        id=0,
        project_id=1,
        direction="in",
        participant_role="customer",
        trace_id=None,
        created_at="2026-06-12T00:00:00Z",
    )
    repo.record(row)  # must not raise


def test_message_repo_count_for_day_not_implemented(tmp_path):
    db = str(tmp_path / "usage.db")
    bootstrap_usage_db(db)
    repo = UsageMessageRepository(db_path=db)
    with pytest.raises(NotImplementedError):
        repo.count_for_day(project_id=1, day_utc="2026-06-11")


def test_hitl_event_repo_record_is_implemented(tmp_path):
    """record() is implemented in Story 14.04 — no longer raises NotImplementedError."""
    db = str(tmp_path / "usage.db")
    bootstrap_usage_db(db)
    repo = UsageHitlEventRepository(db_path=db)
    row = UsageHitlEventRow(
        id=0,
        project_id=1,
        event_type="created",
        ticket_id=10,
        trace_id=None,
        created_at="2026-06-12T00:00:00Z",
    )
    repo.record(row)  # must not raise


def test_hitl_event_repo_count_for_day_not_implemented(tmp_path):
    db = str(tmp_path / "usage.db")
    bootstrap_usage_db(db)
    repo = UsageHitlEventRepository(db_path=db)
    with pytest.raises(NotImplementedError):
        repo.count_for_day(project_id=1, day_utc="2026-06-11")


def test_daily_summary_repo_upsert_not_implemented(tmp_path):
    db = str(tmp_path / "usage.db")
    bootstrap_usage_db(db)
    repo = UsageDailySummaryRepository(db_path=db)
    with pytest.raises(NotImplementedError):
        repo.upsert(None)  # type: ignore[arg-type]


def test_daily_summary_repo_query_not_implemented(tmp_path):
    db = str(tmp_path / "usage.db")
    bootstrap_usage_db(db)
    repo = UsageDailySummaryRepository(db_path=db)
    with pytest.raises(NotImplementedError):
        repo.query(project_id=1, from_day="2026-06-01", to_day="2026-06-11")


def test_daily_summary_repo_query_wasted_not_implemented(tmp_path):
    db = str(tmp_path / "usage.db")
    bootstrap_usage_db(db)
    repo = UsageDailySummaryRepository(db_path=db)
    with pytest.raises(NotImplementedError):
        repo.query_wasted(project_id=1, from_day="2026-06-01", to_day="2026-06-11")


def test_incident_repo_start_not_implemented(tmp_path):
    db = str(tmp_path / "usage.db")
    bootstrap_usage_db(db)
    repo = UsageIncidentRepository(db_path=db)
    with pytest.raises(NotImplementedError):
        repo.start(None)  # type: ignore[arg-type]


def test_incident_repo_expand_not_implemented(tmp_path):
    db = str(tmp_path / "usage.db")
    bootstrap_usage_db(db)
    repo = UsageIncidentRepository(db_path=db)
    with pytest.raises(NotImplementedError):
        repo.expand(incident_id=1, additional_trackers="[]")


def test_incident_repo_end_not_implemented(tmp_path):
    db = str(tmp_path / "usage.db")
    bootstrap_usage_db(db)
    repo = UsageIncidentRepository(db_path=db)
    with pytest.raises(NotImplementedError):
        repo.end(incident_id=1, ended_at="2026-06-11T01:00:00Z")


def test_incident_repo_active_for_project_not_implemented(tmp_path):
    db = str(tmp_path / "usage.db")
    bootstrap_usage_db(db)
    repo = UsageIncidentRepository(db_path=db)
    with pytest.raises(NotImplementedError):
        repo.active_for_project(project_id=1)


def test_incident_repo_list_for_window_not_implemented(tmp_path):
    db = str(tmp_path / "usage.db")
    bootstrap_usage_db(db)
    repo = UsageIncidentRepository(db_path=db)
    with pytest.raises(NotImplementedError):
        repo.list_for_window(
            project_id=1,
            from_ts="2026-06-01T00:00:00Z",
            to_ts="2026-06-11T00:00:00Z",
        )

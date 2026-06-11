"""Tests for UsageLlmCallRepository.record (Story 14.02)."""

from __future__ import annotations

import sqlite3

import pytest

from services.api.app.usage.migrations import bootstrap_usage_db
from services.api.app.usage.repositories import (
    CALL_OUTCOMES,
    UsageLlmCallRepository,
    UsageLlmCallRow,
)


def _row(
    *,
    project_id: int = 1,
    model_name: str = "gpt-4o",
    prompt_tokens: int = 10,
    completion_tokens: int = 20,
    cost_usd: float | None = 0.003,
    call_outcome: str = "customer_visible_answer",
    trace_id: str | None = "t1",
    created_at: str = "2026-06-11T00:00:00Z",
) -> UsageLlmCallRow:
    return UsageLlmCallRow(
        id=0,
        project_id=project_id,
        model_name=model_name,
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        cost_usd=cost_usd,
        call_outcome=call_outcome,
        trace_id=trace_id,
        created_at=created_at,
    )


# ---------------------------------------------------------------------------
# CALL_OUTCOMES constant
# ---------------------------------------------------------------------------

def test_call_outcomes_contains_six_values():
    assert len(CALL_OUTCOMES) == 6


def test_call_outcomes_contains_expected_values():
    assert CALL_OUTCOMES == frozenset({
        "customer_visible_answer",
        "verifier_rejected",
        "escalated_to_hitl",
        "guardrails_blocked",
        "moderation_triggered",
        "error",
    })


# ---------------------------------------------------------------------------
# record() — basic insert
# ---------------------------------------------------------------------------

def test_record_inserts_row(tmp_path):
    db = str(tmp_path / "usage.db")
    bootstrap_usage_db(db)
    repo = UsageLlmCallRepository(db_path=db)
    repo.record(_row())
    with sqlite3.connect(db) as conn:
        rows = conn.execute("SELECT * FROM usage_llm_calls").fetchall()
    assert len(rows) == 1


def test_record_round_trips_all_fields(tmp_path):
    db = str(tmp_path / "usage.db")
    bootstrap_usage_db(db)
    repo = UsageLlmCallRepository(db_path=db)
    repo.record(_row(prompt_tokens=100, completion_tokens=200, cost_usd=0.05))
    with sqlite3.connect(db) as conn:
        row = conn.execute(
            "SELECT project_id, model_name, prompt_tokens, completion_tokens,"
            " cost_usd, call_outcome, trace_id, created_at FROM usage_llm_calls"
        ).fetchone()
    assert row == (  # noqa: E501
        1, "gpt-4o", 100, 200, 0.05, "customer_visible_answer", "t1", "2026-06-11T00:00:00Z"
    )


def test_record_inserts_null_cost_usd(tmp_path):
    db = str(tmp_path / "usage.db")
    bootstrap_usage_db(db)
    repo = UsageLlmCallRepository(db_path=db)
    repo.record(_row(cost_usd=None))
    with sqlite3.connect(db) as conn:
        cost = conn.execute("SELECT cost_usd FROM usage_llm_calls").fetchone()[0]
    assert cost is None


def test_record_auto_assigns_id(tmp_path):
    db = str(tmp_path / "usage.db")
    bootstrap_usage_db(db)
    repo = UsageLlmCallRepository(db_path=db)
    repo.record(_row())
    repo.record(_row())
    with sqlite3.connect(db) as conn:
        ids = [r[0] for r in conn.execute("SELECT id FROM usage_llm_calls ORDER BY id").fetchall()]
    assert ids == [1, 2]


# ---------------------------------------------------------------------------
# record() — call_outcome validation
# ---------------------------------------------------------------------------

def test_record_raises_before_db_on_invalid_call_outcome(tmp_path):
    db = str(tmp_path / "usage.db")
    bootstrap_usage_db(db)
    repo = UsageLlmCallRepository(db_path=db)
    with pytest.raises(ValueError, match="call_outcome"):
        repo.record(_row(call_outcome="bogus"))
    # No rows inserted
    with sqlite3.connect(db) as conn:
        count = conn.execute("SELECT COUNT(*) FROM usage_llm_calls").fetchone()[0]
    assert count == 0


def test_all_six_call_outcomes_insert_successfully(tmp_path):
    db = str(tmp_path / "usage.db")
    bootstrap_usage_db(db)
    repo = UsageLlmCallRepository(db_path=db)
    for outcome in CALL_OUTCOMES:
        repo.record(_row(call_outcome=outcome))
    with sqlite3.connect(db) as conn:
        count = conn.execute("SELECT COUNT(*) FROM usage_llm_calls").fetchone()[0]
    assert count == 6

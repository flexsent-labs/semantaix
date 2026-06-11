"""Tests for usage DB migration / bootstrap (Story 14.01).

Covers:
- Idempotency: run twice → same sqlite_master + PRAGMA results
- WAL mode active after bootstrap
- All five tables present with required columns and CHECK constraints
- All five indexes present with the documented names
- CHECK constraint enforcement (bad enum values raise IntegrityError)
- `usage_db_bootstrapped` structured log emitted on each call
- `tables_created` count: 5 on fresh DB, 0 on re-run
"""

from __future__ import annotations

import sqlite3

import pytest

from services.api.app.usage.migrations import bootstrap_usage_db

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _tables(db: str) -> set[str]:
    with sqlite3.connect(db) as conn:
        return {
            r[0]
            for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
            ).fetchall()
        }


def _indexes(db: str) -> set[str]:
    with sqlite3.connect(db) as conn:
        return {
            r[0]
            for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='index'"
            ).fetchall()
        }


def _columns(db: str, table: str) -> list[str]:
    with sqlite3.connect(db) as conn:
        return [r[1] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()]


def _insert(db: str, sql: str, params: tuple) -> None:
    with sqlite3.connect(db) as conn:
        conn.execute(sql, params)


# ---------------------------------------------------------------------------
# Bootstrap basics
# ---------------------------------------------------------------------------

_EXPECTED_TABLES = {
    "usage_llm_calls",
    "usage_messages",
    "usage_hitl_events",
    "usage_daily_summary",
    "usage_incidents",
}

_EXPECTED_INDEXES = {
    "usage_llm_calls_project_created_idx",
    "usage_messages_project_created_idx",
    "usage_hitl_events_project_created_idx",
    "usage_daily_summary_project_day_model_idx",
    "usage_incidents_project_started_idx",
}


def test_all_five_tables_created(tmp_path):
    db = str(tmp_path / "usage.db")
    bootstrap_usage_db(db)
    assert _EXPECTED_TABLES.issubset(_tables(db))


def test_all_five_indexes_created(tmp_path):
    db = str(tmp_path / "usage.db")
    bootstrap_usage_db(db)
    assert _EXPECTED_INDEXES.issubset(_indexes(db))


def test_wal_mode_active(tmp_path):
    db = str(tmp_path / "usage.db")
    bootstrap_usage_db(db)
    with sqlite3.connect(db) as conn:
        mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
    assert mode == "wal"


# ---------------------------------------------------------------------------
# Idempotency
# ---------------------------------------------------------------------------

def test_idempotent_tables(tmp_path):
    db = str(tmp_path / "usage.db")
    bootstrap_usage_db(db)
    before = _tables(db)
    bootstrap_usage_db(db)
    assert _tables(db) == before


def test_idempotent_indexes(tmp_path):
    db = str(tmp_path / "usage.db")
    bootstrap_usage_db(db)
    before = _indexes(db)
    bootstrap_usage_db(db)
    assert _indexes(db) == before


def test_idempotent_columns(tmp_path):
    db = str(tmp_path / "usage.db")
    bootstrap_usage_db(db)
    before = {t: _columns(db, t) for t in _EXPECTED_TABLES}
    bootstrap_usage_db(db)
    assert {t: _columns(db, t) for t in _EXPECTED_TABLES} == before


# ---------------------------------------------------------------------------
# Structured log
# ---------------------------------------------------------------------------

def test_log_emitted_on_fresh_bootstrap(tmp_path, caplog):
    import logging
    db = str(tmp_path / "usage.db")
    with caplog.at_level(logging.INFO, logger="services.api.app.usage.migrations"):
        bootstrap_usage_db(db)
    assert any("usage_db_bootstrapped" in r.message for r in caplog.records)


def test_tables_created_count_five_on_fresh(tmp_path, caplog):
    import logging
    db = str(tmp_path / "usage.db")
    with caplog.at_level(logging.INFO, logger="services.api.app.usage.migrations"):
        bootstrap_usage_db(db)
    record = next(r for r in caplog.records if "usage_db_bootstrapped" in r.message)
    assert record.__dict__.get("tables_created") == 5


def test_tables_created_count_zero_on_rerun(tmp_path, caplog):
    import logging
    db = str(tmp_path / "usage.db")
    with caplog.at_level(logging.INFO, logger="services.api.app.usage.migrations"):
        bootstrap_usage_db(db)  # first run
    caplog.clear()
    with caplog.at_level(logging.INFO, logger="services.api.app.usage.migrations"):
        bootstrap_usage_db(db)  # re-run — must emit tables_created=0
    record = next(r for r in caplog.records if "usage_db_bootstrapped" in r.message)
    assert record.__dict__.get("tables_created") == 0


# ---------------------------------------------------------------------------
# Column shapes
# ---------------------------------------------------------------------------

def test_usage_llm_calls_columns(tmp_path):
    db = str(tmp_path / "usage.db")
    bootstrap_usage_db(db)
    cols = _columns(db, "usage_llm_calls")
    assert cols == [
        "id", "project_id", "model_name", "prompt_tokens",
        "completion_tokens", "cost_usd", "call_outcome", "trace_id", "created_at",
    ]


def test_usage_messages_columns(tmp_path):
    db = str(tmp_path / "usage.db")
    bootstrap_usage_db(db)
    cols = _columns(db, "usage_messages")
    assert cols == ["id", "project_id", "direction", "participant_role", "trace_id", "created_at"]


def test_usage_hitl_events_columns(tmp_path):
    db = str(tmp_path / "usage.db")
    bootstrap_usage_db(db)
    cols = _columns(db, "usage_hitl_events")
    assert cols == ["id", "project_id", "event_type", "ticket_id", "trace_id", "created_at"]


def test_usage_daily_summary_columns(tmp_path):
    db = str(tmp_path / "usage.db")
    bootstrap_usage_db(db)
    cols = _columns(db, "usage_daily_summary")
    assert cols == [
        "project_id", "day_utc", "tracker_type", "model_name",
        "prompt_tokens_total", "completion_tokens_total", "cost_usd_total",
        "wasted_cost_usd", "call_count", "in_count", "out_count",
        "hitl_created_count", "hitl_assigned_count", "hitl_replied_count",
        "hitl_resolved_count",
    ]


def test_usage_incidents_columns(tmp_path):
    db = str(tmp_path / "usage.db")
    bootstrap_usage_db(db)
    cols = _columns(db, "usage_incidents")
    assert cols == [
        "id", "project_id", "started_at", "ended_at",
        "breached_trackers", "peak_pct", "total_excess_cost_usd",
    ]


# ---------------------------------------------------------------------------
# CHECK constraint enforcement
# ---------------------------------------------------------------------------

def test_check_direction_rejects_invalid(tmp_path):
    db = str(tmp_path / "usage.db")
    bootstrap_usage_db(db)
    with sqlite3.connect(db) as conn:
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO usage_messages (project_id, direction, participant_role, created_at)"
                " VALUES (1, 'sideways', 'customer', '2026-06-11T00:00:00Z')"
            )


def test_check_participant_role_rejects_invalid(tmp_path):
    db = str(tmp_path / "usage.db")
    bootstrap_usage_db(db)
    with sqlite3.connect(db) as conn:
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO usage_messages (project_id, direction, participant_role, created_at)"
                " VALUES (1, 'in', 'bot', '2026-06-11T00:00:00Z')"
            )


def test_check_event_type_rejects_invalid(tmp_path):
    db = str(tmp_path / "usage.db")
    bootstrap_usage_db(db)
    with sqlite3.connect(db) as conn:
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO usage_hitl_events (project_id, event_type, ticket_id, created_at)"
                " VALUES (1, 'bogus', 42, '2026-06-11T00:00:00Z')"
            )


def test_check_tracker_type_rejects_invalid(tmp_path):
    db = str(tmp_path / "usage.db")
    bootstrap_usage_db(db)
    with sqlite3.connect(db) as conn:
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO usage_daily_summary"
                " (project_id, day_utc, tracker_type, model_name)"
                " VALUES (1, '2026-06-11', 'bogus', '')"
            )


def test_check_valid_direction_accepted(tmp_path):
    db = str(tmp_path / "usage.db")
    bootstrap_usage_db(db)
    with sqlite3.connect(db) as conn:
        conn.execute(
            "INSERT INTO usage_messages (project_id, direction, participant_role, created_at)"
            " VALUES (1, 'in', 'customer', '2026-06-11T00:00:00Z')"
        )


# ---------------------------------------------------------------------------
# call_outcome has NO CHECK constraint (enforced at Python boundary only)
# ---------------------------------------------------------------------------

def test_call_outcome_has_no_check_constraint(tmp_path):
    db = str(tmp_path / "usage.db")
    bootstrap_usage_db(db)
    with sqlite3.connect(db) as conn:
        # Any arbitrary string is accepted at the SQL level
        conn.execute(
            "INSERT INTO usage_llm_calls"
            " (project_id, model_name, prompt_tokens, completion_tokens,"
            "  call_outcome, created_at)"
            " VALUES (1, 'gpt-4o', 100, 50, 'future_outcome_not_yet_defined',"
            "  '2026-06-11T00:00:00Z')"
        )


# ---------------------------------------------------------------------------
# model_name sentinel: DEFAULT '' allows empty-string for non-LLM rows
# ---------------------------------------------------------------------------

def test_daily_summary_model_name_defaults_to_empty_string(tmp_path):
    db = str(tmp_path / "usage.db")
    bootstrap_usage_db(db)
    with sqlite3.connect(db) as conn:
        conn.execute(
            "INSERT INTO usage_daily_summary (project_id, day_utc, tracker_type)"
            " VALUES (1, '2026-06-11', 'messages')"
        )
        row = conn.execute(
            "SELECT model_name FROM usage_daily_summary WHERE project_id=1"
        ).fetchone()
    assert row[0] == ""


# ---------------------------------------------------------------------------
# Parent directory auto-creation
# ---------------------------------------------------------------------------

def test_bootstrap_creates_parent_directory(tmp_path):
    db = str(tmp_path / "nested" / "sub" / "usage.db")
    bootstrap_usage_db(db)
    assert _EXPECTED_TABLES.issubset(_tables(db))

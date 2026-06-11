"""Idempotent bootstrap for the usage telemetry DB (Story 14.01).

Creates ``.data/semantaix_usage.db`` with five tables (WAL mode, all indexes)
on first call; subsequent calls are a no-op.

``model_name`` in ``usage_daily_summary`` uses an empty-string sentinel
(``DEFAULT ''``) for non-LLM tracker rows so the composite PRIMARY KEY
``(project_id, day_utc, tracker_type, model_name)`` behaves deterministically.
SQLite treats NULL as non-equal in PK comparisons, which would allow duplicate
rows; the empty-string sentinel avoids that without nullable PK columns.

``call_outcome`` in ``usage_llm_calls`` deliberately has no CHECK constraint —
new outcome values can be added via code changes only (no schema migration).
Validation is enforced at the Python level in ``UsageLlmCallRepository``.
"""

from __future__ import annotations

import logging
import sqlite3
from pathlib import Path

_LOG = logging.getLogger(__name__)


def _connect(db_path: str) -> sqlite3.Connection:
    path = Path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    return conn


def _table_count(conn: sqlite3.Connection) -> int:
    return conn.execute(
        "SELECT COUNT(*) FROM sqlite_master WHERE type='table'"
    ).fetchone()[0]


def bootstrap_usage_db(db_path: str) -> None:
    """Create all five usage tables + indexes in *db_path* (WAL mode).

    Safe to call on every container boot — every statement uses
    ``IF NOT EXISTS`` so re-running is a no-op.
    """
    with _connect(db_path) as conn:
        conn.execute("PRAGMA journal_mode = WAL")
        before = _table_count(conn)

        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS usage_llm_calls (
                id INTEGER PRIMARY KEY,
                project_id INTEGER NOT NULL,
                model_name TEXT NOT NULL,
                prompt_tokens INTEGER NOT NULL,
                completion_tokens INTEGER NOT NULL,
                cost_usd REAL,
                call_outcome TEXT NOT NULL,
                trace_id TEXT,
                created_at TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS usage_llm_calls_project_created_idx
                ON usage_llm_calls (project_id, created_at)
            """
        )

        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS usage_messages (
                id INTEGER PRIMARY KEY,
                project_id INTEGER NOT NULL,
                direction TEXT NOT NULL CHECK(direction IN ('in','out')),
                participant_role TEXT NOT NULL
                    CHECK(participant_role IN ('customer','operator')),
                trace_id TEXT,
                created_at TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS usage_messages_project_created_idx
                ON usage_messages (project_id, created_at)
            """
        )

        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS usage_hitl_events (
                id INTEGER PRIMARY KEY,
                project_id INTEGER NOT NULL,
                event_type TEXT NOT NULL
                    CHECK(event_type IN ('created','assigned','replied','resolved')),
                ticket_id INTEGER NOT NULL,
                trace_id TEXT,
                created_at TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS usage_hitl_events_project_created_idx
                ON usage_hitl_events (project_id, created_at)
            """
        )

        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS usage_daily_summary (
                project_id INTEGER NOT NULL,
                day_utc TEXT NOT NULL,
                tracker_type TEXT NOT NULL
                    CHECK(tracker_type IN ('llm','messages','hitl')),
                model_name TEXT NOT NULL DEFAULT '',
                prompt_tokens_total INTEGER,
                completion_tokens_total INTEGER,
                cost_usd_total REAL,
                wasted_cost_usd REAL,
                call_count INTEGER,
                in_count INTEGER,
                out_count INTEGER,
                hitl_created_count INTEGER,
                hitl_assigned_count INTEGER,
                hitl_replied_count INTEGER,
                hitl_resolved_count INTEGER,
                PRIMARY KEY (project_id, day_utc, tracker_type, model_name)
            )
            """
        )
        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS usage_daily_summary_project_day_model_idx
                ON usage_daily_summary (project_id, day_utc, model_name)
            """
        )

        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS usage_incidents (
                id INTEGER PRIMARY KEY,
                project_id INTEGER NOT NULL,
                started_at TEXT NOT NULL,
                ended_at TEXT,
                breached_trackers TEXT NOT NULL,
                peak_pct REAL,
                total_excess_cost_usd REAL
            )
            """
        )
        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS usage_incidents_project_started_idx
                ON usage_incidents (project_id, started_at)
            """
        )

        after = _table_count(conn)

    _LOG.info("usage_db_bootstrapped", extra={"tables_created": after - before})

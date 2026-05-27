"""``sales_followup_queue`` table + repository (Story 12.08).

Story 12.01 owns the canonical sales DB schema; this module ships the
slice 12.08 needs — a queue row per pending T+1d nudge with statuses
``scheduled`` / ``sent`` / ``skipped_stale`` / ``cancelled_replied`` /
``cancelled_replaced``, an optional ``reason`` column, and the handful
of operations the api endpoints + scheduler job consume.

All times are stored as ISO-8601 UTC strings. Tz conversion happens at
the decision boundary (quiet-hours, nudge rendering) — never in the
repo. Sync ``sqlite3`` per project convention; the async callers (api
endpoints, scheduler job) dispatch via ``asyncio.to_thread``.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

STATUS_SCHEDULED = "scheduled"
STATUS_SENT = "sent"
STATUS_SKIPPED_STALE = "skipped_stale"
STATUS_CANCELLED_REPLIED = "cancelled_replied"
STATUS_CANCELLED_REPLACED = "cancelled_replaced"

REASON_PAST_INTENT_DATE = "past_intent_date"
REASON_TELEGRAM_SEND_FAILED = "telegram_send_failed"


@dataclass(frozen=True)
class FollowupRow:
    id: int
    chat_id: int
    project_id: int
    fire_at: datetime
    status: str
    reason: str | None
    created_at: datetime
    updated_at: datetime


def _connect(db_path: str) -> sqlite3.Connection:
    path = Path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path)
    connection.row_factory = sqlite3.Row
    return connection


def _format_utc(value: datetime) -> str:
    if value.tzinfo is None:
        raise ValueError("datetime must be tz-aware")
    return value.astimezone(UTC).isoformat()


def _parse_iso(value: str) -> datetime:
    return datetime.fromisoformat(value)


def init_schema(db_path: str) -> None:
    with _connect(db_path) as connection:
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS sales_followup_queue (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id INTEGER NOT NULL,
                project_id INTEGER NOT NULL,
                fire_at TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'scheduled',
                reason TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
        connection.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_followup_due
            ON sales_followup_queue (status, fire_at)
            """
        )
        connection.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_sales_followup_queue_chat
            ON sales_followup_queue (chat_id, status)
            """
        )


class FollowupQueueRepository:
    def __init__(self, *, db_path: str) -> None:
        self.db_path = db_path
        init_schema(self.db_path)

    def enqueue(
        self,
        *,
        chat_id: int,
        project_id: int,
        fire_at: datetime,
        now: datetime,
    ) -> int:
        """Insert a new ``scheduled`` row; cancel prior scheduled rows.

        The cancel of prior rows uses the ``cancelled_replaced`` status so
        the audit history (one row per bot turn) stays intact. Returns the
        new row id.
        """
        fire_at_iso = _format_utc(fire_at)
        now_iso = _format_utc(now)
        with _connect(self.db_path) as connection:
            connection.execute(
                """
                UPDATE sales_followup_queue
                   SET status = ?, updated_at = ?
                 WHERE chat_id = ? AND status = ?
                """,
                (STATUS_CANCELLED_REPLACED, now_iso, int(chat_id), STATUS_SCHEDULED),
            )
            cursor = connection.execute(
                """
                INSERT INTO sales_followup_queue (
                    chat_id, project_id, fire_at, status, reason,
                    created_at, updated_at
                )
                VALUES (?, ?, ?, ?, NULL, ?, ?)
                """,
                (
                    int(chat_id),
                    int(project_id),
                    fire_at_iso,
                    STATUS_SCHEDULED,
                    now_iso,
                    now_iso,
                ),
            )
            return int(cursor.lastrowid)

    def get(self, row_id: int) -> FollowupRow | None:
        with _connect(self.db_path) as connection:
            row = connection.execute(
                """
                SELECT id, chat_id, project_id, fire_at, status, reason,
                       created_at, updated_at
                  FROM sales_followup_queue
                 WHERE id = ?
                """,
                (int(row_id),),
            ).fetchone()
        return _row_to_followup(row) if row is not None else None

    def due(self, *, now: datetime, limit: int = 100) -> list[FollowupRow]:
        """Return ``scheduled`` rows whose ``fire_at <= now`` (UTC)."""
        now_iso = _format_utc(now)
        with _connect(self.db_path) as connection:
            rows = connection.execute(
                """
                SELECT id, chat_id, project_id, fire_at, status, reason,
                       created_at, updated_at
                  FROM sales_followup_queue
                 WHERE status = ? AND fire_at <= ?
                 ORDER BY fire_at
                 LIMIT ?
                """,
                (STATUS_SCHEDULED, now_iso, int(limit)),
            ).fetchall()
        return [_row_to_followup(row) for row in rows]

    def list_for_chat(self, chat_id: int) -> list[FollowupRow]:
        with _connect(self.db_path) as connection:
            rows = connection.execute(
                """
                SELECT id, chat_id, project_id, fire_at, status, reason,
                       created_at, updated_at
                  FROM sales_followup_queue
                 WHERE chat_id = ?
                 ORDER BY id
                """,
                (int(chat_id),),
            ).fetchall()
        return [_row_to_followup(row) for row in rows]

    def mark_sent(self, row_id: int, *, now: datetime) -> None:
        self._update_status(row_id, status=STATUS_SENT, reason=None, now=now)

    def mark_skipped_stale(
        self, row_id: int, *, reason: str, now: datetime
    ) -> None:
        self._update_status(
            row_id, status=STATUS_SKIPPED_STALE, reason=reason, now=now
        )

    def reschedule(
        self, row_id: int, *, new_fire_at: datetime, now: datetime
    ) -> None:
        fire_at_iso = _format_utc(new_fire_at)
        now_iso = _format_utc(now)
        with _connect(self.db_path) as connection:
            connection.execute(
                """
                UPDATE sales_followup_queue
                   SET fire_at = ?, updated_at = ?
                 WHERE id = ? AND status = ?
                """,
                (fire_at_iso, now_iso, int(row_id), STATUS_SCHEDULED),
            )

    def mark_cancelled_replied(self, chat_id: int, *, now: datetime) -> int:
        """Cancel every ``scheduled`` row for ``chat_id``; return count."""
        now_iso = _format_utc(now)
        with _connect(self.db_path) as connection:
            cursor = connection.execute(
                """
                UPDATE sales_followup_queue
                   SET status = ?, updated_at = ?
                 WHERE chat_id = ? AND status = ?
                """,
                (
                    STATUS_CANCELLED_REPLIED,
                    now_iso,
                    int(chat_id),
                    STATUS_SCHEDULED,
                ),
            )
            return int(cursor.rowcount)

    def _update_status(
        self,
        row_id: int,
        *,
        status: str,
        reason: str | None,
        now: datetime,
    ) -> None:
        now_iso = _format_utc(now)
        with _connect(self.db_path) as connection:
            connection.execute(
                """
                UPDATE sales_followup_queue
                   SET status = ?, reason = ?, updated_at = ?
                 WHERE id = ?
                """,
                (status, reason, now_iso, int(row_id)),
            )


def _row_to_followup(row: sqlite3.Row) -> FollowupRow:
    return FollowupRow(
        id=int(row["id"]),
        chat_id=int(row["chat_id"]),
        project_id=int(row["project_id"]),
        fire_at=_parse_iso(str(row["fire_at"])),
        status=str(row["status"]),
        reason=(str(row["reason"]) if row["reason"] is not None else None),
        created_at=_parse_iso(str(row["created_at"])),
        updated_at=_parse_iso(str(row["updated_at"])),
    )


__all__ = [
    "FollowupQueueRepository",
    "FollowupRow",
    "REASON_PAST_INTENT_DATE",
    "REASON_TELEGRAM_SEND_FAILED",
    "STATUS_CANCELLED_REPLACED",
    "STATUS_CANCELLED_REPLIED",
    "STATUS_SCHEDULED",
    "STATUS_SENT",
    "STATUS_SKIPPED_STALE",
    "init_schema",
]

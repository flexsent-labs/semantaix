"""SQLite-backed buffer for redelivered customer-message backlog.

When bot_gateway is offline (restart, deploy, crash) Telegram queues the
customer's messages and redelivers the whole backlog once the webhook
endpoint recovers. Answering each redelivered message independently would
spam the customer with N acks/escalations, so we buffer the burst here,
keyed by ``chat_id``, and a debounced flush answers only the latest message
(pulling earlier ones in as context when the latest is too thin).

This mirrors ``media_group_buffer.MediaGroupBuffer``: ``add()`` reports
whether it inserted the FIRST row for a chat (so the caller schedules the
flush exactly once), ``latest_received_at()`` powers the quiet-window
detection, and ``drain()`` atomically reads + deletes. Persistence — rather
than an in-memory dict — means a restart that overlaps the debounce window
does not lose the backlog.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path


def _connect(db_path: str) -> sqlite3.Connection:
    path = Path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path, isolation_level=None, timeout=10.0)
    connection.row_factory = sqlite3.Row
    return connection


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


def init_schema(db_path: str) -> None:
    with _connect(db_path) as connection:
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS offline_backlog_buffer (
                chat_id INTEGER NOT NULL,
                update_id INTEGER NOT NULL,
                source_message_id INTEGER NOT NULL,
                customer_username TEXT,
                text TEXT NOT NULL,
                message_date INTEGER,
                received_at TEXT NOT NULL,
                PRIMARY KEY (chat_id, update_id)
            )
            """
        )


@dataclass(frozen=True)
class BufferedMessage:
    chat_id: int
    update_id: int
    source_message_id: int
    customer_username: str | None
    text: str
    message_date: int | None
    received_at: str


class OfflineBacklogBuffer:
    def __init__(self, db_path: str) -> None:
        self.db_path = db_path
        init_schema(db_path)

    def add(
        self,
        *,
        chat_id: int,
        update_id: int,
        source_message_id: int,
        customer_username: str | None,
        text: str,
        message_date: int | None,
    ) -> bool:
        """Buffer one backlog message.

        Returns True iff this call inserted the FIRST row for the chat — the
        caller uses that to schedule the debounced flush exactly once.
        Duplicate (chat_id, update_id) inserts are ignored and return False
        (matches Telegram redelivery semantics).
        """
        received_at = _now_iso()
        with _connect(self.db_path) as connection:
            connection.execute("BEGIN IMMEDIATE")
            before = connection.execute(
                "SELECT COUNT(*) FROM offline_backlog_buffer WHERE chat_id = ?",
                (chat_id,),
            ).fetchone()[0]
            connection.execute(
                """
                INSERT OR IGNORE INTO offline_backlog_buffer
                    (chat_id, update_id, source_message_id, customer_username,
                     text, message_date, received_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    chat_id,
                    update_id,
                    source_message_id,
                    customer_username,
                    text,
                    message_date,
                    received_at,
                ),
            )
            connection.execute("COMMIT")
        return before == 0

    def latest_received_at(self, *, chat_id: int) -> datetime | None:
        """Return MAX(received_at) for the chat, or None when no rows exist
        (chat was never buffered or has already been drained).

        The flusher uses this to detect "the chat has been quiet for N
        seconds, safe to drain".
        """
        with _connect(self.db_path) as connection:
            row = connection.execute(
                """
                SELECT MAX(received_at) AS latest
                FROM offline_backlog_buffer
                WHERE chat_id = ?
                """,
                (chat_id,),
            ).fetchone()
        if row is None or row["latest"] is None:
            return None
        return datetime.fromisoformat(str(row["latest"]))

    def pending_chat_ids(self) -> list[int]:
        """Return every chat with buffered rows.

        Used by the startup recovery sweep: a restart loses the in-memory
        flush task but not the SQLite rows, so each pending chat needs a flush
        rescheduled or its latest message would be stranded.
        """
        with _connect(self.db_path) as connection:
            rows = connection.execute(
                "SELECT DISTINCT chat_id FROM offline_backlog_buffer"
            ).fetchall()
        return [int(row["chat_id"]) for row in rows]

    def drain(self, *, chat_id: int) -> list[BufferedMessage]:
        """Atomically read + delete all buffered messages for a chat.

        Returns [] if the chat has already been drained (concurrent winner
        path). Order matches arrival order via the ``update_id`` column, so
        the caller can treat the last element as the latest message.
        """
        with _connect(self.db_path) as connection:
            connection.execute("BEGIN IMMEDIATE")
            rows = connection.execute(
                """
                SELECT chat_id, update_id, source_message_id, customer_username,
                       text, message_date, received_at
                FROM offline_backlog_buffer
                WHERE chat_id = ?
                ORDER BY update_id ASC
                """,
                (chat_id,),
            ).fetchall()
            if rows:
                connection.execute(
                    "DELETE FROM offline_backlog_buffer WHERE chat_id = ?",
                    (chat_id,),
                )
            connection.execute("COMMIT")
        return [_row_to_buffered(row) for row in rows]


def _row_to_buffered(row: sqlite3.Row) -> BufferedMessage:
    raw_date = row["message_date"]
    return BufferedMessage(
        chat_id=int(row["chat_id"]),
        update_id=int(row["update_id"]),
        source_message_id=int(row["source_message_id"]),
        customer_username=(
            str(row["customer_username"])
            if row["customer_username"] is not None
            else None
        ),
        text=str(row["text"]),
        message_date=int(raw_date) if raw_date is not None else None,
        received_at=str(row["received_at"]),
    )

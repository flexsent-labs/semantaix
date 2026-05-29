"""SQLite-backed outbox of customer messages awaiting a confirmed forward.

The live customer path returns ``200 OK`` to Telegram *before* the message is
forwarded to the api (the forward runs as a fire-and-forget background task so
the webhook never blocks on the answer pipeline). Once Telegram has the 200 it
never redelivers that update, so a restart — or a silent forward failure — in
the window between the ack and the forward would strand the message: persisted
in the transcript, never answered, and invisible to the
``offline_backlog_buffer`` recovery (which only ever holds *redelivered* stale
webhooks).

This outbox closes that gap. ``mark_pending`` records a message the instant it
is accepted; the forward background task calls ``clear`` only on a confirmed
success. Anything still pending after a restart is replayed by the startup
recovery sweep. Persistence — rather than an in-memory set — is what lets a
restart mid-forward recover the message instead of losing it.
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
            CREATE TABLE IF NOT EXISTS pending_forward_outbox (
                chat_id INTEGER NOT NULL,
                update_id INTEGER NOT NULL,
                source_message_id INTEGER NOT NULL,
                customer_username TEXT,
                text TEXT NOT NULL,
                trace_id TEXT NOT NULL,
                received_at TEXT NOT NULL,
                PRIMARY KEY (chat_id, update_id)
            )
            """
        )


@dataclass(frozen=True)
class PendingForward:
    chat_id: int
    update_id: int
    source_message_id: int
    customer_username: str | None
    text: str
    trace_id: str
    received_at: str


class PendingForwardOutbox:
    def __init__(self, db_path: str) -> None:
        self.db_path = db_path
        init_schema(db_path)

    def mark_pending(
        self,
        *,
        chat_id: int,
        update_id: int,
        source_message_id: int,
        customer_username: str | None,
        text: str,
        trace_id: str,
    ) -> None:
        """Record that a customer message has been accepted but not yet
        confirmed-forwarded.

        Duplicate ``(chat_id, update_id)`` marks are ignored (first write wins),
        mirroring Telegram redelivery dedup; in practice the upstream
        source-message dedup already drops duplicates before this is reached.
        """
        with _connect(self.db_path) as connection:
            connection.execute(
                """
                INSERT OR IGNORE INTO pending_forward_outbox
                    (chat_id, update_id, source_message_id, customer_username,
                     text, trace_id, received_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    chat_id,
                    update_id,
                    source_message_id,
                    customer_username,
                    text,
                    trace_id,
                    _now_iso(),
                ),
            )

    def pending_chat_ids(self) -> list[int]:
        """Return every chat that still has an unconfirmed forward.

        Used by the startup recovery sweep to know which chats to replay.
        """
        with _connect(self.db_path) as connection:
            rows = connection.execute(
                "SELECT DISTINCT chat_id FROM pending_forward_outbox"
            ).fetchall()
        return [int(row["chat_id"]) for row in rows]

    def peek_chat(self, *, chat_id: int) -> list[PendingForward]:
        """Read (without deleting) all pending rows for a chat, oldest first.

        Non-destructive on purpose: the recovery replay must keep the rows so a
        re-forward that fails again is retried on the next restart. Rows are
        removed only by ``clear``/``clear_chat`` after a confirmed forward.
        """
        with _connect(self.db_path) as connection:
            rows = connection.execute(
                """
                SELECT chat_id, update_id, source_message_id, customer_username,
                       text, trace_id, received_at
                FROM pending_forward_outbox
                WHERE chat_id = ?
                ORDER BY update_id ASC
                """,
                (chat_id,),
            ).fetchall()
        return [_row_to_pending(row) for row in rows]

    def clear(self, *, chat_id: int, update_id: int) -> None:
        """Drop one row after its forward succeeded (live single-message path)."""
        with _connect(self.db_path) as connection:
            connection.execute(
                """
                DELETE FROM pending_forward_outbox
                WHERE chat_id = ? AND update_id = ?
                """,
                (chat_id, update_id),
            )

    def clear_chat(self, *, chat_id: int) -> None:
        """Drop all rows for a chat after a collapsed-to-latest recovery forward.

        The recovery answers only the latest pending message per chat (preceding
        ones become inline context when the latest is thin), so on success the
        whole chat's backlog is considered handled.
        """
        with _connect(self.db_path) as connection:
            connection.execute(
                "DELETE FROM pending_forward_outbox WHERE chat_id = ?",
                (chat_id,),
            )


def _row_to_pending(row: sqlite3.Row) -> PendingForward:
    return PendingForward(
        chat_id=int(row["chat_id"]),
        update_id=int(row["update_id"]),
        source_message_id=int(row["source_message_id"]),
        customer_username=(
            str(row["customer_username"])
            if row["customer_username"] is not None
            else None
        ),
        text=str(row["text"]),
        trace_id=str(row["trace_id"]),
        received_at=str(row["received_at"]),
    )

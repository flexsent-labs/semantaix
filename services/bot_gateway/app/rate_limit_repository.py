"""Story 12.103 — per-customer inbound rate limiting.

Tracks message counts per ``chat_id`` in a fixed-window counter. Each window
starts when the first message arrives after the previous window expired. If a
``chat_id`` sends more than ``max_messages`` in ``window_seconds``, subsequent
messages are rejected until the window rolls over.

The store is a dedicated SQLite file separate from the transcript and other
operational stores, mirroring the ``WebhookUpdateClaimRepository`` pattern.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime
from pathlib import Path


def _connect(db_path: str) -> sqlite3.Connection:
    path = Path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path)
    connection.row_factory = sqlite3.Row
    return connection


def _init_schema(db_path: str) -> None:
    with _connect(db_path) as connection:
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS inbound_request_counts (
                chat_id INTEGER PRIMARY KEY,
                window_start_iso TEXT NOT NULL,
                message_count INTEGER NOT NULL
            )
            """
        )


class InboundRateLimitRepository:
    def __init__(self, *, db_path: str) -> None:
        self.db_path = db_path
        _init_schema(db_path)

    def check_and_record(
        self,
        *,
        chat_id: int,
        now: datetime,
        max_messages: int,
        window_seconds: int,
    ) -> bool:
        """Check whether the message is within the rate limit and record it.

        Returns ``True`` (allowed) when the message is within the window budget.
        Returns ``False`` (rejected) when the budget is exhausted; does NOT
        increment the count on rejection so the window boundary remains correct.

        The ``now`` parameter is injected so callers can control the clock in
        tests without relying on ``datetime.now()``.
        """
        now_iso = now.isoformat()
        # Re-init so a re-assigned db_path (per-test isolation) has the table.
        _init_schema(self.db_path)
        with _connect(self.db_path) as connection:
            row = connection.execute(
                "SELECT window_start_iso, message_count FROM inbound_request_counts "
                "WHERE chat_id = ?",
                (chat_id,),
            ).fetchone()

            if row is None:
                # First ever message from this chat_id.
                connection.execute(
                    "INSERT INTO inbound_request_counts "
                    "(chat_id, window_start_iso, message_count) VALUES (?, ?, 1)",
                    (chat_id, now_iso),
                )
                return True

            window_start = datetime.fromisoformat(row["window_start_iso"])
            count = row["message_count"]
            elapsed = (now - window_start).total_seconds()

            if elapsed > window_seconds:
                # Window expired — reset.
                connection.execute(
                    "INSERT OR REPLACE INTO inbound_request_counts "
                    "(chat_id, window_start_iso, message_count) VALUES (?, ?, 1)",
                    (chat_id, now_iso),
                )
                return True

            if count >= max_messages:
                # Budget exhausted — reject without incrementing.
                return False

            # Within budget — increment.
            connection.execute(
                "UPDATE inbound_request_counts SET message_count = ? WHERE chat_id = ?",
                (count + 1, chat_id),
            )
            return True

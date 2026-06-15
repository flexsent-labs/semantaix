from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path


def _connect(db_path: str) -> sqlite3.Connection:
    path = Path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path)
    connection.row_factory = sqlite3.Row
    return connection


def _now() -> str:
    return datetime.now(UTC).isoformat()


def init_schema(db_path: str) -> None:
    with _connect(db_path) as connection:
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS hitl_tickets (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                conversation_ref TEXT NOT NULL,
                reason TEXT NOT NULL,
                status TEXT NOT NULL,
                operator_username TEXT,
                target_chat_id INTEGER,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                resolved_at TEXT
            )
            """
        )
        columns = [
            row[1]
            for row in connection.execute("PRAGMA table_info(hitl_tickets)").fetchall()
        ]
        if "target_chat_id" not in columns:
            connection.execute("ALTER TABLE hitl_tickets ADD COLUMN target_chat_id INTEGER")
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS hitl_runtime_config (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                updated_by TEXT NOT NULL
            )
            """
        )


@dataclass(frozen=True)
class HitlTicket:
    id: int
    conversation_ref: str
    reason: str
    status: str
    operator_username: str | None
    target_chat_id: int | None
    created_at: str
    updated_at: str
    resolved_at: str | None


class HitlTicketRepository:
    def __init__(self, db_path: str) -> None:
        self.db_path = db_path
        init_schema(db_path)

    def create(
        self,
        *,
        conversation_ref: str,
        reason: str,
        target_chat_id: int | None = None,
    ) -> HitlTicket:
        init_schema(self.db_path)
        now = _now()
        with _connect(self.db_path) as connection:
            cursor = connection.execute(
                """
                INSERT INTO hitl_tickets (
                    conversation_ref, reason, status, operator_username,
                    target_chat_id, created_at, updated_at, resolved_at
                )
                VALUES (?, ?, 'open', NULL, ?, ?, ?, NULL)
                """,
                (conversation_ref, reason, target_chat_id, now, now),
            )
            row = connection.execute(
                """
                SELECT id, conversation_ref, reason, status, operator_username, target_chat_id,
                       created_at, updated_at, resolved_at
                FROM hitl_tickets
                WHERE id = ?
                """,
                (int(cursor.lastrowid),),
            ).fetchone()
            assert row is not None
            return self._row_to_ticket(row)

    def assign(self, *, ticket_id: int, operator_username: str) -> HitlTicket:
        init_schema(self.db_path)
        with _connect(self.db_path) as connection:
            connection.execute(
                """
                UPDATE hitl_tickets
                SET operator_username = ?, status = 'assigned', updated_at = ?
                WHERE id = ?
                """,
                (operator_username, _now(), ticket_id),
            )
            row = connection.execute(
                """
                SELECT id, conversation_ref, reason, status, operator_username, target_chat_id,
                       created_at, updated_at, resolved_at
                FROM hitl_tickets
                WHERE id = ?
                """,
                (ticket_id,),
            ).fetchone()
            assert row is not None
            return self._row_to_ticket(row)

    def resolve(self, *, ticket_id: int) -> HitlTicket:
        init_schema(self.db_path)
        now = _now()
        with _connect(self.db_path) as connection:
            connection.execute(
                """
                UPDATE hitl_tickets
                SET status = 'resolved', resolved_at = ?, updated_at = ?
                WHERE id = ?
                """,
                (now, now, ticket_id),
            )
            row = connection.execute(
                """
                SELECT id, conversation_ref, reason, status, operator_username, target_chat_id,
                       created_at, updated_at, resolved_at
                FROM hitl_tickets
                WHERE id = ?
                """,
                (ticket_id,),
            ).fetchone()
            assert row is not None
            return self._row_to_ticket(row)

    def find_active_for_chat(self, target_chat_id: int) -> HitlTicket | None:
        """Return the most recent active (open or assigned) ticket for a chat.

        Used to coalesce rapid customer follow-up questions onto a single
        HITL ticket so the operator does not see N parallel notifications
        for the same conversation.
        """
        init_schema(self.db_path)
        with _connect(self.db_path) as connection:
            row = connection.execute(
                """
                SELECT id, conversation_ref, reason, status, operator_username, target_chat_id,
                       created_at, updated_at, resolved_at
                FROM hitl_tickets
                WHERE target_chat_id = ?
                  AND status IN ('open', 'assigned')
                ORDER BY id DESC
                LIMIT 1
                """,
                (target_chat_id,),
            ).fetchone()
            if row is None:
                return None
            return self._row_to_ticket(row)

    def list_active_for_operator(self, operator_username: str) -> list[HitlTicket]:
        """Return assigned tickets for an operator, newest first.

        Used by the bot gateway to disambiguate operator replies when no
        ticket reference is present in the quoted message.
        """
        init_schema(self.db_path)
        with _connect(self.db_path) as connection:
            rows = connection.execute(
                """
                SELECT id, conversation_ref, reason, status, operator_username, target_chat_id,
                       created_at, updated_at, resolved_at
                FROM hitl_tickets
                WHERE operator_username = ?
                  AND status = 'assigned'
                ORDER BY id DESC
                """,
                (operator_username,),
            ).fetchall()
            return [self._row_to_ticket(row) for row in rows]

    def list_all(self) -> list[HitlTicket]:
        init_schema(self.db_path)
        with _connect(self.db_path) as connection:
            rows = connection.execute(
                """
                SELECT id, conversation_ref, reason, status, operator_username, target_chat_id,
                       created_at, updated_at, resolved_at
                FROM hitl_tickets
                ORDER BY id DESC
                """
            ).fetchall()
            return [self._row_to_ticket(row) for row in rows]

    def get(self, ticket_id: int) -> HitlTicket:
        init_schema(self.db_path)
        with _connect(self.db_path) as connection:
            row = connection.execute(
                """
                SELECT id, conversation_ref, reason, status, operator_username, target_chat_id,
                       created_at, updated_at, resolved_at
                FROM hitl_tickets
                WHERE id = ?
                """,
                (ticket_id,),
            ).fetchone()
            assert row is not None
            return self._row_to_ticket(row)

    def latest_for_chat(self, chat_id: int) -> HitlTicket | None:
        """Return the most recent ticket whose target chat matches.

        Used by Epic 10 story 10.06 to scope RAG retrieval by the
        operator-on-record for a conversation. The lookup is best-effort:
        deployments without HITL tickets simply get `None`.
        """
        init_schema(self.db_path)
        with _connect(self.db_path) as connection:
            row = connection.execute(
                """
                SELECT id, conversation_ref, reason, status, operator_username,
                       target_chat_id, created_at, updated_at, resolved_at
                FROM hitl_tickets
                WHERE target_chat_id = ?
                ORDER BY id DESC
                LIMIT 1
                """,
                (chat_id,),
            ).fetchone()
        return self._row_to_ticket(row) if row is not None else None

    def set_runtime_config(self, *, key: str, value: str, updated_by: str) -> None:
        init_schema(self.db_path)
        with _connect(self.db_path) as connection:
            connection.execute(
                """
                INSERT INTO hitl_runtime_config (key, value, updated_at, updated_by)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(key) DO UPDATE SET
                    value = excluded.value,
                    updated_at = excluded.updated_at,
                    updated_by = excluded.updated_by
                """,
                (key, value, _now(), updated_by),
            )

    def get_runtime_config(self, key: str) -> str | None:
        init_schema(self.db_path)
        with _connect(self.db_path) as connection:
            row = connection.execute(
                "SELECT value FROM hitl_runtime_config WHERE key = ?",
                (key,),
            ).fetchone()
            if row is None:
                return None
            return str(row["value"])

    def delete_runtime_config(self, key: str) -> None:
        """Delete a runtime-config row by key (no-op if absent)."""
        init_schema(self.db_path)
        with _connect(self.db_path) as connection:
            connection.execute(
                "DELETE FROM hitl_runtime_config WHERE key = ?",
                (key,),
            )

    def get_bot_persona(
        self, *, default_first_name: str, default_last_name: str
    ) -> tuple[str, str]:
        """Read persona overrides with settings fallback.

        Returns ``(first_name, last_name)``. Used by the LLM system prompt
        and the startup Telegram identity sync so they share one source of
        truth. A stored empty string (operator cleared the surname) is
        preserved — only an absent key falls back to the settings default.
        """
        first = self.get_runtime_config("bot_persona_first_name")
        last = self.get_runtime_config("bot_persona_last_name")
        return (
            first if first is not None else default_first_name,
            last if last is not None else default_last_name,
        )

    @staticmethod
    def _row_to_ticket(row: sqlite3.Row) -> HitlTicket:
        return HitlTicket(
            id=int(row["id"]),
            conversation_ref=str(row["conversation_ref"]),
            reason=str(row["reason"]),
            status=str(row["status"]),
            operator_username=str(row["operator_username"]) if row["operator_username"] else None,
            target_chat_id=(
                int(row["target_chat_id"]) if row["target_chat_id"] is not None else None
            ),
            created_at=str(row["created_at"]),
            updated_at=str(row["updated_at"]),
            resolved_at=str(row["resolved_at"]) if row["resolved_at"] else None,
        )

"""Epic 16.06 — per-operator Telegram auth phase and session metadata."""

from __future__ import annotations

import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path

_STALE_PHASES = ("qr_pending", "2fa_pending")


def _connect(db_path: str) -> sqlite3.Connection:
    path = Path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(db_path)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA journal_mode=WAL")
    return connection


def init_schema(db_path: str) -> None:
    with _connect(db_path) as connection:
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS operator_telegram_auth (
                operator_id INTEGER PRIMARY KEY,
                phase TEXT NOT NULL DEFAULT 'idle',
                session_path TEXT NOT NULL,
                linked_username TEXT,
                customer_channel_active INTEGER NOT NULL DEFAULT 0,
                started_at REAL,
                updated_at REAL NOT NULL
            )
            """
        )


@dataclass(frozen=True)
class OperatorTelegramAuth:
    operator_id: int
    phase: str
    session_path: str
    linked_username: str | None
    customer_channel_active: bool
    started_at: float | None
    updated_at: float


class OperatorTelegramAuthRepository:
    def __init__(self, db_path: str) -> None:
        self.db_path = db_path
        init_schema(db_path)

    def get(self, operator_id: int) -> OperatorTelegramAuth | None:
        init_schema(self.db_path)
        with _connect(self.db_path) as connection:
            row = connection.execute(
                """
                SELECT operator_id, phase, session_path, linked_username,
                       customer_channel_active, started_at, updated_at
                FROM operator_telegram_auth
                WHERE operator_id = ?
                """,
                (operator_id,),
            ).fetchone()
        if row is None:
            return None
        return _row_to_auth(row)

    def get_phase(self, operator_id: int) -> str:
        record = self.get(operator_id)
        if record is None:
            return "idle"
        return record.phase

    def upsert(
        self,
        *,
        operator_id: int,
        phase: str,
        session_path: str,
        linked_username: str | None = None,
        customer_channel_active: bool = False,
    ) -> None:
        init_schema(self.db_path)
        now = time.time()
        with _connect(self.db_path) as connection:
            connection.execute(
                """
                INSERT INTO operator_telegram_auth (
                    operator_id, phase, session_path, linked_username,
                    customer_channel_active, started_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(operator_id) DO UPDATE SET
                    phase = excluded.phase,
                    session_path = excluded.session_path,
                    linked_username = COALESCE(excluded.linked_username, linked_username),
                    customer_channel_active = excluded.customer_channel_active,
                    updated_at = excluded.updated_at
                """,
                (
                    operator_id,
                    phase,
                    session_path,
                    linked_username,
                    1 if customer_channel_active else 0,
                    now,
                    now,
                ),
            )

    def set_phase(self, operator_id: int, phase: str) -> None:
        record = self.get(operator_id)
        if record is None:
            raise KeyError(f"operator {operator_id} not registered")
        self.upsert(
            operator_id=operator_id,
            phase=phase,
            session_path=record.session_path,
            linked_username=record.linked_username,
            customer_channel_active=record.customer_channel_active,
        )

    def set_linked_username(self, operator_id: int, linked_username: str) -> None:
        record = self.get(operator_id)
        if record is None:
            raise KeyError(f"operator {operator_id} not registered")
        self.upsert(
            operator_id=operator_id,
            phase=record.phase,
            session_path=record.session_path,
            linked_username=linked_username,
            customer_channel_active=record.customer_channel_active,
        )

    def set_customer_channel_active(self, operator_id: int, active: bool) -> None:
        record = self.get(operator_id)
        if record is None:
            raise KeyError(f"operator {operator_id} not registered")
        self.upsert(
            operator_id=operator_id,
            phase=record.phase,
            session_path=record.session_path,
            linked_username=record.linked_username,
            customer_channel_active=active,
        )

    def clear_stale_on_startup(self) -> list[int]:
        init_schema(self.db_path)
        cleared: list[int] = []
        with _connect(self.db_path) as connection:
            rows = connection.execute(
                """
                SELECT operator_id, phase FROM operator_telegram_auth
                WHERE phase IN (?, ?)
                """,
                _STALE_PHASES,
            ).fetchall()
            for row in rows:
                cleared.append(int(row["operator_id"]))
            if cleared:
                connection.execute(
                    """
                    UPDATE operator_telegram_auth
                    SET phase = 'idle', updated_at = ?
                    WHERE phase IN (?, ?)
                    """,
                    (time.time(), *_STALE_PHASES),
                )
        return cleared


def _row_to_auth(row: sqlite3.Row) -> OperatorTelegramAuth:
    return OperatorTelegramAuth(
        operator_id=int(row["operator_id"]),
        phase=str(row["phase"]),
        session_path=str(row["session_path"]),
        linked_username=(
            str(row["linked_username"]) if row["linked_username"] is not None else None
        ),
        customer_channel_active=bool(row["customer_channel_active"]),
        started_at=float(row["started_at"]) if row["started_at"] is not None else None,
        updated_at=float(row["updated_at"]),
    )

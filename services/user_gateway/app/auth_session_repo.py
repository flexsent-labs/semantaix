"""Epic 15.02 — singleton auth phase tracking for legacy user session."""

from __future__ import annotations

import sqlite3
import time
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
            CREATE TABLE IF NOT EXISTS auth_session (
                id         INTEGER PRIMARY KEY CHECK (id = 1),
                phase      TEXT NOT NULL DEFAULT 'idle',
                started_at REAL,
                updated_at REAL NOT NULL
            )
            """
        )


class AuthSessionRepository:
    """Singleton row auth phase store for the legacy user_gateway session."""

    def __init__(self, db_path: str) -> None:
        self.db_path = db_path
        init_schema(db_path)

    def get_phase(self) -> str:
        init_schema(self.db_path)
        with _connect(self.db_path) as connection:
            row = connection.execute(
                "SELECT phase FROM auth_session WHERE id = 1"
            ).fetchone()
        if row is None:
            return "idle"
        return str(row["phase"])

    def set_phase(self, phase: str) -> None:
        init_schema(self.db_path)
        now = time.time()
        with _connect(self.db_path) as connection:
            connection.execute(
                """
                INSERT INTO auth_session (id, phase, started_at, updated_at)
                VALUES (1, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    phase = excluded.phase,
                    updated_at = excluded.updated_at
                """,
                (phase, now, now),
            )

    def clear_stale_on_startup(self) -> str:
        init_schema(self.db_path)
        current = self.get_phase()
        if current not in _STALE_PHASES:
            return current
        with _connect(self.db_path) as connection:
            connection.execute(
                "UPDATE auth_session SET phase = 'idle', updated_at = ? WHERE id = 1",
                (time.time(),),
            )
        return current

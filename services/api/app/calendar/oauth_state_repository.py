"""Single-use OAuth CSRF state for the calendar-connect flow (story 11.01).

The plaintext ``state`` (``secrets.token_urlsafe``) goes to Google; only its
sha256 is stored, with an ``expires_at``. ``consume`` is atomic single-use:
an unconsumed, unexpired hash is marked consumed and returned; anything else
(unknown / replayed / expired) raises ``InvalidOAuthState``. ``now`` is
injected into ``create``/``consume`` so TTL-edge branches are test-reachable.
Sync ``sqlite3``; callers dispatch via ``asyncio.to_thread``.
"""

from __future__ import annotations

import hashlib
import secrets
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

_STATE_NBYTES = 32


class InvalidOAuthState(Exception):
    """Raised when an OAuth state is unknown, expired, or already consumed."""


@dataclass(frozen=True)
class PendingState:
    project_id: int
    operator: str
    created_at: str
    expires_at: str


def _connect(db_path: str) -> sqlite3.Connection:
    path = Path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path)
    connection.row_factory = sqlite3.Row
    return connection


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


class CalendarOAuthStateRepository:
    def __init__(self, *, db_path: str) -> None:
        self.db_path = db_path
        self.init_schema()

    def init_schema(self) -> None:
        with _connect(self.db_path) as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS calendar_oauth_pending_state (
                    state_hash TEXT PRIMARY KEY,
                    project_id INTEGER,
                    operator TEXT,
                    created_at TEXT,
                    expires_at TEXT,
                    consumed_at TEXT
                )
                """
            )

    def create(
        self,
        *,
        project_id: int,
        operator: str,
        ttl_seconds: int,
        now: datetime,
    ) -> str:
        state = secrets.token_urlsafe(_STATE_NBYTES)
        state_hash = _sha256(state)
        created_at = now.isoformat()
        expires_at = (now + timedelta(seconds=ttl_seconds)).isoformat()
        with _connect(self.db_path) as connection:
            connection.execute(
                """
                INSERT INTO calendar_oauth_pending_state
                    (state_hash, project_id, operator,
                     created_at, expires_at, consumed_at)
                VALUES (?, ?, ?, ?, ?, NULL)
                """,
                (state_hash, project_id, operator, created_at, expires_at),
            )
        return state

    def consume(self, state: str, *, now: datetime) -> PendingState:
        state_hash = _sha256(state)
        now_iso = now.isoformat()
        with _connect(self.db_path) as connection:
            # Atomic single-use: claim the row in ONE conditional UPDATE so two
            # concurrent callbacks presenting the same state cannot both pass an
            # "unconsumed" check before either writes (TOCTOU). The WHERE clause
            # enforces unconsumed + unexpired; rowcount == 1 means we won the claim.
            claimed = connection.execute(
                """
                UPDATE calendar_oauth_pending_state
                SET consumed_at = ?
                WHERE state_hash = ?
                  AND consumed_at IS NULL
                  AND expires_at > ?
                """,
                (now_iso, state_hash, now_iso),
            )
            if claimed.rowcount == 1:
                row = connection.execute(
                    """
                    SELECT project_id, operator, created_at, expires_at
                    FROM calendar_oauth_pending_state
                    WHERE state_hash = ?
                    """,
                    (state_hash,),
                ).fetchone()
                return PendingState(
                    project_id=int(row["project_id"]),
                    operator=str(row["operator"]),
                    created_at=str(row["created_at"]),
                    expires_at=str(row["expires_at"]),
                )
            # Claim failed — disambiguate the miss for a precise error.
            row = connection.execute(
                """
                SELECT expires_at, consumed_at
                FROM calendar_oauth_pending_state
                WHERE state_hash = ?
                """,
                (state_hash,),
            ).fetchone()
            if row is None:
                raise InvalidOAuthState("unknown_state")
            if row["consumed_at"] is not None:
                raise InvalidOAuthState("state_already_consumed")
            raise InvalidOAuthState("state_expired")

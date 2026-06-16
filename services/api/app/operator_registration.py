from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from services.api.app.operators import Operator, OperatorUsernameConflict

_REGISTRATION_COOLDOWN = timedelta(hours=24)


class RegistrationPendingConflict(Exception):
    """Raised when a pending request already exists for the username."""


class RegistrationCooldownActive(Exception):
    """Raised when a rejected username is still inside the cooldown window."""


class RegistrationNotFound(Exception):
    """Raised when the registration request id does not exist."""


class RegistrationAlreadyProcessed(Exception):
    """Raised when approve/reject is attempted on a non-pending request."""


def _connect(db_path: str) -> sqlite3.Connection:
    path = Path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(db_path)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA journal_mode=WAL")
    return connection


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _normalize_username(username: str) -> str:
    candidate = username.strip()
    if not candidate.startswith("@"):
        candidate = f"@{candidate}"
    return candidate


@dataclass(frozen=True)
class RegistrationRequest:
    id: int
    username: str
    chat_id: int
    display_name: str | None
    status: str
    project_id: int | None
    created_at: str
    reviewed_at: str | None
    reviewed_by: str | None
    rejection_cooldown_until: str | None


class OperatorRegistrationRepository:
    def __init__(self, db_path: str) -> None:
        self.db_path = db_path
        self.init_schema()

    def init_schema(self) -> None:
        with _connect(self.db_path) as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS operator_registration_requests (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    username TEXT NOT NULL,
                    chat_id INTEGER NOT NULL,
                    display_name TEXT,
                    status TEXT NOT NULL DEFAULT 'pending',
                    project_id INTEGER,
                    created_at TEXT NOT NULL,
                    reviewed_at TEXT,
                    reviewed_by TEXT,
                    rejection_cooldown_until TEXT
                )
                """
            )
            connection.execute(
                """
                CREATE UNIQUE INDEX IF NOT EXISTS idx_op_reg_pending_username
                ON operator_registration_requests(username)
                WHERE status = 'pending'
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS operator_onboarding_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    operator_id INTEGER NOT NULL,
                    event_type TEXT NOT NULL,
                    created_at TEXT NOT NULL
                )
                """
            )
            connection.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_onboarding_events_operator
                ON operator_onboarding_events(operator_id, created_at)
                """
            )

    def create_request(
        self,
        *,
        username: str,
        chat_id: int,
        display_name: str | None = None,
    ) -> RegistrationRequest:
        normalized = _normalize_username(username)
        now = _now()
        with _connect(self.db_path) as connection:
            latest = connection.execute(
                """
                SELECT status, rejection_cooldown_until
                FROM operator_registration_requests
                WHERE username = ?
                ORDER BY id DESC
                LIMIT 1
                """,
                (normalized,),
            ).fetchone()
            if latest is not None:
                if str(latest["status"]) == "pending":
                    raise RegistrationPendingConflict(normalized)
                cooldown_until = latest["rejection_cooldown_until"]
                if (
                    str(latest["status"]) == "rejected"
                    and cooldown_until is not None
                    and datetime.fromisoformat(str(cooldown_until)) > datetime.now(UTC)
                ):
                    raise RegistrationCooldownActive(normalized)
            try:
                cursor = connection.execute(
                    """
                    INSERT INTO operator_registration_requests (
                        username, chat_id, display_name, status, created_at
                    )
                    VALUES (?, ?, ?, 'pending', ?)
                    """,
                    (normalized, chat_id, display_name, now),
                )
            except sqlite3.IntegrityError as exc:
                raise RegistrationPendingConflict(normalized) from exc
            request_id = int(cursor.lastrowid)
        fetched = self.get(request_id)
        assert fetched is not None
        return fetched

    def list_by_status(self, status: str) -> list[RegistrationRequest]:
        with _connect(self.db_path) as connection:
            rows = connection.execute(
                _SELECT_REQUEST_SQL + " WHERE status = ? ORDER BY id ASC",
                (status,),
            ).fetchall()
        return [_row_to_request(row) for row in rows]

    def get(self, request_id: int) -> RegistrationRequest | None:
        with _connect(self.db_path) as connection:
            row = connection.execute(
                _SELECT_REQUEST_SQL + " WHERE id = ?",
                (request_id,),
            ).fetchone()
        return _row_to_request(row) if row is not None else None

    def approve(
        self,
        *,
        request_id: int,
        reviewed_by: str,
        project_id: int,
        operator_repository,
    ) -> Operator:
        now = _now()
        with _connect(self.db_path) as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                _SELECT_REQUEST_SQL + " WHERE id = ?",
                (request_id,),
            ).fetchone()
            if row is None:
                connection.execute("ROLLBACK")
                raise RegistrationNotFound(request_id)
            if str(row["status"]) != "pending":
                connection.execute("ROLLBACK")
                raise RegistrationAlreadyProcessed(request_id)
            username = str(row["username"])
            chat_id = int(row["chat_id"])
            display_name = row["display_name"]
            display_value = str(display_name) if display_name is not None else None
            existing = connection.execute(
                "SELECT 1 FROM operators WHERE username = ? LIMIT 1",
                (username,),
            ).fetchone()
            if existing is not None:
                connection.execute("ROLLBACK")
                raise OperatorUsernameConflict(username)
            try:
                cursor = connection.execute(
                    """
                    INSERT INTO operators (
                        username, chat_id, project_id, display_name,
                        is_active, created_at, updated_at
                    )
                    VALUES (?, ?, ?, ?, 1, ?, ?)
                    """,
                    (username, chat_id, project_id, display_value, now, now),
                )
                operator_id = int(cursor.lastrowid)
                connection.execute(
                    """
                    UPDATE operator_registration_requests
                    SET status = 'approved',
                        project_id = ?,
                        reviewed_at = ?,
                        reviewed_by = ?
                    WHERE id = ?
                    """,
                    (project_id, now, reviewed_by, request_id),
                )
                connection.execute(
                    """
                    INSERT INTO operator_onboarding_events (
                        operator_id, event_type, created_at
                    )
                    VALUES (?, 'approved', ?)
                    """,
                    (operator_id, now),
                )
                connection.execute("COMMIT")
            except sqlite3.IntegrityError as exc:
                connection.execute("ROLLBACK")
                raise OperatorUsernameConflict(username) from exc
        operator = operator_repository.find_by_username(username)
        assert operator is not None
        return operator

    def reject(self, *, request_id: int, reviewed_by: str) -> RegistrationRequest:
        now = _now()
        cooldown_until = (datetime.now(UTC) + _REGISTRATION_COOLDOWN).isoformat()
        with _connect(self.db_path) as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                _SELECT_REQUEST_SQL + " WHERE id = ?",
                (request_id,),
            ).fetchone()
            if row is None:
                connection.execute("ROLLBACK")
                raise RegistrationNotFound(request_id)
            if str(row["status"]) != "pending":
                connection.execute("ROLLBACK")
                raise RegistrationAlreadyProcessed(request_id)
            connection.execute(
                """
                UPDATE operator_registration_requests
                SET status = 'rejected',
                    reviewed_at = ?,
                    reviewed_by = ?,
                    rejection_cooldown_until = ?
                WHERE id = ?
                """,
                (now, reviewed_by, cooldown_until, request_id),
            )
            connection.execute("COMMIT")
        updated = self.get(request_id)
        assert updated is not None
        return updated

    def record_onboarding_event(self, *, operator_id: int, event_type: str) -> None:
        with _connect(self.db_path) as connection:
            connection.execute(
                """
                INSERT INTO operator_onboarding_events (
                    operator_id, event_type, created_at
                )
                VALUES (?, ?, ?)
                """,
                (operator_id, event_type, _now()),
            )

    def list_onboarding_events(self, *, operator_id: int) -> list[tuple[str, str]]:
        with _connect(self.db_path) as connection:
            rows = connection.execute(
                """
                SELECT event_type, created_at
                FROM operator_onboarding_events
                WHERE operator_id = ?
                ORDER BY id ASC
                """,
                (operator_id,),
            ).fetchall()
        return [(str(row["event_type"]), str(row["created_at"])) for row in rows]


_SELECT_REQUEST_SQL = (
    "SELECT id, username, chat_id, display_name, status, project_id, "
    "created_at, reviewed_at, reviewed_by, rejection_cooldown_until "
    "FROM operator_registration_requests"
)


def _row_to_request(row: sqlite3.Row) -> RegistrationRequest:
    project_raw = row["project_id"]
    display_raw = row["display_name"]
    reviewed_at = row["reviewed_at"]
    reviewed_by = row["reviewed_by"]
    cooldown = row["rejection_cooldown_until"]
    return RegistrationRequest(
        id=int(row["id"]),
        username=str(row["username"]),
        chat_id=int(row["chat_id"]),
        display_name=str(display_raw) if display_raw is not None else None,
        status=str(row["status"]),
        project_id=int(project_raw) if project_raw is not None else None,
        created_at=str(row["created_at"]),
        reviewed_at=str(reviewed_at) if reviewed_at is not None else None,
        reviewed_by=str(reviewed_by) if reviewed_by is not None else None,
        rejection_cooldown_until=str(cooldown) if cooldown is not None else None,
    )

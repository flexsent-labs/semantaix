"""``services`` catalog table + repository (Story 12.02).

Lives in ``.data/semantaix_sales.db`` alongside :class:`StateRepository` and
the followup queue. Story 12.02 ships the minimal surface needed by the
``/service_add`` / ``/service_list`` / ``/service_remove`` Telegram commands:

* :meth:`ServicesRepository.add` — creates an active row keyed on
  ``(project_id, lower(name))``; raises :class:`ServiceAlreadyExists` when
  another active row already owns the name.
* :meth:`ServicesRepository.list_active` — returns active rows for a project
  in id order (matches the operator's "/service_list" reading order).
* :meth:`ServicesRepository.list_for_project` — duck-type compatible with the
  :class:`SalesPersonaAnswerer`'s ``_ServicesRepo`` protocol so it can be
  injected as a drop-in replacement for the Story 12.03 stub.
* :meth:`ServicesRepository.get_by_name` — case-insensitive lookup used by
  the concept-ask path in the sales answerer.
* :meth:`ServicesRepository.count_active` — used by the always-on activation
  gate (cheapest possible query).
* :meth:`ServicesRepository.soft_delete` — flips ``is_active=0`` so the row
  stops appearing in the catalog without losing audit history.

The unique-name guarantee is enforced with a partial unique index over
``(project_id, lower(name)) WHERE is_active = 1`` so an operator can readd
a name after a soft delete.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path


class ServiceAlreadyExists(Exception):
    """Raised by :meth:`ServicesRepository.add` on duplicate ``(project_id, name)``.

    ``args[0]`` is the offending name (lower-cased) so callers can echo it
    verbatim in operator-facing DMs.
    """


class ServiceNotFound(Exception):
    """Raised by :meth:`ServicesRepository.soft_delete` when no active row matches."""


@dataclass(frozen=True)
class Service:
    id: int
    project_id: int
    name: str
    description_md: str | None
    tags: list[str]
    is_active: bool
    created_at: str
    updated_at: str


def _unicode_lower(value):
    """SQLite UDF replacement for ``lower`` that handles non-ASCII (Cyrillic).

    SQLite's built-in ``lower`` is ASCII-only; without this UDF the partial
    unique index would treat ``каньонинг`` and ``КАНЬОНИНГ`` as distinct.
    The ``name`` column is ``NOT NULL`` so NULL never reaches this UDF.
    """
    return str(value).casefold()


def _connect(db_path: str) -> sqlite3.Connection:
    path = Path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path)
    connection.row_factory = sqlite3.Row
    connection.create_function("lower", 1, _unicode_lower, deterministic=True)
    return connection


def _format_utc(now: datetime) -> str:
    if now.tzinfo is None:
        raise ValueError("now must be tz-aware")
    return now.astimezone(UTC).isoformat()


def init_schema(db_path: str) -> None:
    with _connect(db_path) as connection:
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS services (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                project_id INTEGER NOT NULL,
                name TEXT NOT NULL,
                description_md TEXT,
                tags_json TEXT NOT NULL DEFAULT '[]',
                is_active INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
        connection.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS services_active_name_unique "
            "ON services(project_id, lower(name)) WHERE is_active = 1"
        )
        connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_services_project "
            "ON services(project_id, is_active)"
        )


def _row_to_service(row: sqlite3.Row) -> Service:
    tags_raw = row["tags_json"]
    return Service(
        id=int(row["id"]),
        project_id=int(row["project_id"]),
        name=str(row["name"]),
        description_md=row["description_md"],
        tags=list(json.loads(tags_raw)) if tags_raw else [],
        is_active=bool(row["is_active"]),
        created_at=str(row["created_at"]),
        updated_at=str(row["updated_at"]),
    )


_SELECT_SQL = (
    "SELECT id, project_id, name, description_md, tags_json, is_active, "
    "created_at, updated_at FROM services"
)


class ServicesRepository:
    def __init__(self, *, db_path: str) -> None:
        self.db_path = db_path
        init_schema(self.db_path)

    def add(
        self,
        *,
        project_id: int,
        name: str,
        now: datetime,
        description_md: str | None = None,
        tags: list[str] | None = None,
    ) -> int:
        clean_name = (name or "").strip()
        if not clean_name:
            raise ValueError("name must be non-blank")
        timestamp = _format_utc(now)
        tags_payload = json.dumps(
            list(tags) if tags is not None else [],
            ensure_ascii=False,
            sort_keys=True,
        )
        try:
            with _connect(self.db_path) as connection:
                cursor = connection.execute(
                    """
                    INSERT INTO services
                        (project_id, name, description_md, tags_json,
                         is_active, created_at, updated_at)
                    VALUES (?, ?, ?, ?, 1, ?, ?)
                    """,
                    (
                        int(project_id),
                        clean_name,
                        description_md,
                        tags_payload,
                        timestamp,
                        timestamp,
                    ),
                )
                return int(cursor.lastrowid)
        except sqlite3.IntegrityError as exc:
            raise ServiceAlreadyExists(clean_name.casefold()) from exc

    def list_active(self, *, project_id: int) -> list[Service]:
        with _connect(self.db_path) as connection:
            rows = connection.execute(
                _SELECT_SQL
                + " WHERE project_id = ? AND is_active = 1 ORDER BY id ASC",
                (int(project_id),),
            ).fetchall()
        return [_row_to_service(row) for row in rows]

    def list_for_project(self, *, project_id: int) -> list[Service]:
        """Alias matching the SalesPersonaAnswerer's ``_ServicesRepo`` protocol."""
        return self.list_active(project_id=project_id)

    def get_by_name(
        self, *, project_id: int, name: str
    ) -> Service | None:
        with _connect(self.db_path) as connection:
            row = connection.execute(
                _SELECT_SQL
                + (
                    " WHERE project_id = ? AND is_active = 1 "
                    "AND lower(name) = lower(?)"
                ),
                (int(project_id), name),
            ).fetchone()
        return _row_to_service(row) if row is not None else None

    def find_by_name(self, *, project_id: int, name: str) -> Service | None:
        """Alias for :meth:`get_by_name` matching the Story 12.01 surface."""
        return self.get_by_name(project_id=project_id, name=name)

    def get(self, *, service_id: int) -> Service | None:
        with _connect(self.db_path) as connection:
            row = connection.execute(
                _SELECT_SQL + " WHERE id = ?",
                (int(service_id),),
            ).fetchone()
        return _row_to_service(row) if row is not None else None

    def count_active(self, *, project_id: int) -> int:
        with _connect(self.db_path) as connection:
            row = connection.execute(
                "SELECT COUNT(*) AS n FROM services "
                "WHERE project_id = ? AND is_active = 1",
                (int(project_id),),
            ).fetchone()
        return int(row["n"]) if row is not None else 0

    def soft_delete(self, *, service_id: int) -> None:
        now = datetime.now(UTC).isoformat()
        with _connect(self.db_path) as connection:
            cursor = connection.execute(
                "UPDATE services SET is_active = 0, updated_at = ? "
                "WHERE id = ? AND is_active = 1",
                (now, int(service_id)),
            )
            if cursor.rowcount == 0:
                raise ServiceNotFound(f"service_not_found:{service_id}")

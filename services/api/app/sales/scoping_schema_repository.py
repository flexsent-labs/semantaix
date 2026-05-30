"""`ScopingSchemaRepository` — per-service / per-project anketas (Story 12.16).

Owns the ``scoping_schemas`` table in ``.data/semantaix_sales.db``: a JSON
serialisation of a `ScopingSchema` keyed by ``(project_id, service_id)``.
``service_id == PROJECT_DEFAULT_SERVICE_ID`` (0) is the project-wide default the
funnel uses when no single service is resolved.

`resolve_scoping_schema` implements the lean resolution the operator chose
("Авто + дефолт проекта"): a single active service → its schema; otherwise the
project default; otherwise the built-in transfer fallback. No conversation-order
change and no extra state column — the service is resolved lazily each turn.

Sync ``sqlite3`` per the project-context rule; callers dispatch via
``asyncio.to_thread`` from the async answerer.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol

from services.api.app.sales.scoping_schema import ScopingField, ScopingSchema

PROJECT_DEFAULT_SERVICE_ID = 0


class _ServicesRepo(Protocol):
    def list_for_project(self, *, project_id: int) -> list[Any]: ...


def _connect(db_path: str) -> sqlite3.Connection:
    path = Path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path)
    connection.row_factory = sqlite3.Row
    return connection


def init_schema(db_path: str) -> None:
    with _connect(db_path) as connection:
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS scoping_schemas (
                project_id INTEGER NOT NULL,
                service_id INTEGER NOT NULL,
                schema_json TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                updated_by TEXT NOT NULL,
                PRIMARY KEY (project_id, service_id)
            )
            """
        )


def _serialize(schema: ScopingSchema) -> str:
    return json.dumps(
        [
            {
                "key": field.key,
                "question": field.question,
                "kind": field.kind,
                "required": field.required,
            }
            for field in schema.fields
        ],
        ensure_ascii=False,
    )


def _deserialize(schema_json: str) -> ScopingSchema:
    rows = json.loads(schema_json)
    return ScopingSchema(
        tuple(
            ScopingField(
                key=row["key"],
                question=row["question"],
                kind=row["kind"],
                required=row["required"],
            )
            for row in rows
        )
    )


class ScopingSchemaRepository:
    def __init__(self, *, db_path: str) -> None:
        self.db_path = db_path
        init_schema(self.db_path)

    def set_schema(
        self,
        *,
        project_id: int,
        service_id: int,
        schema: ScopingSchema,
        updated_by: str,
    ) -> None:
        now = datetime.now(UTC).isoformat()
        with _connect(self.db_path) as connection:
            connection.execute(
                """
                INSERT INTO scoping_schemas
                    (project_id, service_id, schema_json, updated_at, updated_by)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(project_id, service_id) DO UPDATE SET
                    schema_json = excluded.schema_json,
                    updated_at = excluded.updated_at,
                    updated_by = excluded.updated_by
                """,
                (project_id, service_id, _serialize(schema), now, updated_by),
            )

    def get_schema(
        self, *, project_id: int, service_id: int
    ) -> ScopingSchema | None:
        with _connect(self.db_path) as connection:
            row = connection.execute(
                """
                SELECT schema_json FROM scoping_schemas
                WHERE project_id = ? AND service_id = ?
                """,
                (project_id, service_id),
            ).fetchone()
        if row is None:
            return None
        return _deserialize(row["schema_json"])


def _single_active_service_id(
    services_repo: _ServicesRepo, project_id: int
) -> int | None:
    """The id of the only active service, or ``None`` when 0 or 2+ exist."""
    services = services_repo.list_for_project(project_id=project_id)
    return services[0].id if len(services) == 1 else None


def resolve_scoping_schema(
    repo: ScopingSchemaRepository,
    services_repo: _ServicesRepo,
    *,
    project_id: int,
    transfer_fallback: ScopingSchema,
) -> ScopingSchema:
    """Active anketa for a project (Story 12.16, "Авто + дефолт проекта")."""
    service_id = _single_active_service_id(services_repo, project_id)
    if service_id is not None:
        service_schema = repo.get_schema(
            project_id=project_id, service_id=service_id
        )
        if service_schema is not None:
            return service_schema
    project_default = repo.get_schema(
        project_id=project_id, service_id=PROJECT_DEFAULT_SERVICE_ID
    )
    if project_default is not None:
        return project_default
    return transfer_fallback

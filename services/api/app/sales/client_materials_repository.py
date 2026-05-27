"""``client_materials`` table + repository (Story 12.01 surface).

Owns the ``client_materials`` table in ``.data/semantaix_sales.db``: row
shape (kind, local_path, byte_size, duration_seconds, caption, tags_json,
source_operator_file_id, telegram_file_id, is_active) plus the operations
the analyzer (12.05b) and dispatcher (12.05) consume: ``add``,
``list_active``, ``get``, ``pick_by_tags``, ``update_telegram_file_id``,
``soft_delete``.

All public methods are keyword-only; the ``now`` datetime is injected for
deterministic tests and stored verbatim. JSON columns use the
``ensure_ascii=False, sort_keys=True`` discipline shared with the other sales
repos so diffs stay stable. ``pick_by_tags`` ranks by overlap count
(most-specific first) since the dataset is small — no JSON1 / FTS5 needed.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path


@dataclass(frozen=True)
class ClientMaterial:
    id: int
    project_id: int
    kind: str
    telegram_file_id: str | None
    local_path: str
    byte_size: int
    duration_seconds: int | None
    caption: str | None
    tags: list[str]
    source_operator_file_id: str | None
    is_active: bool
    created_at: str
    updated_at: str


def _connect(db_path: str) -> sqlite3.Connection:
    path = Path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path)
    connection.row_factory = sqlite3.Row
    return connection


def _format_utc(value: datetime) -> str:
    if value.tzinfo is None:
        raise ValueError("now must be tz-aware")
    return value.astimezone(UTC).isoformat()


def init_schema(db_path: str) -> None:
    with _connect(db_path) as connection:
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS client_materials (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                project_id INTEGER NOT NULL,
                kind TEXT NOT NULL,
                telegram_file_id TEXT,
                local_path TEXT NOT NULL,
                byte_size INTEGER NOT NULL,
                duration_seconds INTEGER,
                caption TEXT,
                tags_json TEXT NOT NULL DEFAULT '[]',
                source_operator_file_id TEXT,
                is_active INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
        connection.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_client_materials_project
                ON client_materials (project_id, is_active)
            """
        )


class ClientMaterialsRepository:
    def __init__(self, *, db_path: str) -> None:
        self.db_path = db_path
        init_schema(self.db_path)

    def add(
        self,
        *,
        project_id: int,
        kind: str,
        local_path: str,
        byte_size: int,
        now: datetime,
        duration_seconds: int | None = None,
        caption: str | None = None,
        tags: list[str] | None = None,
        telegram_file_id: str | None = None,
        source_operator_file_id: str | None = None,
    ) -> int:
        timestamp = _format_utc(now)
        tags_payload = json.dumps(
            list(tags) if tags is not None else [],
            ensure_ascii=False,
            sort_keys=True,
        )
        with _connect(self.db_path) as connection:
            cursor = connection.execute(
                """
                INSERT INTO client_materials (
                    project_id,
                    kind,
                    telegram_file_id,
                    local_path,
                    byte_size,
                    duration_seconds,
                    caption,
                    tags_json,
                    source_operator_file_id,
                    is_active,
                    created_at,
                    updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?)
                """,
                (
                    int(project_id),
                    kind,
                    telegram_file_id,
                    local_path,
                    int(byte_size),
                    duration_seconds,
                    caption,
                    tags_payload,
                    source_operator_file_id,
                    timestamp,
                    timestamp,
                ),
            )
            return int(cursor.lastrowid)

    def list_active(self, *, project_id: int) -> list[ClientMaterial]:
        with _connect(self.db_path) as connection:
            rows = connection.execute(
                _SELECT_SQL
                + " WHERE project_id = ? AND is_active = 1 ORDER BY id ASC",
                (int(project_id),),
            ).fetchall()
        return [_row_to_material(row) for row in rows]

    def get(self, *, material_id: int) -> ClientMaterial | None:
        with _connect(self.db_path) as connection:
            row = connection.execute(
                _SELECT_SQL + " WHERE id = ?",
                (int(material_id),),
            ).fetchone()
        return _row_to_material(row) if row is not None else None

    def pick_by_tags(
        self, *, project_id: int, tags: list[str]
    ) -> list[ClientMaterial]:
        """Return active rows whose tags overlap ``tags``, most-specific first.

        Ranking is by overlap count (intersection size) descending, with id
        ascending as a stable tiebreaker. Zero-overlap rows are excluded.
        """
        requested = {str(tag) for tag in tags}
        if not requested:
            return []
        materials = self.list_active(project_id=project_id)
        ranked: list[tuple[int, int, ClientMaterial]] = []
        for material in materials:
            overlap = len(requested.intersection(material.tags))
            if overlap == 0:
                continue
            ranked.append((-overlap, material.id, material))
        ranked.sort()
        return [item[2] for item in ranked]

    def update_telegram_file_id(
        self, *, material_id: int, telegram_file_id: str
    ) -> None:
        with _connect(self.db_path) as connection:
            connection.execute(
                "UPDATE client_materials "
                "SET telegram_file_id = ? WHERE id = ?",
                (telegram_file_id, int(material_id)),
            )

    def soft_delete(self, *, material_id: int) -> None:
        now_iso = datetime.now(UTC).isoformat()
        with _connect(self.db_path) as connection:
            connection.execute(
                "UPDATE client_materials "
                "SET is_active = 0, updated_at = ? WHERE id = ?",
                (now_iso, int(material_id)),
            )


_SELECT_SQL = (
    "SELECT id, project_id, kind, telegram_file_id, local_path, byte_size, "
    "duration_seconds, caption, tags_json, source_operator_file_id, "
    "is_active, created_at, updated_at FROM client_materials"
)


def _row_to_material(row: sqlite3.Row) -> ClientMaterial:
    tags_raw = row["tags_json"]
    return ClientMaterial(
        id=int(row["id"]),
        project_id=int(row["project_id"]),
        kind=str(row["kind"]),
        telegram_file_id=(
            str(row["telegram_file_id"]) if row["telegram_file_id"] is not None else None
        ),
        local_path=str(row["local_path"]),
        byte_size=int(row["byte_size"]),
        duration_seconds=(
            int(row["duration_seconds"]) if row["duration_seconds"] is not None else None
        ),
        caption=(str(row["caption"]) if row["caption"] is not None else None),
        tags=list(json.loads(tags_raw)) if tags_raw else [],
        source_operator_file_id=(
            str(row["source_operator_file_id"])
            if row["source_operator_file_id"] is not None
            else None
        ),
        is_active=bool(row["is_active"]),
        created_at=str(row["created_at"]),
        updated_at=str(row["updated_at"]),
    )

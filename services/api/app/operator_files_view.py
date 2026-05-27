"""Read-only view joining operator_files (bot_gateway DB) to
knowledge_moderation_candidates (api DB). Both DBs live in the shared .data
volume; we use SQLite ATTACH to do a cross-file join in a single query.

Access rules (enforced in SQL WHERE clauses):

- Admin: no scope; can see every file including confidential.
- Operator: only files where ``op.username`` matches the viewer. Since
  confidential filtering for non-uploaders is moot when the viewer can only
  see their own rows, no extra clause is needed.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path

_SNIPPET_HALF_WINDOW = 40


@dataclass(frozen=True)
class FileSummary:
    short_id: str
    source_file_name: str | None
    source_file_type: str | None
    uploaded_by: str
    uploaded_at: str
    file_size_bytes: int | None
    is_confidential: bool
    kb_ingest_status: str
    kb_inserted_chunks: int | None
    has_extracted_text: bool
    extracted_chars: int


@dataclass(frozen=True)
class FileDetail:
    short_id: str
    source_file_name: str | None
    source_file_type: str | None
    uploaded_by: str
    uploaded_at: str
    file_size_bytes: int | None
    is_confidential: bool
    kb_ingest_status: str
    kb_inserted_chunks: int | None
    candidate_text: str | None


@dataclass(frozen=True)
class FileSearchHit:
    short_id: str
    source_file_name: str | None
    uploaded_by: str
    uploaded_at: str
    snippet: str


@dataclass(frozen=True)
class KbFileMaterialView:
    """Server-internal projection used by the KB-upload analyzer (12.05b).

    Exposes the file metadata + extracted text the analyzer needs to judge
    whether a KB-uploaded file is suitable as customer-facing material.
    No viewer scope: the analyzer runs as a server-internal background
    job after a successful KB ingest.
    """

    short_id: str
    mime_type: str | None
    file_extension: str
    byte_size: int
    local_path: str | None
    is_confidential: bool
    extracted_text: str | None
    project_id: int | None


def _open(operator_files_db_path: str, knowledge_db_path: str) -> sqlite3.Connection:
    if not Path(operator_files_db_path).exists():
        # Caller still needs a connection to query; create-empty by opening RW once,
        # then immediately close — but simpler: just open RW so the empty schema
        # gets created upstream. For RO callers, a missing DB is a programmer error.
        raise FileNotFoundError(operator_files_db_path)
    uri = f"file:{operator_files_db_path}?mode=ro"
    connection = sqlite3.connect(uri, uri=True)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA query_only = 1")
    connection.execute("PRAGMA busy_timeout = 2000")
    connection.execute(
        f"ATTACH DATABASE 'file:{knowledge_db_path}?mode=ro' AS kdb",
    )
    return connection


def _scope_sql(*, viewer_username: str, viewer_role: str) -> tuple[str, list]:
    if viewer_role == "admin":
        return "1=1", []
    return "op.username = ?", [viewer_username]


class OperatorFilesView:
    def __init__(
        self, *, operator_files_db_path: str, knowledge_db_path: str
    ) -> None:
        self.operator_files_db_path = operator_files_db_path
        self.knowledge_db_path = knowledge_db_path

    def list_files(
        self,
        *,
        viewer_username: str,
        viewer_role: str,
        limit: int,
        owner_filter: str | None = None,
    ) -> list[FileSummary]:
        scope_sql, scope_params = _scope_sql(
            viewer_username=viewer_username, viewer_role=viewer_role
        )
        owner_sql = ""
        owner_params: list = []
        if owner_filter and viewer_role == "admin":
            owner_sql = " AND op.username = ?"
            owner_params = [owner_filter]
        sql = f"""
            SELECT op.short_id, op.username, op.source_file_name,
                   op.source_file_type, op.file_size_bytes, op.is_confidential,
                   op.kb_ingest_status, op.kb_inserted_chunks, op.created_at,
                   kmc.candidate_text AS candidate_text
            FROM operator_files op
            LEFT JOIN kdb.knowledge_moderation_candidates kmc
                ON kmc.id = op.knowledge_candidate_id
            WHERE {scope_sql}{owner_sql}
            ORDER BY op.created_at DESC, op.rowid DESC
            LIMIT ?
        """
        with _open(self.operator_files_db_path, self.knowledge_db_path) as conn:
            rows = conn.execute(
                sql, [*scope_params, *owner_params, limit]
            ).fetchall()
        return [_row_to_summary(row) for row in rows]

    def get_file(
        self, *, short_id: str, viewer_username: str, viewer_role: str
    ) -> FileDetail | None:
        scope_sql, scope_params = _scope_sql(
            viewer_username=viewer_username, viewer_role=viewer_role
        )
        sql = f"""
            SELECT op.short_id, op.username, op.source_file_name,
                   op.source_file_type, op.file_size_bytes, op.is_confidential,
                   op.kb_ingest_status, op.kb_inserted_chunks, op.created_at,
                   kmc.candidate_text AS candidate_text
            FROM operator_files op
            LEFT JOIN kdb.knowledge_moderation_candidates kmc
                ON kmc.id = op.knowledge_candidate_id
            WHERE op.short_id = ? AND {scope_sql}
            LIMIT 1
        """
        with _open(self.operator_files_db_path, self.knowledge_db_path) as conn:
            row = conn.execute(sql, [short_id, *scope_params]).fetchone()
        if row is None:
            return None
        return _row_to_detail(row)

    def get_for_kb_material(
        self, *, short_id: str
    ) -> KbFileMaterialView | None:
        """Server-internal lookup used by the 12.05b KB-material analyzer.

        Returns ``None`` when the file is unknown. Joins to the knowledge
        moderation row to source ``extracted_text`` and ``project_id``;
        ``file_extension`` is derived from ``source_file_name`` (lowercased,
        no leading dot) and falls back to a normalized hint from
        ``source_file_type`` when the original name has no extension.
        """
        sql = """
            SELECT op.short_id, op.source_file_name, op.source_file_type,
                   op.mime_type, op.file_size_bytes, op.is_confidential,
                   op.stored_binary_path,
                   kmc.candidate_text AS candidate_text,
                   kmc.project_id AS project_id
            FROM operator_files op
            LEFT JOIN kdb.knowledge_moderation_candidates kmc
                ON kmc.id = op.knowledge_candidate_id
            WHERE op.short_id = ?
            LIMIT 1
        """
        with _open(self.operator_files_db_path, self.knowledge_db_path) as conn:
            row = conn.execute(sql, [short_id]).fetchone()
        if row is None:
            return None
        candidate_text = row["candidate_text"]
        source_file_name = row["source_file_name"]
        source_file_type = row["source_file_type"]
        return KbFileMaterialView(
            short_id=str(row["short_id"]),
            mime_type=(
                str(row["mime_type"]) if row["mime_type"] is not None else None
            ),
            file_extension=_resolve_file_extension(
                source_file_name=(
                    str(source_file_name)
                    if source_file_name is not None
                    else None
                ),
                source_file_type=(
                    str(source_file_type)
                    if source_file_type is not None
                    else None
                ),
            ),
            byte_size=(
                int(row["file_size_bytes"])
                if row["file_size_bytes"] is not None
                else 0
            ),
            local_path=(
                str(row["stored_binary_path"])
                if row["stored_binary_path"] is not None
                else None
            ),
            is_confidential=bool(row["is_confidential"]),
            extracted_text=(
                str(candidate_text) if candidate_text is not None else None
            ),
            project_id=(
                int(row["project_id"]) if row["project_id"] is not None else None
            ),
        )

    def search_files(
        self,
        *,
        query: str,
        viewer_username: str,
        viewer_role: str,
        limit: int = 10,
    ) -> list[FileSearchHit]:
        scope_sql, scope_params = _scope_sql(
            viewer_username=viewer_username, viewer_role=viewer_role
        )
        like = f"%{query}%"
        sql = f"""
            SELECT op.short_id, op.username, op.source_file_name,
                   op.created_at, kmc.candidate_text AS candidate_text
            FROM operator_files op
            JOIN kdb.knowledge_moderation_candidates kmc
                ON kmc.id = op.knowledge_candidate_id
            WHERE kmc.candidate_text LIKE ? AND {scope_sql}
            ORDER BY op.created_at DESC, op.rowid DESC
            LIMIT ?
        """
        with _open(self.operator_files_db_path, self.knowledge_db_path) as conn:
            rows = conn.execute(sql, [like, *scope_params, limit]).fetchall()
        return [_row_to_hit(row, query=query) for row in rows]


def _row_to_summary(row: sqlite3.Row) -> FileSummary:
    candidate_text = row["candidate_text"]
    chars = len(candidate_text) if candidate_text is not None else 0
    return FileSummary(
        short_id=str(row["short_id"]),
        source_file_name=(
            str(row["source_file_name"])
            if row["source_file_name"] is not None
            else None
        ),
        source_file_type=(
            str(row["source_file_type"])
            if row["source_file_type"] is not None
            else None
        ),
        uploaded_by=str(row["username"]),
        uploaded_at=str(row["created_at"]),
        file_size_bytes=(
            int(row["file_size_bytes"])
            if row["file_size_bytes"] is not None
            else None
        ),
        is_confidential=bool(row["is_confidential"]),
        kb_ingest_status=str(row["kb_ingest_status"]),
        kb_inserted_chunks=(
            int(row["kb_inserted_chunks"])
            if row["kb_inserted_chunks"] is not None
            else None
        ),
        has_extracted_text=candidate_text is not None and len(candidate_text) > 0,
        extracted_chars=chars,
    )


def _row_to_detail(row: sqlite3.Row) -> FileDetail:
    candidate_text = row["candidate_text"]
    return FileDetail(
        short_id=str(row["short_id"]),
        source_file_name=(
            str(row["source_file_name"])
            if row["source_file_name"] is not None
            else None
        ),
        source_file_type=(
            str(row["source_file_type"])
            if row["source_file_type"] is not None
            else None
        ),
        uploaded_by=str(row["username"]),
        uploaded_at=str(row["created_at"]),
        file_size_bytes=(
            int(row["file_size_bytes"])
            if row["file_size_bytes"] is not None
            else None
        ),
        is_confidential=bool(row["is_confidential"]),
        kb_ingest_status=str(row["kb_ingest_status"]),
        kb_inserted_chunks=(
            int(row["kb_inserted_chunks"])
            if row["kb_inserted_chunks"] is not None
            else None
        ),
        candidate_text=(
            str(candidate_text) if candidate_text is not None else None
        ),
    )


def _row_to_hit(row: sqlite3.Row, *, query: str) -> FileSearchHit:
    text = str(row["candidate_text"])
    snippet = _build_snippet(text=text, query=query)
    return FileSearchHit(
        short_id=str(row["short_id"]),
        source_file_name=(
            str(row["source_file_name"])
            if row["source_file_name"] is not None
            else None
        ),
        uploaded_by=str(row["username"]),
        uploaded_at=str(row["created_at"]),
        snippet=snippet,
    )


_SOURCE_TYPE_EXTENSION_FALLBACK: dict[str, str] = {
    "pdf": "pdf",
    "docx": "docx",
    "pptx": "pptx",
    "xlsx": "xlsx",
    "txt": "txt",
    "csv": "csv",
    "html": "html",
    "md": "md",
    "rtf": "rtf",
    "epub": "epub",
    "zip": "zip",
    "image": "jpg",
    "audio": "ogg",
    "video": "mp4",
    "inline_text": "txt",
}


def _resolve_file_extension(
    *, source_file_name: str | None, source_file_type: str | None
) -> str:
    if source_file_name and "." in source_file_name:
        suffix = source_file_name.rsplit(".", 1)[-1].lower().strip()
        if suffix:
            return suffix
    if source_file_type:
        normalized = source_file_type.lower().strip()
        return _SOURCE_TYPE_EXTENSION_FALLBACK.get(normalized, normalized)
    return "bin"


def _build_snippet(*, text: str, query: str) -> str:
    lowered = text.lower()
    idx = lowered.find(query.lower())
    if idx < 0:
        return text[: 2 * _SNIPPET_HALF_WINDOW].replace("\n", " ").strip()
    start = max(0, idx - _SNIPPET_HALF_WINDOW)
    end = min(len(text), idx + len(query) + _SNIPPET_HALF_WINDOW)
    raw = text[start:end].replace("\n", " ").strip()
    prefix = "…" if start > 0 else ""
    suffix = "…" if end < len(text) else ""
    return f"{prefix}{raw}{suffix}"

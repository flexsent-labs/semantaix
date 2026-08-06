from __future__ import annotations

import hashlib
import logging
import sqlite3
from dataclasses import dataclass
from pathlib import Path

from services.api.app.russian_text import (
    get_retrieval_stopwords,
    get_russian_normalizer,
)

logger = logging.getLogger(__name__)


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
            CREATE TABLE IF NOT EXISTS rag_chunks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                source_id TEXT NOT NULL,
                chunk_hash TEXT NOT NULL,
                chunk_text TEXT NOT NULL,
                UNIQUE(source_id, chunk_hash)
            )
            """
        )
        columns = {
            str(r["name"])
            for r in connection.execute("PRAGMA table_info(rag_chunks)").fetchall()
        }
        if "is_confidential" not in columns:
            connection.execute(
                "ALTER TABLE rag_chunks ADD COLUMN is_confidential INTEGER NOT NULL DEFAULT 0"
            )
        if "project_id" not in columns:
            connection.execute(
                "ALTER TABLE rag_chunks ADD COLUMN project_id INTEGER"
            )
        connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_rag_chunks_project "
            "ON rag_chunks(project_id)"
        )


def _tokenize(text: str) -> set[str]:
    # Pre-split on hyphens so compounds like "Багги-тур" produce ("багги", "тур")
    # on both query and chunk sides. razdel keeps them as one token otherwise,
    # which makes "багги тур" miss "багги-тур" entirely.
    flattened = text.replace("-", " ")
    return set(get_russian_normalizer().lemmas(flattened))


def split_into_chunks(text: str) -> list[str]:
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if not lines:
        return []
    return lines


@dataclass(frozen=True)
class RagChunk:
    id: int
    source_id: str
    chunk_text: str
    score: float
    is_confidential: bool = False
    project_id: int | None = None


class RagRepository:
    def __init__(self, db_path: str) -> None:
        self.db_path = db_path
        init_schema(db_path)

    def ingest(
        self,
        *,
        source_id: str,
        text: str,
        is_confidential: bool = False,
        project_id: int | None = None,
    ) -> int:
        init_schema(self.db_path)
        inserted = 0
        chunks = split_into_chunks(text)
        confidential_flag = 1 if is_confidential else 0
        with _connect(self.db_path) as connection:
            for chunk in chunks:
                digest = hashlib.sha256(chunk.encode("utf-8")).hexdigest()
                cursor = connection.execute(
                    """
                    INSERT OR IGNORE INTO rag_chunks
                        (source_id, chunk_hash, chunk_text,
                         is_confidential, project_id)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (
                        source_id,
                        digest,
                        chunk,
                        confidential_flag,
                        project_id,
                    ),
                )
                if cursor.rowcount > 0:
                    inserted += 1
        return inserted

    def update_project_id_for_source(
        self, *, source_id: str, project_id: int
    ) -> int:
        init_schema(self.db_path)
        with _connect(self.db_path) as connection:
            cursor = connection.execute(
                "UPDATE rag_chunks SET project_id = ? WHERE source_id = ?",
                (project_id, source_id),
            )
            return int(cursor.rowcount or 0)

    def list_for_catalog(
        self, *, project_id: int | None = None, limit: int = 8
    ) -> list[RagChunk]:
        """Return bounded public chunks for a whole-catalog question.

        A generic question such as ``«Какие услуги есть?»`` has no distinctive
        lemma to retrieve.  The catalog digest normally supplies the aggregate
        answer, but when that digest is unavailable the fallback must enumerate
        source chunks rather than run a zero-result keyword search.
        """
        init_schema(self.db_path)
        with _connect(self.db_path) as connection:
            if project_id is None:
                rows = connection.execute(
                    "SELECT id, source_id, chunk_text, is_confidential, "
                    "project_id FROM rag_chunks "
                    "WHERE is_confidential = 0 ORDER BY id LIMIT ?",
                    (max(0, int(limit)),),
                ).fetchall()
            else:
                rows = connection.execute(
                    "SELECT id, source_id, chunk_text, is_confidential, "
                    "project_id FROM rag_chunks "
                    "WHERE is_confidential = 0 "
                    "AND (project_id = ? OR project_id IS NULL) "
                    "ORDER BY id LIMIT ?",
                    (int(project_id), max(0, int(limit))),
                ).fetchall()
        return [
            RagChunk(
                id=int(row["id"]),
                source_id=str(row["source_id"]),
                chunk_text=str(row["chunk_text"]),
                score=1.0,
                is_confidential=False,
                project_id=(
                    int(row["project_id"])
                    if row["project_id"] is not None
                    else None
                ),
            )
            for row in rows
        ]

    def retrieve(
        self,
        *,
        query: str,
        limit: int = 3,
        project_id: int | None = None,
    ) -> list[RagChunk]:
        init_schema(self.db_path)
        query_tokens = _tokenize(query)
        if not query_tokens:
            logger.info(
                "rag_retrieve_empty_query",
                extra={
                    "query": query,
                    "project_id_filter": project_id,
                    "limit": limit,
                },
            )
            return []
        # Score over content lemmas only so intent/connector words ("хочу",
        # "поехать", "на") do not deflate the denominator for short natural-
        # language queries. Stopword-only queries fall back to the full token
        # set to avoid awarding a perfect score against any chunk by accident.
        stopwords_removed = sorted(query_tokens & get_retrieval_stopwords())
        content_tokens = query_tokens - get_retrieval_stopwords()
        scoring_tokens = content_tokens or query_tokens
        denominator = len(scoring_tokens)

        logger.info(
            "rag_retrieve_request",
            extra={
                "query": query,
                "query_lemmas_all": sorted(query_tokens),
                "query_lemmas_content": sorted(content_tokens),
                "stopwords_removed": stopwords_removed,
                "denominator": denominator,
                "project_id_filter": project_id,
                "limit": limit,
            },
        )

        with _connect(self.db_path) as connection:
            if project_id is None:
                rows = connection.execute(
                    "SELECT id, source_id, chunk_text, is_confidential, "
                    "project_id FROM rag_chunks"
                ).fetchall()
            else:
                rows = connection.execute(
                    "SELECT id, source_id, chunk_text, is_confidential, "
                    "project_id FROM rag_chunks "
                    "WHERE project_id = ? OR project_id IS NULL",
                    (project_id,),
                ).fetchall()

        scored: list[tuple[RagChunk, list[str]]] = []
        for row in rows:
            chunk_text = str(row["chunk_text"])
            chunk_tokens = _tokenize(chunk_text)
            matched = scoring_tokens.intersection(chunk_tokens)
            if not matched:
                continue
            score = len(matched) / max(denominator, 1)
            chunk_project_id = (
                int(row["project_id"]) if row["project_id"] is not None else None
            )
            scored.append(
                (
                    RagChunk(
                        id=int(row["id"]),
                        source_id=str(row["source_id"]),
                        chunk_text=chunk_text,
                        score=score,
                        is_confidential=bool(row["is_confidential"]),
                        project_id=chunk_project_id,
                    ),
                    sorted(matched),
                )
            )

        scored.sort(key=lambda item: item[0].score, reverse=True)
        top = scored[:limit]
        logger.info(
            "rag_retrieve_result",
            extra={
                "query": query,
                "project_id_filter": project_id,
                "total_rows_scanned": len(rows),
                "matched_count": len(scored),
                "returned_count": len(top),
                "top_score": top[0][0].score if top else None,
                "candidates": [
                    {
                        "id": chunk.id,
                        "source_id": chunk.source_id,
                        "project_id": chunk.project_id,
                        "is_confidential": chunk.is_confidential,
                        "score": chunk.score,
                        "chunk_text_snippet": chunk.chunk_text[:200],
                        "matched_lemmas": matched_lemmas,
                    }
                    for chunk, matched_lemmas in scored[:5]
                ],
            },
        )
        return [chunk for chunk, _ in top]

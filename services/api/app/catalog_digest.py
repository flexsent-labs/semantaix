"""Catalog digest: a compact, LLM-built list of a project's offerings.

Aggregate questions like "какие ещё услуги есть" need the model to know the
*whole* set of things the company offers, not the handful of lemma-overlapping
chunks `RagRepository.retrieve` returns. We solve that by maintaining a digest:
an LLM map-reduce summary of every (non-confidential) knowledge chunk in scope,
collapsed into a deduplicated offerings list.

The digest is rebuilt lazily — only when a catalog query arrives and the source
chunk set has changed since the stored digest was built (detected via
``revision_key``). That keeps it always-fresh without coupling to the five RAG
ingest call sites or slowing down approvals.
"""

from __future__ import annotations

import hashlib
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol

from services.api.app.project_prompts import ProjectPromptRepository, resolve_prompt

# Per-batch character budget for the map phase. Large KBs are summarized in
# batches and then reduced, so the build stays within model context limits
# regardless of how big the knowledge base grows.
_MAX_BATCH_CHARS = 6000
_NO_OFFERINGS = "NO_OFFERINGS"


def _connect(db_path: str) -> sqlite3.Connection:
    path = Path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path)
    connection.row_factory = sqlite3.Row
    return connection


def _project_key(project_id: int | None) -> str:
    return f"project:{project_id}" if project_id is not None else "global"


@dataclass(frozen=True)
class StoredDigest:
    digest_text: str
    revision_key: str


@dataclass(frozen=True)
class DigestSource:
    lines: list[str]
    revision_key: str


class _OfferingsSummarizer(Protocol):
    async def summarize_offerings(
        self,
        *,
        knowledge_text: str,
        system_prompt: str | None = None,
        project_id: int | None = None,
        trace_id: str | None = None,
    ) -> str: ...


class CatalogDigestRepository:
    """Owns the ``catalog_digests`` table and reads the source chunk set.

    Lives in the same SQLite file as ``rag_chunks`` so the source scan and the
    digest cache share one DB.
    """

    def __init__(self, db_path: str) -> None:
        self.db_path = db_path
        self.init_schema()

    def init_schema(self) -> None:
        with _connect(self.db_path) as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS catalog_digests (
                    project_key  TEXT PRIMARY KEY,
                    digest_text  TEXT NOT NULL,
                    revision_key TEXT NOT NULL,
                    updated_at   TEXT NOT NULL
                )
                """
            )

    def read_source(self, *, project_id: int | None) -> DigestSource:
        """Return the in-scope non-confidential chunk texts + a revision key.

        Scope mirrors ``RagRepository.retrieve``: a project sees its own chunks
        plus global (``project_id IS NULL``) chunks; ``project_id=None`` sees
        the whole store. Confidential chunks are excluded so nothing
        confidential is ever enumerated to a customer.
        """
        self.init_schema()
        with _connect(self.db_path) as connection:
            if project_id is None:
                rows = connection.execute(
                    "SELECT chunk_hash, chunk_text FROM rag_chunks "
                    "WHERE is_confidential = 0 ORDER BY id"
                ).fetchall()
            else:
                rows = connection.execute(
                    "SELECT chunk_hash, chunk_text FROM rag_chunks "
                    "WHERE is_confidential = 0 "
                    "AND (project_id = ? OR project_id IS NULL) ORDER BY id",
                    (project_id,),
                ).fetchall()
        lines = [str(row["chunk_text"]) for row in rows]
        fingerprint = "\n".join(sorted(str(row["chunk_hash"]) for row in rows))
        revision_key = hashlib.sha256(fingerprint.encode("utf-8")).hexdigest()
        return DigestSource(lines=lines, revision_key=revision_key)

    def get(self, *, project_id: int | None) -> StoredDigest | None:
        self.init_schema()
        with _connect(self.db_path) as connection:
            row = connection.execute(
                "SELECT digest_text, revision_key FROM catalog_digests "
                "WHERE project_key = ?",
                (_project_key(project_id),),
            ).fetchone()
        if row is None:
            return None
        return StoredDigest(
            digest_text=str(row["digest_text"]),
            revision_key=str(row["revision_key"]),
        )

    def upsert(
        self, *, project_id: int | None, digest_text: str, revision_key: str
    ) -> None:
        self.init_schema()
        with _connect(self.db_path) as connection:
            connection.execute(
                """
                INSERT INTO catalog_digests
                    (project_key, digest_text, revision_key, updated_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(project_key) DO UPDATE SET
                    digest_text = excluded.digest_text,
                    revision_key = excluded.revision_key,
                    updated_at = excluded.updated_at
                """,
                (
                    _project_key(project_id),
                    digest_text,
                    revision_key,
                    datetime.now(UTC).isoformat(),
                ),
            )


def _batch_lines(lines: list[str], *, max_chars: int) -> list[str]:
    """Group lines into batches whose joined length stays under ``max_chars``."""
    batches: list[str] = []
    current: list[str] = []
    size = 0
    for line in lines:
        addition = len(line) + 1
        if current and size + addition > max_chars:
            batches.append("\n".join(current))
            current = []
            size = 0
        current.append(line)
        size += addition
    if current:
        batches.append("\n".join(current))
    return batches


def _clean(text: str) -> str:
    """Normalize an LLM offerings reply; the NO_OFFERINGS sentinel becomes ''."""
    stripped = text.strip()
    if not stripped or stripped.upper() == _NO_OFFERINGS:
        return ""
    return stripped


class CatalogDigestService:
    """Builds and caches the offerings digest for a project."""

    def __init__(
        self,
        *,
        repository: CatalogDigestRepository,
        openrouter_client: _OfferingsSummarizer,
        project_prompt_repository: ProjectPromptRepository,
    ) -> None:
        self._repo = repository
        self._llm = openrouter_client
        self._prompts = project_prompt_repository

    async def get_digest(self, *, project_id: int | None) -> str:
        source = self._repo.read_source(project_id=project_id)
        if not source.lines:
            return ""
        stored = self._repo.get(project_id=project_id)
        if stored is not None and stored.revision_key == source.revision_key:
            return stored.digest_text
        digest = await self._build(project_id=project_id, lines=source.lines)
        self._repo.upsert(
            project_id=project_id,
            digest_text=digest,
            revision_key=source.revision_key,
        )
        return digest

    async def _build(self, *, project_id: int | None, lines: list[str]) -> str:
        system_prompt = resolve_prompt(
            self._prompts, project_id, "catalog_digest_system"
        )
        batches = _batch_lines(lines, max_chars=_MAX_BATCH_CHARS)
        partials: list[str] = []
        for batch in batches:
            reply = await self._llm.summarize_offerings(
                knowledge_text=batch, system_prompt=system_prompt,
                project_id=project_id, trace_id=None,
            )
            cleaned = _clean(reply)
            if cleaned:
                partials.append(cleaned)
        if not partials:
            return ""
        if len(partials) == 1:
            return partials[0]
        # Reduce: merge and dedupe the per-batch offering lists into one.
        reduced = await self._llm.summarize_offerings(
            knowledge_text="\n".join(partials), system_prompt=system_prompt,
            project_id=project_id, trace_id=None,
        )
        return _clean(reduced)

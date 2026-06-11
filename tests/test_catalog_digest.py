from __future__ import annotations

from typing import Any

import pytest

from services.api.app.catalog_digest import (
    CatalogDigestRepository,
    CatalogDigestService,
    _batch_lines,
)
from services.api.app.project_prompts import ProjectPromptRepository
from services.api.app.rag import RagRepository


def _rag(tmp_path) -> RagRepository:
    return RagRepository(str(tmp_path / "rag.sqlite3"))


def _digest_repo(tmp_path) -> CatalogDigestRepository:
    # Same DB file as the RagRepository so the source scan sees the chunks.
    return CatalogDigestRepository(str(tmp_path / "rag.sqlite3"))


class _FakeSummarizer:
    """Records calls and replays a queue of canned offering replies."""

    def __init__(self, replies: list[str]) -> None:
        self._replies = list(replies)
        self.calls: list[tuple[str, str | None]] = []

    async def summarize_offerings(
        self, *, knowledge_text: str, system_prompt: str | None = None, **_kw: Any
    ) -> str:
        self.calls.append((knowledge_text, system_prompt))
        return self._replies.pop(0)


# --- Repository ---------------------------------------------------------


def test_read_source_excludes_confidential_and_scopes_by_project(tmp_path):
    rag = _rag(tmp_path)
    rag.ingest(source_id="s1", text="Тур на багги", project_id=7)
    rag.ingest(source_id="s2", text="Глобальная услуга", project_id=None)
    rag.ingest(source_id="s3", text="Другой проект", project_id=99)
    rag.ingest(
        source_id="s4", text="Секретный прайс", project_id=7, is_confidential=True
    )

    source = _digest_repo(tmp_path).read_source(project_id=7)

    assert "Тур на багги" in source.lines
    assert "Глобальная услуга" in source.lines  # global is in scope
    assert "Другой проект" not in source.lines  # other project excluded
    assert "Секретный прайс" not in source.lines  # confidential excluded


def test_read_source_none_project_reads_whole_store(tmp_path):
    rag = _rag(tmp_path)
    rag.ingest(source_id="s1", text="A", project_id=7)
    rag.ingest(source_id="s2", text="B", project_id=99)
    source = _digest_repo(tmp_path).read_source(project_id=None)
    assert set(source.lines) == {"A", "B"}


def test_read_source_revision_key_changes_when_chunks_change(tmp_path):
    rag = _rag(tmp_path)
    repo = _digest_repo(tmp_path)
    rag.ingest(source_id="s1", text="A", project_id=7)
    first = repo.read_source(project_id=7).revision_key
    rag.ingest(source_id="s1", text="B", project_id=7)
    second = repo.read_source(project_id=7).revision_key
    assert first != second


def test_get_returns_none_when_missing(tmp_path):
    assert _digest_repo(tmp_path).get(project_id=7) is None


def test_upsert_then_get_round_trip_and_overwrite(tmp_path):
    repo = _digest_repo(tmp_path)
    repo.upsert(project_id=7, digest_text="- A", revision_key="rev1")
    stored = repo.get(project_id=7)
    assert stored is not None
    assert stored.digest_text == "- A"
    assert stored.revision_key == "rev1"
    repo.upsert(project_id=7, digest_text="- A\n- B", revision_key="rev2")
    stored2 = repo.get(project_id=7)
    assert stored2 is not None
    assert stored2.digest_text == "- A\n- B"
    assert stored2.revision_key == "rev2"


# --- Batching helper ----------------------------------------------------


def test_batch_lines_groups_under_budget():
    lines = ["aaaa", "bbbb", "cccc"]
    batches = _batch_lines(lines, max_chars=10)
    # Each line is 4 chars + 1 -> two fit (10), third spills to a new batch.
    assert batches == ["aaaa\nbbbb", "cccc"]


def test_batch_lines_single_batch_when_small():
    assert _batch_lines(["a", "b"], max_chars=100) == ["a\nb"]


# --- Service ------------------------------------------------------------


def _service(tmp_path, summarizer) -> CatalogDigestService:
    return CatalogDigestService(
        repository=_digest_repo(tmp_path),
        openrouter_client=summarizer,
        project_prompt_repository=ProjectPromptRepository(
            str(tmp_path / "prompts.sqlite3")
        ),
    )


@pytest.mark.asyncio
async def test_get_digest_empty_kb_returns_empty_without_llm(tmp_path):
    _rag(tmp_path)  # creates an empty rag_chunks table, as at startup
    summarizer = _FakeSummarizer([])
    service = _service(tmp_path, summarizer)
    assert await service.get_digest(project_id=7) == ""
    assert summarizer.calls == []


@pytest.mark.asyncio
async def test_get_digest_single_batch_builds_and_caches(tmp_path):
    rag = _rag(tmp_path)
    rag.ingest(source_id="s1", text="Тур на багги\nПрокат лодок", project_id=7)
    summarizer = _FakeSummarizer(["- Тур на багги\n- Прокат лодок"])
    service = _service(tmp_path, summarizer)

    result = await service.get_digest(project_id=7)
    assert result == "- Тур на багги\n- Прокат лодок"
    assert len(summarizer.calls) == 1
    # The system prompt resolved from the project repo is passed through.
    assert "NO_OFFERINGS" in (summarizer.calls[0][1] or "")

    # Second call hits the cache (revision unchanged) — no new LLM call.
    again = await service.get_digest(project_id=7)
    assert again == "- Тур на багги\n- Прокат лодок"
    assert len(summarizer.calls) == 1


@pytest.mark.asyncio
async def test_get_digest_rebuilds_when_kb_changes(tmp_path):
    rag = _rag(tmp_path)
    rag.ingest(source_id="s1", text="Тур на багги", project_id=7)
    summarizer = _FakeSummarizer(["- Тур на багги", "- Тур на багги\n- Лодки"])
    service = _service(tmp_path, summarizer)

    assert await service.get_digest(project_id=7) == "- Тур на багги"
    rag.ingest(source_id="s2", text="Прокат лодок", project_id=7)
    assert await service.get_digest(project_id=7) == "- Тур на багги\n- Лодки"
    assert len(summarizer.calls) == 2


@pytest.mark.asyncio
async def test_get_digest_map_reduce_for_large_kb(tmp_path):
    rag = _rag(tmp_path)
    # Two big chunks that exceed one batch -> map (2 calls) then reduce (1 call).
    rag.ingest(source_id="s1", text="x" * 5000, project_id=7)
    rag.ingest(source_id="s2", text="y" * 5000, project_id=7)
    summarizer = _FakeSummarizer(["- Услуга A", "- Услуга B", "- Услуга A\n- Услуга B"])
    service = _service(tmp_path, summarizer)

    result = await service.get_digest(project_id=7)
    assert result == "- Услуга A\n- Услуга B"
    assert len(summarizer.calls) == 3  # 2 map + 1 reduce


@pytest.mark.asyncio
async def test_get_digest_no_offerings_sentinel_becomes_empty(tmp_path):
    rag = _rag(tmp_path)
    rag.ingest(source_id="s1", text="Сегодня хорошая погода", project_id=7)
    summarizer = _FakeSummarizer(["NO_OFFERINGS"])
    service = _service(tmp_path, summarizer)

    assert await service.get_digest(project_id=7) == ""
    # Empty result is cached too, so a repeat does not re-run the LLM.
    assert await service.get_digest(project_id=7) == ""
    assert len(summarizer.calls) == 1


@pytest.mark.asyncio
async def test_get_digest_drops_empty_partials_in_reduce(tmp_path):
    rag = _rag(tmp_path)
    rag.ingest(source_id="s1", text="x" * 5000, project_id=7)
    rag.ingest(source_id="s2", text="y" * 5000, project_id=7)
    # First batch yields a real offering, second yields NO_OFFERINGS -> only one
    # non-empty partial remains, so no reduce call is made.
    summarizer = _FakeSummarizer(["- Услуга A", "NO_OFFERINGS"])
    service = _service(tmp_path, summarizer)

    assert await service.get_digest(project_id=7) == "- Услуга A"
    assert len(summarizer.calls) == 2

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock

import pytest

from services.api.app.answerers import AnswerContext
from services.api.app.answerers.grounded_rag import GroundedRagAnswerer
from services.api.app.openrouter_client import GroundingVerdict, LlmUsageCapture
from services.api.app.project_prompts import ProjectPromptRepository
from services.api.app.rag import RagChunk


def _ctx() -> AnswerContext:
    return AnswerContext(
        chat_id=1,
        customer_username="@c",
        trace_id="t-conf",
        now=datetime(2026, 5, 12, 12, 0, tzinfo=UTC),
        grounding_threshold=0.5,
    )


class _FakeRag:
    def __init__(self, items: list[RagChunk]) -> None:
        self._items = items

    def retrieve(
        self,
        *,
        query: str,
        limit: int = 3,
        project_id: int | None = None,
    ) -> list[RagChunk]:
        return list(self._items)


class _FakeCatalog:
    async def get_digest(self, *, project_id: int | None) -> str:
        return ""


@pytest.mark.asyncio
async def test_confidential_chunk_redacts_metadata_but_grounds_normally(tmp_path):
    confidential_text = "Внутренние расценки на ремонт офисной мебели."
    chunks = [
        RagChunk(
            id=1,
            source_id="knowledge_candidate:42",
            chunk_text=confidential_text,
            score=0.9,
            is_confidential=True,
        )
    ]
    rag = _FakeRag(chunks)
    llm = AsyncMock()
    _cap = LlmUsageCapture(
        model_name="gpt-4o", prompt_tokens=50, completion_tokens=25,
        cost_usd=0.001, created_at="2026-06-11T00:00:00Z",
    )
    llm.answer_grounded = AsyncMock(return_value=(
        "Ответ по расценкам.",
        _cap,
    ))
    llm.verify_grounding = AsyncMock(
        return_value=(
            GroundingVerdict(label="GROUNDED", reason="matches confidential snippet"),
            LlmUsageCapture(
                model_name="gpt-4o", prompt_tokens=20, completion_tokens=10,
                cost_usd=0.0005, created_at="2026-06-11T00:00:00Z",
            ),
        )
    )

    answerer = GroundedRagAnswerer(
        rag_repository=rag,
        openrouter_client=llm,
        persona_reader=lambda: ("Анна", "Иванова"),
        project_prompt_repository=ProjectPromptRepository(
            str(tmp_path / "prompts.sqlite3")
        ),
        catalog_digest_service=_FakeCatalog(),
    )
    result = await answerer.try_answer(question="сколько стоит ремонт?", ctx=_ctx())

    assert result.handled is True
    assert "Ответ по расценкам" in result.text
    metadata_retrieval = result.metadata["retrieval"]
    assert metadata_retrieval[0]["source_id"] == "knowledge_candidate:confidential"
    assert metadata_retrieval[0]["chunk_text"] == "[redacted]"
    assert metadata_retrieval[0]["is_confidential"] is True

    answer_grounded_call = llm.answer_grounded.await_args
    sent_snippets = answer_grounded_call.kwargs["snippets"]
    assert sent_snippets[0].chunk_text == confidential_text


@pytest.mark.asyncio
async def test_non_confidential_chunk_passes_through_in_metadata(tmp_path):
    chunks = [
        RagChunk(
            id=2,
            source_id="kb-public",
            chunk_text="Открытое время работы 9-18.",
            score=0.95,
            is_confidential=False,
        )
    ]
    rag = _FakeRag(chunks)
    llm = AsyncMock()
    llm.answer_grounded = AsyncMock(return_value=(
        "Открыто с 9 до 18.",
        LlmUsageCapture(
            model_name="gpt-4o", prompt_tokens=50, completion_tokens=25,
            cost_usd=0.001, created_at="2026-06-11T00:00:00Z",
        ),
    ))
    llm.verify_grounding = AsyncMock(
        return_value=(
            GroundingVerdict(label="GROUNDED", reason="matches public snippet"),
            LlmUsageCapture(
                model_name="gpt-4o", prompt_tokens=20, completion_tokens=10,
                cost_usd=0.0005, created_at="2026-06-11T00:00:00Z",
            ),
        )
    )
    answerer = GroundedRagAnswerer(
        rag_repository=rag,
        openrouter_client=llm,
        persona_reader=lambda: ("Анна", "Иванова"),
        project_prompt_repository=ProjectPromptRepository(
            str(tmp_path / "prompts.sqlite3")
        ),
        catalog_digest_service=_FakeCatalog(),
    )
    result = await answerer.try_answer(question="часы работы офиса?", ctx=_ctx())

    assert result.handled is True
    metadata_retrieval = result.metadata["retrieval"]
    assert metadata_retrieval[0]["source_id"] == "kb-public"
    assert metadata_retrieval[0]["chunk_text"] == "Открытое время работы 9-18."
    assert metadata_retrieval[0]["is_confidential"] is False

"""Concept-ask when the term is NOT a known service (Story 12.06).

`find_by_name` miss → RAG retrieval scoped to the term. RAG hit → reply.
RAG miss → escalate. This lets the bot answer "что такое каньонинг?"
even when no service row exists for it yet.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import pytest

from services.api.app.answerers import AnswerContext
from services.api.app.rag import RagChunk
from services.api.app.russian_text import get_russian_normalizer
from services.api.app.sales.intent import Intent
from services.api.app.sales.sales_persona_answerer import SalesPersonaAnswerer


@dataclass(frozen=True)
class _FakeService:
    name: str
    description: str | None = None


class _FakeStateRepo:
    def __init__(self) -> None:
        self.rows: dict[int, dict[str, Any]] = {}
        self.upsert_calls: list[dict[str, Any]] = []

    def get(self, chat_id: int):
        return self.rows.get(chat_id)

    def upsert(self, **kwargs: Any) -> None:
        self.upsert_calls.append(kwargs)
        self.rows[int(kwargs["chat_id"])] = dict(kwargs)


class _FakeServicesRepo:
    def __init__(self, services: list[_FakeService]) -> None:
        self._services = list(services)

    def count_active(self, *, project_id: int) -> int:
        return len(self._services)

    def list_for_project(self, *, project_id: int) -> list[_FakeService]:
        return list(self._services)

    def get_by_name(
        self, *, project_id: int, name: str
    ) -> _FakeService | None:
        target = name.strip().casefold()
        for service in self._services:
            if service.name.strip().casefold() == target:
                return service
        return None


class _FakeOpenRouter:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self.queue: list[dict[str, Any]] = []

    def queue_response(self, payload: dict[str, Any]) -> None:
        self.queue.append(payload)

    async def complete_json(
        self, *, system: str, user: str, model: str | None = None
    ) -> dict[str, Any]:
        self.calls.append({"system": system, "user": user, "model": model})
        if not self.queue:
            raise AssertionError("LLM called without a queued payload")
        return self.queue.pop(0)


class _FakeRagRetriever:
    def __init__(self, chunks: list[RagChunk]) -> None:
        self.calls: list[dict[str, Any]] = []
        self._chunks = list(chunks)

    def retrieve(
        self,
        *,
        query: str,
        limit: int = 3,
        project_id: int | None = None,
    ) -> list[RagChunk]:
        self.calls.append(
            {"query": query, "limit": limit, "project_id": project_id}
        )
        return list(self._chunks)


_FIXED_NOW = datetime(2026, 5, 1, 13, 33, tzinfo=UTC)


def _ctx() -> AnswerContext:
    return AnswerContext(
        chat_id=7,
        customer_username="darya",
        trace_id="trace-unknown-service",
        now=_FIXED_NOW,
        project_id=1,
        grounding_threshold=0.6,
    )


def _seed(state_repo: _FakeStateRepo) -> None:
    state_repo.rows[7] = {
        "chat_id": 7,
        "project_id": 1,
        "current_stage": "scoping",
        "collected_intent": Intent(dates="1 мая").to_dict(),
        "last_proposal": None,
        "last_customer_msg_at": None,
        "last_bot_msg_at": None,
    }


@pytest.mark.asyncio
async def test_unknown_service_rag_hit_returns_grounded_reply() -> None:
    # No service rows — the customer asks about "родео".
    chunks = [
        RagChunk(
            id=10,
            source_id="kb:10",
            chunk_text="Родео — это вид экстремального катания на лошадях.",
            score=0.85,
            project_id=1,
        )
    ]
    state_repo = _FakeStateRepo()
    openrouter = _FakeOpenRouter()
    rag = _FakeRagRetriever(chunks=chunks)
    answerer = SalesPersonaAnswerer(
        state_repo=state_repo,
        services_repo=_FakeServicesRepo(services=[]),
        openrouter=openrouter,
        normalizer=get_russian_normalizer(),
        clock=lambda: _FIXED_NOW,
        bot_persona_getter=lambda: "Николай",
        rag_retriever=rag,
    )
    _seed(state_repo)
    openrouter.queue_response(
        {"text": "Родео — это экстремальное катание на лошадях."}
    )

    result = await answerer.try_answer(
        question="А что такое родео?", ctx=_ctx()
    )

    assert result.handled is True
    assert "родео" in (result.text or "").lower()
    assert result.metadata.get("sales_turn_kind") == "concept_rag"
    assert rag.calls and "родео" in rag.calls[0]["query"].lower()


@pytest.mark.asyncio
async def test_unknown_service_rag_miss_escalates() -> None:
    # No service rows, no RAG chunks — escalate.
    state_repo = _FakeStateRepo()
    openrouter = _FakeOpenRouter()
    rag = _FakeRagRetriever(chunks=[])
    answerer = SalesPersonaAnswerer(
        state_repo=state_repo,
        services_repo=_FakeServicesRepo(services=[]),
        openrouter=openrouter,
        normalizer=get_russian_normalizer(),
        clock=lambda: _FIXED_NOW,
        bot_persona_getter=lambda: "Николай",
        rag_retriever=rag,
    )
    _seed(state_repo)

    result = await answerer.try_answer(
        question="А что такое родео?", ctx=_ctx()
    )

    assert result.handled is False
    assert result.metadata.get("skip_reason") == "concept_unknown"
    assert result.metadata.get("concept_term") == "родео"
    assert openrouter.calls == []


@pytest.mark.asyncio
async def test_unknown_service_no_rag_retriever_wired_escalates() -> None:
    """A future deploy without RAG wiring should escalate, not crash."""
    state_repo = _FakeStateRepo()
    answerer = SalesPersonaAnswerer(
        state_repo=state_repo,
        services_repo=_FakeServicesRepo(services=[]),
        openrouter=_FakeOpenRouter(),
        normalizer=get_russian_normalizer(),
        clock=lambda: _FIXED_NOW,
        bot_persona_getter=lambda: "Николай",
        # rag_retriever omitted on purpose.
    )
    _seed(state_repo)

    result = await answerer.try_answer(
        question="А что такое родео?", ctx=_ctx()
    )

    assert result.handled is False
    assert result.metadata.get("skip_reason") == "concept_unknown"

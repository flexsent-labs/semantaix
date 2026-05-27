"""Concept-ask RAG-fallback tests (Story 12.06).

When a matching service exists but has no `description_md`, the answerer
queries the existing RAG retriever scoped to the project. A
high-confidence chunk yields a persona-wrapped one-liner; low confidence
or no chunks escalates via `concept_unknown`.
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
        trace_id="trace-rag-fallback",
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


def _build(
    services: list[_FakeService],
    chunks: list[RagChunk],
):
    state_repo = _FakeStateRepo()
    openrouter = _FakeOpenRouter()
    rag = _FakeRagRetriever(chunks=chunks)
    answerer = SalesPersonaAnswerer(
        state_repo=state_repo,
        services_repo=_FakeServicesRepo(services=services),
        openrouter=openrouter,
        normalizer=get_russian_normalizer(),
        clock=lambda: _FIXED_NOW,
        bot_persona_getter=lambda: "Николай",
        rag_retriever=rag,
    )
    return answerer, state_repo, openrouter, rag


@pytest.mark.asyncio
async def test_concept_high_confidence_rag_returns_persona_wrapped_text() -> None:
    services = [_FakeService(name="Каньонинг", description=None)]
    chunks = [
        RagChunk(
            id=1,
            source_id="kb:1",
            chunk_text="Каньонинг — спуск по верёвке вдоль водопадов.",
            score=0.9,
            project_id=1,
        )
    ]
    answerer, state_repo, openrouter, rag = _build(services, chunks)
    _seed(state_repo)
    openrouter.queue_response(
        {"text": "Каньонинг — это спуск по верёвке вдоль водопадов."}
    )

    result = await answerer.try_answer(
        question="А что такое каньонинг?", ctx=_ctx()
    )

    assert result.handled is True
    assert "каньонинг" in (result.text or "").lower()
    assert result.metadata.get("sales_turn_kind") == "concept_rag"
    # RAG query scoped to the term + project.
    assert rag.calls
    assert "каньонинг" in rag.calls[0]["query"].lower()
    assert rag.calls[0]["project_id"] == 1
    # LLM invoked exactly once for the wrap.
    assert len(openrouter.calls) == 1
    # Funnel state preserved.
    state = state_repo.rows[7]
    assert state["current_stage"] == "scoping"
    assert state["collected_intent"] == Intent(dates="1 мая").to_dict()


@pytest.mark.asyncio
async def test_concept_low_confidence_chunk_escalates() -> None:
    services = [_FakeService(name="Каньонинг", description=None)]
    chunks = [
        RagChunk(
            id=2,
            source_id="kb:2",
            chunk_text="Не релевантный фрагмент.",
            score=0.2,
            project_id=1,
        )
    ]
    answerer, state_repo, openrouter, rag = _build(services, chunks)
    _seed(state_repo)

    result = await answerer.try_answer(
        question="А что такое каньонинг?", ctx=_ctx()
    )

    assert result.handled is False
    assert result.metadata.get("skip_reason") == "concept_unknown"
    assert openrouter.calls == [], "LLM must not be called on a low-score chunk"
    # State is still untouched.
    assert state_repo.rows[7]["current_stage"] == "scoping"


@pytest.mark.asyncio
async def test_concept_no_chunks_escalates() -> None:
    services = [_FakeService(name="Каньонинг", description=None)]
    answerer, state_repo, openrouter, rag = _build(services, chunks=[])
    _seed(state_repo)

    result = await answerer.try_answer(
        question="А что такое каньонинг?", ctx=_ctx()
    )

    assert result.handled is False
    assert result.metadata.get("skip_reason") == "concept_unknown"
    assert rag.calls, "RAG must be queried before escalation"


@pytest.mark.asyncio
async def test_concept_rag_llm_transport_error_escalates() -> None:
    """A transient LLM transport error during the wrap escalates cleanly."""
    services = [_FakeService(name="Каньонинг", description=None)]
    chunks = [
        RagChunk(
            id=4,
            source_id="kb:4",
            chunk_text="Каньонинг — спуск по верёвке.",
            score=0.9,
            project_id=1,
        )
    ]
    state_repo = _FakeStateRepo()

    class _BrokenOpenRouter:
        async def complete_json(
            self, *, system: str, user: str, model: str | None = None
        ) -> dict[str, Any]:
            raise RuntimeError("transport boom")

    rag = _FakeRagRetriever(chunks=chunks)
    answerer = SalesPersonaAnswerer(
        state_repo=state_repo,
        services_repo=_FakeServicesRepo(services=services),
        openrouter=_BrokenOpenRouter(),
        normalizer=get_russian_normalizer(),
        clock=lambda: _FIXED_NOW,
        bot_persona_getter=lambda: "Николай",
        rag_retriever=rag,
    )
    _seed(state_repo)

    result = await answerer.try_answer(
        question="А что такое каньонинг?", ctx=_ctx()
    )

    assert result.handled is False
    assert result.metadata.get("skip_reason") == "concept_unknown"


@pytest.mark.asyncio
async def test_concept_rag_empty_payload_escalates() -> None:
    """When the LLM returns a payload without a usable text field, escalate."""
    services = [_FakeService(name="Каньонинг", description=None)]
    chunks = [
        RagChunk(
            id=5,
            source_id="kb:5",
            chunk_text="Каньонинг — спуск по верёвке.",
            score=0.9,
            project_id=1,
        )
    ]
    answerer, state_repo, openrouter, _ = _build(services, chunks)
    _seed(state_repo)
    # Empty / unusable payload — no `text` and no `next_question` keys.
    openrouter.queue_response({"text": ""})

    result = await answerer.try_answer(
        question="А что такое каньонинг?", ctx=_ctx()
    )

    assert result.handled is False
    assert result.metadata.get("skip_reason") == "concept_unknown"


@pytest.mark.asyncio
async def test_concept_grounding_threshold_getter_falls_back_on_error() -> None:
    """If the threshold getter raises, fall back to ctx.grounding_threshold."""
    services = [_FakeService(name="Каньонинг", description=None)]
    chunks = [
        RagChunk(
            id=6,
            source_id="kb:6",
            chunk_text="Каньонинг — спуск по верёвке.",
            score=0.65,
            project_id=1,
        )
    ]
    state_repo = _FakeStateRepo()
    openrouter = _FakeOpenRouter()
    rag = _FakeRagRetriever(chunks=chunks)

    def _broken_getter() -> float:
        raise ValueError("config corrupt")

    answerer = SalesPersonaAnswerer(
        state_repo=state_repo,
        services_repo=_FakeServicesRepo(services=services),
        openrouter=openrouter,
        normalizer=get_russian_normalizer(),
        clock=lambda: _FIXED_NOW,
        bot_persona_getter=lambda: "Николай",
        rag_retriever=rag,
        grounding_threshold_getter=_broken_getter,
    )
    _seed(state_repo)
    openrouter.queue_response({"text": "Это спуск по верёвке."})

    result = await answerer.try_answer(
        question="А что такое каньонинг?",
        ctx=_ctx(),  # grounding_threshold=0.6 — chunk at 0.65 passes
    )

    assert result.handled is True
    assert result.metadata.get("sales_turn_kind") == "concept_rag"


@pytest.mark.asyncio
async def test_concept_grounding_threshold_getter_overrides_ctx() -> None:
    """If a runtime threshold getter is wired, it overrides ctx.grounding_threshold."""
    services = [_FakeService(name="Каньонинг", description=None)]
    chunks = [
        RagChunk(
            id=3,
            source_id="kb:3",
            chunk_text="Каньонинг — спуск по верёвке вдоль водопадов.",
            score=0.55,
            project_id=1,
        )
    ]
    state_repo = _FakeStateRepo()
    openrouter = _FakeOpenRouter()
    rag = _FakeRagRetriever(chunks=chunks)
    # Override threshold to 0.5 — chunk at 0.55 should now pass.
    answerer = SalesPersonaAnswerer(
        state_repo=state_repo,
        services_repo=_FakeServicesRepo(services=services),
        openrouter=openrouter,
        normalizer=get_russian_normalizer(),
        clock=lambda: _FIXED_NOW,
        bot_persona_getter=lambda: "Николай",
        rag_retriever=rag,
        grounding_threshold_getter=lambda: 0.5,
    )
    _seed(state_repo)
    openrouter.queue_response(
        {"text": "Каньонинг — это спуск по верёвке вдоль водопадов."}
    )

    result = await answerer.try_answer(
        question="А что такое каньонинг?",
        ctx=AnswerContext(
            chat_id=7,
            customer_username="darya",
            trace_id="trace-threshold",
            now=_FIXED_NOW,
            project_id=1,
            grounding_threshold=0.9,  # would normally reject 0.55
        ),
    )

    assert result.handled is True
    assert result.metadata.get("sales_turn_kind") == "concept_rag"

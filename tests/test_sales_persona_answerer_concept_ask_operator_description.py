"""Concept-ask "operator description" path (Story 12.06).

When `find_by_name(term)` returns a service with a populated
`description_md`, the bot returns that description verbatim — no LLM
rewrite. This keeps a tour like "каньонинг — это…" exactly the way the
operator wrote it.
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


class _RecordingOpenRouter:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def complete_json(
        self, *, system: str, user: str, model: str | None = None
    ) -> dict[str, Any]:
        self.calls.append({"system": system, "user": user, "model": model})
        raise AssertionError(
            "LLM must not be invoked when an operator-authored description exists"
        )


class _RecordingRag:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

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
        raise AssertionError(
            "RAG must not be queried when the operator wrote a description"
        )


_FIXED_NOW = datetime(2026, 5, 1, 13, 33, tzinfo=UTC)


def _ctx() -> AnswerContext:
    return AnswerContext(
        chat_id=7,
        customer_username="darya",
        trace_id="trace-concept-op",
        now=_FIXED_NOW,
        project_id=1,
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


def _build(services: list[_FakeService]):
    state_repo = _FakeStateRepo()
    openrouter = _RecordingOpenRouter()
    rag = _RecordingRag()
    services_repo = _FakeServicesRepo(services=services)
    answerer = SalesPersonaAnswerer(
        state_repo=state_repo,
        services_repo=services_repo,
        openrouter=openrouter,
        normalizer=get_russian_normalizer(),
        clock=lambda: _FIXED_NOW,
        bot_persona_getter=lambda: "Николай",
        rag_retriever=rag,
    )
    return answerer, state_repo, openrouter, rag


@pytest.mark.asyncio
async def test_concept_op_description_returned_verbatim() -> None:
    description = (
        "Каньонинг — это спуск по верёвке вдоль водопадов; "
        "идеально для активного отдыха."
    )
    services = [_FakeService(name="Каньонинг", description=description)]
    answerer, state_repo, openrouter, rag = _build(services)
    _seed(state_repo)

    result = await answerer.try_answer(
        question="А что такое каньонинг?", ctx=_ctx()
    )

    assert result.handled is True
    assert result.text == description
    assert openrouter.calls == [], "LLM must not be called"
    assert rag.calls == [], "RAG retriever must not be queried"
    assert result.metadata.get("sales_turn_kind") == "concept_op_desc"
    assert result.metadata.get("service_name") == "Каньонинг"
    # Funnel state preserved.
    state = state_repo.rows[7]
    assert state["current_stage"] == "scoping"
    assert state["collected_intent"] == Intent(dates="1 мая").to_dict()


@pytest.mark.asyncio
async def test_concept_match_is_case_insensitive() -> None:
    description = "Тур по живописным местам."
    services = [_FakeService(name="Медовеевка Лайт", description=description)]
    answerer, state_repo, _, _ = _build(services)
    _seed(state_repo)

    result = await answerer.try_answer(
        question="Расскажите про медовеевку лайт",
        ctx=_ctx(),
    )

    # `Расскажите про` is a concept trigger; case-insensitive match
    # against the configured service name returns the description.
    assert result.handled is True
    assert result.text == description


@pytest.mark.asyncio
async def test_concept_description_empty_falls_through_to_rag() -> None:
    """An empty-string `description_md` is treated as missing, not verbatim."""
    services = [_FakeService(name="Каньонинг", description="   ")]
    state_repo = _FakeStateRepo()
    openrouter = _RecordingOpenRouter()

    class _RecordedRag:
        def __init__(self) -> None:
            self.calls: list[dict[str, Any]] = []

        def retrieve(self, *, query, limit=3, project_id=None):
            self.calls.append(
                {"query": query, "limit": limit, "project_id": project_id}
            )
            return []  # empty → escalation

    rag = _RecordedRag()
    services_repo = _FakeServicesRepo(services=services)
    answerer = SalesPersonaAnswerer(
        state_repo=state_repo,
        services_repo=services_repo,
        openrouter=openrouter,
        normalizer=get_russian_normalizer(),
        clock=lambda: _FIXED_NOW,
        bot_persona_getter=lambda: "Николай",
        rag_retriever=rag,
    )
    _seed(state_repo)

    result = await answerer.try_answer(
        question="А что такое каньонинг?", ctx=_ctx()
    )

    # Empty description → RAG path; this fake RAG returns nothing → escalate.
    assert rag.calls, "RAG must be queried when description is empty"
    assert result.handled is False
    assert result.metadata.get("skip_reason") == "concept_unknown"

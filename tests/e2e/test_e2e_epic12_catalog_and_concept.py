"""Epic 12, Story 12.06 — catalog list + concept-explainer turn.

The pipeline wiring for ``SalesPersonaAnswerer`` lands in story 12.09, so
the integration here drives the answerer directly against real SQLite
repositories (sales state + project services + RAG). Three flows:

  1. Customer: "Что у вас есть?" → bot lists both seeded service names.
  2. Customer: "А что такое каньонинг?" → bot returns the operator's
     description verbatim (``sales_turn_kind="concept_op_desc"``).
  3. Customer: "А что такое родео?" → no service, no RAG hit → escalate
     with ``skip_reason="concept_unknown"``.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import pytest

from services.api.app.answerers import AnswerContext
from services.api.app.calendar.project_services_repository import (
    ProjectServiceRepository,
)
from services.api.app.rag import RagChunk
from services.api.app.russian_text import get_russian_normalizer
from services.api.app.sales.sales_persona_answerer import SalesPersonaAnswerer
from services.api.app.sales.state_repository import StateRepository

pytestmark = [
    pytest.mark.e2e,
    pytest.mark.epic("12"),
    pytest.mark.story("12-06"),
]


@dataclass
class _ServiceShim:
    """Adapter exposing ``ProjectService`` rows as `_ServiceRow` duck-types."""

    name: str
    description: str | None


class _ServicesRepoAdapter:
    """Wrap ``ProjectServiceRepository`` so it speaks the answerer's protocol."""

    def __init__(self, repo: ProjectServiceRepository) -> None:
        self._repo = repo

    def count_active(self, *, project_id: int) -> int:
        return len(self._repo.list_for_project(project_id=project_id))

    def list_for_project(self, *, project_id: int) -> list[_ServiceShim]:
        return [
            _ServiceShim(name=row.name, description=row.description)
            for row in self._repo.list_for_project(project_id=project_id)
        ]

    def get_by_name(
        self, *, project_id: int, name: str
    ) -> _ServiceShim | None:
        row = self._repo.get_by_name(project_id=project_id, name=name)
        if row is None:
            return None
        return _ServiceShim(name=row.name, description=row.description)


class _StubOpenRouter:
    def __init__(self) -> None:
        self.queue: list[dict[str, Any]] = []
        self.calls: list[dict[str, Any]] = []

    def queue_response(self, payload: dict[str, Any]) -> None:
        self.queue.append(payload)

    async def complete_json(
        self, *, system: str, user: str, model: str | None = None
    ) -> dict[str, Any]:
        self.calls.append({"system": system, "user": user, "model": model})
        if not self.queue:
            raise AssertionError("LLM called without a queued payload")
        return self.queue.pop(0)


class _EmptyRag:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def retrieve(
        self, *, query: str, limit: int = 3, project_id: int | None = None
    ) -> list[RagChunk]:
        self.calls.append(
            {"query": query, "limit": limit, "project_id": project_id}
        )
        return []


_NOW = datetime(2026, 5, 1, 13, 33, tzinfo=UTC)


def _ctx(chat_id: int, project_id: int) -> AnswerContext:
    return AnswerContext(
        chat_id=chat_id,
        customer_username="darya",
        trace_id=f"e2e-epic12-{chat_id}",
        now=_NOW,
        project_id=project_id,
        grounding_threshold=0.6,
    )


def _build_answerer(
    tmp_path,
) -> tuple[SalesPersonaAnswerer, ProjectServiceRepository, _StubOpenRouter, _EmptyRag]:
    sales_db = str(tmp_path / "sales.sqlite3")
    services_db = str(tmp_path / "services.sqlite3")
    state_repo = StateRepository(db_path=sales_db)
    services_repo = ProjectServiceRepository(db_path=services_db)
    openrouter = _StubOpenRouter()
    rag = _EmptyRag()
    answerer = SalesPersonaAnswerer(
        state_repo=state_repo,
        services_repo=_ServicesRepoAdapter(services_repo),
        openrouter=openrouter,
        normalizer=get_russian_normalizer(),
        clock=lambda: _NOW,
        bot_persona_getter=lambda: "Николай",
        rag_retriever=rag,
    )
    return answerer, services_repo, openrouter, rag


@pytest.mark.asyncio
async def test_catalog_lists_seeded_service_names(tmp_path) -> None:
    answerer, services_repo, openrouter, _ = _build_answerer(tmp_path)
    project_id = 42
    services_repo.upsert(
        project_id=project_id,
        name="Медовеевка Лайт",
        description="Лайт уровень, с видами",
    )
    services_repo.upsert(
        project_id=project_id,
        name="Каньонинг",
        description="Каньонинг — это спуск по верёвке вдоль водопадов.",
    )

    # Seed scoping state so the aside-intercept gate fires.
    openrouter.queue_response(
        {
            "extracted_fields": {"dates": "1 мая"},
            "next_question": "Сколько человек?",
        }
    )
    await answerer.try_answer(
        question="Хочу тур 1 мая", ctx=_ctx(chat_id=10, project_id=project_id)
    )

    # Catalog ask in mid-scoping.
    result = await answerer.try_answer(
        question="А что у вас вообще есть?",
        ctx=_ctx(chat_id=10, project_id=project_id),
    )

    assert result.handled is True
    assert "Медовеевка Лайт" in (result.text or "")
    assert "Каньонинг" in (result.text or "")
    assert result.metadata.get("sales_turn_kind") == "catalog"


@pytest.mark.asyncio
async def test_concept_returns_operator_description_verbatim(tmp_path) -> None:
    answerer, services_repo, openrouter, rag = _build_answerer(tmp_path)
    project_id = 43
    description = "Каньонинг — это спуск по верёвке вдоль водопадов."
    services_repo.upsert(
        project_id=project_id,
        name="Каньонинг",
        description=description,
    )

    openrouter.queue_response(
        {
            "extracted_fields": {"dates": "1 мая"},
            "next_question": "Сколько человек?",
        }
    )
    await answerer.try_answer(
        question="Хочу тур 1 мая",
        ctx=_ctx(chat_id=11, project_id=project_id),
    )
    llm_calls_before = len(openrouter.calls)
    rag_calls_before = len(rag.calls)

    result = await answerer.try_answer(
        question="А что такое каньонинг?",
        ctx=_ctx(chat_id=11, project_id=project_id),
    )

    assert result.handled is True
    assert result.text == description
    assert result.metadata.get("sales_turn_kind") == "concept_op_desc"
    # Verbatim path — neither RAG nor LLM consulted on this turn.
    assert len(openrouter.calls) == llm_calls_before
    assert len(rag.calls) == rag_calls_before


@pytest.mark.asyncio
async def test_concept_unknown_term_escalates(tmp_path) -> None:
    answerer, services_repo, openrouter, rag = _build_answerer(tmp_path)
    project_id = 44
    services_repo.upsert(
        project_id=project_id,
        name="Каньонинг",
        description="Каньонинг — это спуск по верёвке.",
    )

    openrouter.queue_response(
        {
            "extracted_fields": {"dates": "1 мая"},
            "next_question": "Сколько человек?",
        }
    )
    await answerer.try_answer(
        question="Хочу тур 1 мая",
        ctx=_ctx(chat_id=12, project_id=project_id),
    )
    llm_calls_before = len(openrouter.calls)

    result = await answerer.try_answer(
        question="А что такое родео?",
        ctx=_ctx(chat_id=12, project_id=project_id),
    )

    assert result.handled is False
    assert result.metadata.get("skip_reason") == "concept_unknown"
    assert result.metadata.get("concept_term") == "родео"
    # No LLM call: the RAG retriever returned empty, so no wrap was requested.
    assert len(openrouter.calls) == llm_calls_before
    # RAG was queried scoped to the project.
    assert rag.calls
    assert rag.calls[-1]["project_id"] == project_id

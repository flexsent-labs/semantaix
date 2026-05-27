"""Funnel state preservation across catalog/concept asides (Story 12.06).

A three-turn run: scoping → concept aside → scoping resumes with the prior
`collected_intent` intact. The aside must not bump `current_stage` or
clobber any collected field.
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


class _NoopRag:
    def retrieve(
        self, *, query: str, limit: int = 3, project_id: int | None = None
    ) -> list[RagChunk]:
        return []


_FIXED_NOW = datetime(2026, 5, 1, 13, 33, tzinfo=UTC)


def _ctx() -> AnswerContext:
    return AnswerContext(
        chat_id=7,
        customer_username="darya",
        trace_id="trace-funnel-state",
        now=_FIXED_NOW,
        project_id=1,
        grounding_threshold=0.6,
    )


@pytest.mark.asyncio
async def test_concept_aside_preserves_collected_intent_and_stage() -> None:
    description = "Каньонинг — спуск по верёвке вдоль водопадов."
    services = [_FakeService(name="Каньонинг", description=description)]
    state_repo = _FakeStateRepo()
    openrouter = _FakeOpenRouter()
    rag = _NoopRag()
    answerer = SalesPersonaAnswerer(
        state_repo=state_repo,
        services_repo=_FakeServicesRepo(services=services),
        openrouter=openrouter,
        normalizer=get_russian_normalizer(),
        clock=lambda: _FIXED_NOW,
        bot_persona_getter=lambda: "Николай",
        rag_retriever=rag,
    )

    # Turn 1: customer first message → greeting → scoping.
    openrouter.queue_response(
        {
            "extracted_fields": {"dates": "1 мая"},
            "next_question": "Сколько человек?",
        }
    )
    await answerer.try_answer(
        question="Хочу тур 1 мая, какие даты возможны?", ctx=_ctx()
    )
    state_after_turn1 = dict(state_repo.rows[7])
    assert state_after_turn1["current_stage"] == "scoping"
    assert state_after_turn1["collected_intent"] == Intent(
        dates="1 мая"
    ).to_dict()

    # Turn 2: customer asks "что такое каньонинг?" mid-scoping — aside.
    upsert_count_before_aside = len(state_repo.upsert_calls)
    aside_result = await answerer.try_answer(
        question="А что такое каньонинг?", ctx=_ctx()
    )
    assert aside_result.handled is True
    assert aside_result.text == description
    # No state mutation — aside did not call upsert.
    assert len(state_repo.upsert_calls) == upsert_count_before_aside
    # State on disk matches turn-1 snapshot.
    assert state_repo.rows[7]["current_stage"] == "scoping"
    assert (
        state_repo.rows[7]["collected_intent"]
        == Intent(dates="1 мая").to_dict()
    )

    # Turn 3: scoping resumes — customer provides headcount.
    openrouter.queue_response(
        {
            "extracted_fields": {"headcount": 6},
            "next_question": "Сколько квадроциклов?",
        }
    )
    await answerer.try_answer(question="Нас 6 человек", ctx=_ctx())
    final = state_repo.rows[7]
    assert final["current_stage"] == "scoping"
    # The pre-aside `dates` is intact, and the new field merged in.
    assert final["collected_intent"] == Intent(
        dates="1 мая", headcount=6
    ).to_dict()


@pytest.mark.asyncio
async def test_catalog_aside_in_scoping_does_not_overwrite_state() -> None:
    services = [_FakeService(name="Каньонинг")]
    state_repo = _FakeStateRepo()
    openrouter = _FakeOpenRouter()
    answerer = SalesPersonaAnswerer(
        state_repo=state_repo,
        services_repo=_FakeServicesRepo(services=services),
        openrouter=openrouter,
        normalizer=get_russian_normalizer(),
        clock=lambda: _FIXED_NOW,
        bot_persona_getter=lambda: "Николай",
        rag_retriever=_NoopRag(),
    )

    # Seed mid-scoping with two fields already collected.
    state_repo.rows[7] = {
        "chat_id": 7,
        "project_id": 1,
        "current_stage": "scoping",
        "collected_intent": Intent(dates="1 мая", headcount=6).to_dict(),
        "last_proposal": None,
        "last_customer_msg_at": None,
        "last_bot_msg_at": None,
    }
    upsert_count_before = len(state_repo.upsert_calls)

    result = await answerer.try_answer(
        question="Что у вас есть?", ctx=_ctx()
    )

    assert result.handled is True
    assert len(state_repo.upsert_calls) == upsert_count_before
    assert state_repo.rows[7]["collected_intent"] == Intent(
        dates="1 мая", headcount=6
    ).to_dict()

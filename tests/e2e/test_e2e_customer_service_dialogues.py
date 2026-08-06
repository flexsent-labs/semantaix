"""Customer-facing conversational acceptance scenarios.

These tests deliberately avoid asserting the bot's exact prose.  They model
different customer intentions and verify the observable contract instead:
which part of the pipeline handled the turn, whether the conversation state
progressed, and whether the answer stayed grounded instead of inventing a
service or silently dropping the request.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

import pytest

from services.api.app.answerers import AnswerContext, AnswerPipeline, AnswerResult
from services.api.app.answerers.grounded_rag import GroundedRagAnswerer
from services.api.app.calendar.project_services_repository import (
    ProjectServiceRepository,
)
from services.api.app.project_prompts import ProjectPromptRepository
from services.api.app.rag import RagChunk
from services.api.app.russian_text import get_russian_normalizer
from services.api.app.sales.intent import Intent
from services.api.app.sales.sales_persona_answerer import SalesPersonaAnswerer
from services.api.app.sales.state_repository import StateRepository

pytestmark = [
    pytest.mark.e2e,
    pytest.mark.epic("12"),
    pytest.mark.story("12-customer-dialogues"),
]

_NOW = datetime(2026, 5, 1, 9, 0, tzinfo=UTC)


class _StubOpenRouter:
    def __init__(self) -> None:
        self.queue: list[dict[str, Any]] = []

    def queue_response(self, payload: dict[str, Any]) -> None:
        self.queue.append(payload)

    async def complete_json(
        self, *, system: str, user: str, model: str | None = None, **_kw: Any
    ) -> dict[str, Any]:
        del system, user, model
        if not self.queue:
            raise AssertionError("LLM called without a queued test response")
        return self.queue.pop(0)


@dataclass
class _Conversation:
    pipeline: AnswerPipeline
    chat_id: int
    project_id: int
    turns: list[tuple[str, AnswerResult]] = field(default_factory=list)

    async def say(self, message: str) -> AnswerResult:
        result = await self.pipeline.run(
            question=message,
            ctx=AnswerContext(
                chat_id=self.chat_id,
                customer_username="@customer",
                trace_id=f"dialogue-{self.chat_id}-{len(self.turns) + 1}",
                now=_NOW,
                project_id=self.project_id,
            ),
        )
        self.turns.append((message, result))
        return result


def _build_sales_environment(
    tmp_path, *, project_id: int, with_services: bool = True
) -> tuple[_Conversation, StateRepository, _StubOpenRouter, ProjectServiceRepository]:
    state_repo = StateRepository(db_path=str(tmp_path / "sales_state.sqlite3"))
    services_repo = ProjectServiceRepository(
        db_path=str(tmp_path / "project_services.sqlite3")
    )
    if with_services:
        services_repo.upsert(
            project_id=project_id,
            name="Багги",
            description="Поездка на багги по лесному маршруту с остановками.",
        )
        services_repo.upsert(
            project_id=project_id,
            name="Квадроциклы",
            description="Маршрут на квадроциклах для начинающих и опытных гостей.",
        )

    openrouter = _StubOpenRouter()
    answerer = SalesPersonaAnswerer(
        state_repo=state_repo,
        services_repo=services_repo,
        openrouter=openrouter,
        normalizer=get_russian_normalizer(),
        clock=lambda: _NOW,
        bot_persona_getter=lambda: "Анна",
    )
    return (
        _Conversation(
            pipeline=AnswerPipeline([answerer]),
            chat_id=project_id * 100 + 1,
            project_id=project_id,
        ),
        state_repo,
        openrouter,
        services_repo,
    )


@pytest.mark.asyncio
async def test_customer_explores_catalog_and_then_asks_how_a_service_works(
    tmp_path,
) -> None:
    """A curious customer can explore first and only later enter booking."""
    conversation, _state_repo, openrouter, _services_repo = _build_sales_environment(
        tmp_path, project_id=1201
    )

    greeting = await conversation.say("Привет")
    assert greeting.handled is True
    assert greeting.metadata["sales_turn_kind"] == "greeting"
    # The first turn scopes the customer's need; it must not jump to a date.
    greeting_text = (greeting.text or "").casefold()
    assert "дат" not in greeting_text

    catalog = await conversation.say("Какие варианты поездок у вас есть?")
    assert catalog.handled is True
    assert catalog.metadata["sales_turn_kind"] == "catalog"
    catalog_text = catalog.text or ""
    assert "Багги" in catalog_text
    assert "Квадроциклы" in catalog_text

    # A later detailed question has booking context, so it enters the normal
    # funnel and the next turn can be answered from the structured service row.
    openrouter.queue_response(
        {
            "extracted_fields": {"dates": "2026-05-02"},
            "next_question": "Сколько будет человек?",
        }
    )
    started = await conversation.say("Хочу подробнее про поездку на багги 2 мая")
    assert started.handled is True

    details = await conversation.say("А что такое багги?")
    assert details.handled is True
    assert details.metadata["sales_turn_kind"] == "concept_op_desc"
    # Validate that the answer contains an operator fact, without locking the
    # test to punctuation or the exact sentence template.
    assert "маршрут" in (details.text or "").casefold()


@pytest.mark.asyncio
async def test_customer_dialogue_collects_booking_details_before_asking_for_time(
    tmp_path,
) -> None:
    """A booking conversation persists answers instead of losing its context."""
    conversation, state_repo, openrouter, _services_repo = _build_sales_environment(
        tmp_path, project_id=1202
    )

    openrouter.queue_response(
        {
            "extracted_fields": {"dates": "2026-05-03"},
            "next_question": "Сколько будет человек?",
        }
    )
    turns = [
        "Хочу записаться на поездку 3 мая",
    ]
    for extracted, customer_message in (
        ({"headcount": 4}, "Нас четверо"),
        ({"vehicle_count": 2}, "Нам нужны два багги"),
        ({"difficulty": "начальный"}, "Мы новички"),
        ({"drivers": 1}, "Водитель будет один"),
    ):
        openrouter.queue_response(
            {"extracted_fields": extracted, "next_question": "Принято"}
        )
        turns.append(customer_message)

    results = [await conversation.say(message) for message in turns]
    assert all(result.handled for result in results)

    state = state_repo.get(conversation.chat_id)
    assert state is not None
    intent = state["collected_intent"]
    assert intent["dates"]
    assert intent["headcount"] == 4
    assert intent["vehicle_count"] == 2
    assert intent["difficulty"] == "начальный"
    assert intent["drivers"] == 1
    # With all factual fields collected, the next missing booking fact is the
    # time/slot; the implementation may ask, offer, or hand that to an operator.
    assert state["current_stage"] in {
        "awaiting_time",
        "pitching",
        "proposing",
        "closing",
    }


@pytest.mark.asyncio
async def test_customer_can_select_a_service_with_common_typos(tmp_path) -> None:
    """A typo in a service name still advances the booking conversation."""
    conversation, state_repo, openrouter, _services_repo = _build_sales_environment(
        tmp_path, project_id=1205
    )

    greeting = await conversation.say("Привет")
    assert greeting.handled is True
    catalog = await conversation.say("Услуги")
    assert catalog.handled is True

    openrouter.queue_response(
        {
            "extracted_fields": {},
            "next_question": "Здравствуйте! На какую дату планируете поездку?",
        }
    )
    started = await conversation.say("Хочу поездить на квадрацыклах")
    assert started.handled is True
    assert started.metadata.get("stage_after") == "scoping"
    assert "здравствуйте" not in (started.text or "").casefold()

    selected = await conversation.say("квадрациклы")
    assert selected.handled is True
    assert selected.metadata.get("sales_turn_kind") == "service_selection"
    assert selected.metadata.get("stage_after") == "scoping"
    assert "дат" in (selected.text or "").casefold()
    assert state_repo.get(conversation.chat_id)["current_stage"] == "scoping"

    # A customer can answer the date question with a short typo-heavy reply;
    # this must continue the funnel even when no LLM response is available.
    date_reply = await conversation.say("завтро")
    assert date_reply.handled is True
    assert date_reply.metadata.get("sales_turn_kind") == "temporal_reply"
    assert "человек" in (date_reply.text or "").casefold()
    assert state_repo.get(conversation.chat_id)["collected_intent"]["dates"] == "завтра"


@pytest.mark.asyncio
async def test_new_service_selection_drops_previous_booking_fields(tmp_path) -> None:
    """A new service must collect date and party size again.

    This mirrors the live regression: a prior booking had already stored date
    and headcount, then the customer selected another service after a greeting.
    The new questionnaire must not inherit those values.
    """
    conversation, state_repo, _openrouter, _services_repo = _build_sales_environment(
        tmp_path, project_id=1206
    )
    state_repo.upsert(
        chat_id=conversation.chat_id,
        project_id=conversation.project_id,
        current_stage="scoping",
        collected_intent=Intent(
            dates="завтра",
            headcount=3,
            vehicle_count=2,
            extra={"service": "багги"},
        ).to_dict(),
        now=_NOW,
    )

    greeting = await conversation.say("Здравствуйте")
    selected = await conversation.say("квадро")

    assert greeting.handled is True
    assert selected.handled is True
    assert selected.metadata["sales_turn_kind"] == "service_selection"
    assert "дат" in (selected.text or "").casefold()
    assert state_repo.get(conversation.chat_id)["collected_intent"] == Intent(
        extra={"service": "квадроцикл"}
    ).to_dict()


@pytest.mark.asyncio
async def test_unknown_service_question_is_not_invented_during_booking_dialogue(
    tmp_path,
) -> None:
    """The assistant must defer an unknown offering instead of making it up."""
    conversation, _state_repo, openrouter, _services_repo = _build_sales_environment(
        tmp_path, project_id=1203
    )
    openrouter.queue_response(
        {
            "extracted_fields": {"dates": "2026-05-04"},
            "next_question": "Сколько человек?",
        }
    )
    await conversation.say("Хочу поездку 4 мая")

    unknown = await conversation.say("А что такое вертолётная прогулка?")
    assert unknown.handled is False


class _CatalogRag:
    def __init__(self, chunks: list[RagChunk]) -> None:
        self.chunks = chunks
        self.queries: list[str] = []

    def retrieve(
        self, *, query: str, limit: int = 3, project_id: int | None = None
    ) -> list[RagChunk]:
        del limit, project_id
        self.queries.append(query)
        return list(self.chunks)


class _BrokenCatalogDigest:
    async def get_digest(self, *, project_id: int | None) -> str:
        del project_id
        raise RuntimeError("catalog summarizer unavailable")


class _UnavailableGrounding:
    async def answer_grounded(self, **_kwargs: Any) -> tuple[str, None]:
        raise RuntimeError("OpenRouter unavailable")


@pytest.mark.asyncio
async def test_catalog_dialogue_uses_imported_rag_when_llm_is_unavailable(tmp_path) -> None:
    """A catalog request remains useful when the optional LLM layer is down."""
    project_id = 1204
    rag = _CatalogRag(
        [
            RagChunk(
                id=1,
                source_id="tour-brochure.pdf",
                chunk_text="Туры на багги по лесному маршруту",
                score=0.91,
                project_id=project_id,
            ),
            RagChunk(
                id=2,
                source_id="tour-brochure.pdf",
                chunk_text="Квадроциклы для начинающих и опытных гостей",
                score=0.88,
                project_id=project_id,
            ),
            RagChunk(
                id=3,
                source_id="internal.txt",
                chunk_text="Служебная заметка оператора",
                score=0.99,
                is_confidential=True,
                project_id=project_id,
            ),
        ]
    )
    state_repo = StateRepository(db_path=str(tmp_path / "rag_sales_state.sqlite3"))
    services_repo = ProjectServiceRepository(
        db_path=str(tmp_path / "rag_services.sqlite3")
    )
    sales = SalesPersonaAnswerer(
        state_repo=state_repo,
        services_repo=services_repo,
        openrouter=_StubOpenRouter(),
        normalizer=get_russian_normalizer(),
        clock=lambda: _NOW,
        bot_persona_getter=lambda: "Анна",
        rag_retriever=rag,
    )
    grounded = GroundedRagAnswerer(
        rag_repository=rag,
        openrouter_client=_UnavailableGrounding(),
        persona_reader=lambda: ("Анна", "Иванова"),
        project_prompt_repository=ProjectPromptRepository(
            str(tmp_path / "rag_prompts.sqlite3")
        ),
        catalog_digest_service=_BrokenCatalogDigest(),
    )
    conversation = _Conversation(
        pipeline=AnswerPipeline([sales, grounded]),
        chat_id=project_id * 100 + 1,
        project_id=project_id,
    )

    greeting = await conversation.say("Здравствуйте")
    assert greeting.handled is True
    catalog = await conversation.say("Какие услуги у вас есть?")

    assert catalog.handled is True
    assert catalog.response_mode == "grounded_rag_fallback"
    catalog_text = catalog.text or ""
    assert "багги" in catalog_text.casefold()
    assert "квадроциклы" in catalog_text.casefold()
    assert "служебная" not in catalog_text.casefold()
    assert rag.queries == ["Какие услуги у вас есть?"]

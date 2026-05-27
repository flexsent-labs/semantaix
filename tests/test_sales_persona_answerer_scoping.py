"""Scoping-stage tests for SalesPersonaAnswerer (Story 12.03).

Scoping is the loop in `scoping` until all 5 intent fields are populated.
Order: dates → headcount → vehicle_count → difficulty → drivers. Once all
five are present the next turn transitions to `pitching` and v1 of this
story `_skip`s pitching with `stage_not_implemented_yet`.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest

from services.api.app.answerers import AnswerContext
from services.api.app.russian_text import get_russian_normalizer
from services.api.app.sales.intent import Intent
from services.api.app.sales.sales_persona_answerer import SalesPersonaAnswerer


class _FakeStateRepo:
    def __init__(self) -> None:
        self.rows: dict[int, dict[str, Any]] = {}
        self.upsert_calls: list[dict[str, Any]] = []

    def get(self, chat_id: int):
        return self.rows.get(chat_id)

    def upsert(self, **kwargs: Any) -> None:
        self.upsert_calls.append(kwargs)
        chat_id = int(kwargs["chat_id"])
        self.rows[chat_id] = dict(kwargs)


class _FakeServicesRepo:
    def count_active(self, *, project_id: int) -> int:  # pragma: no cover
        return 0


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


_FIXED_NOW = datetime(2026, 5, 1, 13, 33, tzinfo=UTC)


def _clock() -> datetime:
    return _FIXED_NOW


def _ctx() -> AnswerContext:
    return AnswerContext(
        chat_id=7,
        customer_username="darya",
        trace_id="trace-scope",
        now=_FIXED_NOW,
        project_id=1,
    )


def _build() -> tuple[SalesPersonaAnswerer, _FakeStateRepo, _FakeOpenRouter]:
    state_repo = _FakeStateRepo()
    openrouter = _FakeOpenRouter()
    answerer = SalesPersonaAnswerer(
        state_repo=state_repo,
        services_repo=_FakeServicesRepo(),
        openrouter=openrouter,
        normalizer=get_russian_normalizer(),
        clock=_clock,
        bot_persona_getter=lambda: "Николай",
    )
    return answerer, state_repo, openrouter


@pytest.mark.asyncio
async def test_full_scoping_run_then_pitching_is_skipped() -> None:
    answerer, state_repo, openrouter = _build()
    # Turn 1 — greeting (new → scoping), customer mentions dates.
    openrouter.queue_response(
        {
            "extracted_fields": {"dates": "1 мая"},
            "next_question": "Сколько человек поедет?",
        }
    )
    # Turn 2 — scoping, customer answers headcount.
    openrouter.queue_response(
        {
            "extracted_fields": {"headcount": 6},
            "next_question": "Сколько квадроциклов нужно?",
        }
    )
    # Turn 3 — scoping, customer answers vehicle_count.
    openrouter.queue_response(
        {
            "extracted_fields": {"vehicle_count": 3},
            "next_question": "Какой сложности хотите тур?",
        }
    )
    # Turn 4 — scoping, customer answers difficulty.
    openrouter.queue_response(
        {
            "extracted_fields": {"difficulty": "средний"},
            "next_question": "Кто за рулем? Опыт есть?",
        }
    )
    # Turn 5 — scoping, customer answers drivers — last field; the turn
    # itself still asks the question (the bot replied before the LLM saw
    # the new value), so this turn just records the field and remains in
    # scoping. The transition to pitching happens on the NEXT customer
    # turn, where the answerer detects all 5 are present and skips with
    # `stage_not_implemented_yet`.
    openrouter.queue_response(
        {
            "extracted_fields": {"drivers": "мужчины 30+, опыт"},
            "next_question": "",
        }
    )

    questions = [
        "1 мая хочу тур",
        "нас 6",
        "3 квадрика",
        "средний",
        "мужчины 30+",
    ]
    for question in questions:
        result = await answerer.try_answer(question=question, ctx=_ctx())
        assert result.handled is True

    # All five upserts went through; state ends with full intent +
    # current_stage='pitching' (transition recorded once all 5 fields hit).
    assert len(state_repo.upsert_calls) == 5
    last = state_repo.upsert_calls[-1]
    assert last["collected_intent"] == Intent(
        dates="1 мая",
        headcount=6,
        vehicle_count=3,
        difficulty="средний",
        drivers="мужчины 30+, опыт",
    ).to_dict()
    assert last["current_stage"] == "pitching"

    # Sixth turn — resume from `pitching` state → skip per the v1 contract.
    result = await answerer.try_answer(question="ну что?", ctx=_ctx())
    assert result.handled is False
    assert result.metadata.get("skip_reason") == "stage_not_implemented_yet"


@pytest.mark.asyncio
async def test_scoping_picks_next_missing_field_in_order() -> None:
    """The scoping system prompt must name the next missing field at the
    top of the missing-fields list — `dates` first, then `headcount`, …
    so the LLM asks them in that order across turns."""
    answerer, state_repo, openrouter = _build()
    # Seed an in-scoping row with an empty intent → next missing is `dates`.
    state_repo.rows[7] = {
        "chat_id": 7,
        "project_id": 1,
        "current_stage": "scoping",
        "collected_intent": Intent().to_dict(),
        "last_proposal": None,
        "last_customer_msg_at": None,
        "last_bot_msg_at": None,
    }
    openrouter.queue_response(
        {"extracted_fields": {}, "next_question": "Какие даты?"}
    )
    await answerer.try_answer(question="да", ctx=_ctx())
    first_system = openrouter.calls[-1]["system"]
    # The missing-fields list inside the system prompt drives the order.
    dates_index = first_system.find("dates")
    headcount_index = first_system.find("headcount")
    assert dates_index != -1
    assert headcount_index != -1
    # Both fields appear, but `dates` is listed BEFORE `headcount`.
    assert dates_index < headcount_index

    # Seed the state to skip directly to headcount.
    state_repo.rows[7] = {
        "chat_id": 7,
        "project_id": 1,
        "current_stage": "scoping",
        "collected_intent": Intent(dates="1 мая").to_dict(),
        "last_proposal": None,
        "last_customer_msg_at": None,
        "last_bot_msg_at": None,
    }
    openrouter.queue_response(
        {"extracted_fields": {}, "next_question": "Сколько человек?"}
    )
    await answerer.try_answer(question="да", ctx=_ctx())
    second_system = openrouter.calls[-1]["system"]
    # `dates` now in the "known" block; `headcount` is the next missing one.
    assert "headcount" in second_system


@pytest.mark.asyncio
async def test_scoping_merges_partial_extraction_idempotently() -> None:
    """When the customer mentions two fields in one turn, both are merged."""
    answerer, state_repo, openrouter = _build()
    openrouter.queue_response(
        {
            "extracted_fields": {"dates": "1 мая", "headcount": 6},
            "next_question": "Сколько квадроциклов?",
        }
    )
    await answerer.try_answer(
        question="1 мая, нас 6 человек", ctx=_ctx()
    )
    last = state_repo.upsert_calls[-1]
    assert last["collected_intent"] == Intent(
        dates="1 мая", headcount=6
    ).to_dict()
    assert last["current_stage"] == "scoping"


@pytest.mark.asyncio
async def test_scoping_uses_existing_state_intent_as_base() -> None:
    answerer, state_repo, openrouter = _build()
    state_repo.rows[7] = {
        "chat_id": 7,
        "project_id": 1,
        "current_stage": "scoping",
        "collected_intent": Intent(dates="1 мая", headcount=6).to_dict(),
        "last_proposal": None,
        "last_customer_msg_at": None,
        "last_bot_msg_at": None,
    }
    openrouter.queue_response(
        {
            "extracted_fields": {"vehicle_count": 3},
            "next_question": "Какой сложности?",
        }
    )
    await answerer.try_answer(question="3 квадрика", ctx=_ctx())
    last = state_repo.upsert_calls[-1]
    assert last["collected_intent"] == Intent(
        dates="1 мая", headcount=6, vehicle_count=3
    ).to_dict()


@pytest.mark.asyncio
async def test_scoping_does_not_clobber_existing_field_with_null() -> None:
    answerer, state_repo, openrouter = _build()
    state_repo.rows[7] = {
        "chat_id": 7,
        "project_id": 1,
        "current_stage": "scoping",
        "collected_intent": Intent(dates="1 мая").to_dict(),
        "last_proposal": None,
        "last_customer_msg_at": None,
        "last_bot_msg_at": None,
    }
    # Defensive: LLM violates the prompt and sends `null` for an already-
    # populated field. The merge MUST preserve "1 мая".
    openrouter.queue_response(
        {
            "extracted_fields": {"dates": None, "headcount": 6},
            "next_question": "Сколько квадроциклов?",
        }
    )
    await answerer.try_answer(question="нас шестеро", ctx=_ctx())
    last = state_repo.upsert_calls[-1]
    assert last["collected_intent"] == Intent(
        dates="1 мая", headcount=6
    ).to_dict()

"""Story 12.28 — a price question on the FIRST turn must be answered.

The per-turn classifier already tags "сколько стоит" as a price ask, but it
was only consulted mid-funnel (scoping / pitching). On first contact the
greeting handler extracted the fields, was forbidden to quote a price, and
asked for a date — silently dropping the customer's actual question.

Live bug (багги, 31 May 2026, "Анна Иванова"):

    Customer: 8 человек, сколько стоит?
    Анна:     На какую дату планируете?     ← price + group size both dropped
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest

from services.api.app.answerers import AnswerContext
from services.api.app.russian_text import get_russian_normalizer
from services.api.app.sales.intent import Intent
from services.api.app.sales.price_lookup import (
    PriceFound,
    PriceMissing,
    PriceUnknownPayload,
)
from services.api.app.sales.sales_persona_answerer import (
    HITL_REASON_PRICE_UNKNOWN,
    RESPONSE_MODE_SALES_ESCALATION,
    STAGE_AWAITING_OPERATOR_PRICE,
    STAGE_NEW,
    STAGE_PRICING,
    STAGE_SCOPING,
    SalesPersonaAnswerer,
)

_NOW = datetime(2026, 4, 25, 9, 0, tzinfo=UTC)


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
    def count_active(self, *, project_id: int) -> int:
        return 1

    def list_for_project(self, *, project_id: int) -> list:
        return []

    def get_by_name(self, *, project_id: int, name: str):
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


class _StubPriceLookup:
    def __init__(self, result: PriceFound | PriceMissing) -> None:
        self.result = result
        self.calls: list[dict[str, Any]] = []

    async def lookup(
        self, *, project_id: int | None, intent: Intent, question: str, **_kwargs
    ):
        self.calls.append(
            {"project_id": project_id, "intent": intent, "question": question}
        )
        return self.result


def _ctx() -> AnswerContext:
    return AnswerContext(
        chat_id=7,
        customer_username="anna",
        trace_id="trace-first-turn-price",
        now=_NOW,
        project_id=1,
    )


def _build(*, price_result=None, with_pricing: bool = True):
    state_repo = _FakeStateRepo()
    openrouter = _FakeOpenRouter()
    price_lookup = (
        _StubPriceLookup(result=price_result) if with_pricing else None
    )
    answerer = SalesPersonaAnswerer(
        state_repo=state_repo,
        services_repo=_FakeServicesRepo(),
        openrouter=openrouter,
        normalizer=get_russian_normalizer(),
        clock=lambda: _NOW,
        bot_persona_getter=lambda: "Анна",
        price_lookup=price_lookup,
    )
    return answerer, state_repo, openrouter, price_lookup


@pytest.mark.asyncio
async def test_first_turn_price_ask_is_answered_not_dropped() -> None:
    """`8 человек, сколько стоит?` on turn 1 → a price quote, not a date question."""
    found = PriceFound(
        text="Каньонинг — 15 000 ₽ за группу.",
        source_chunk_id="11",
        snippet="Каньонинг — 15 000 ₽ за группу.",
    )
    answerer, state_repo, openrouter, price_lookup = _build(price_result=found)
    # 1) greeting extraction (headcount=8); 2) pricing render.
    openrouter.queue_response(
        {"extracted_fields": {"headcount": 8}, "next_question": "На какую дату?"}
    )
    openrouter.queue_response({"text": "Каньонинг — 15 000 ₽ за группу."})

    result = await answerer.try_answer(
        question="8 человек, сколько стоит?", ctx=_ctx()
    )

    assert result.handled is True
    assert "15 000 ₽" in (result.text or "")
    assert result.text != "На какую дату?"  # did NOT jump to dates
    assert result.metadata["stage_before"] == STAGE_NEW
    assert result.metadata["stage_after"] == STAGE_PRICING
    assert result.metadata["sales_turn_kind"] == "pricing_hit"


@pytest.mark.asyncio
async def test_first_turn_group_size_is_captured_for_pricing() -> None:
    """The headcount from the opener rides into pricing and is persisted (not re-asked)."""
    found = PriceFound(
        text="15 000 ₽ за группу.", source_chunk_id="11", snippet="15 000 ₽ за группу."
    )
    answerer, state_repo, openrouter, price_lookup = _build(price_result=found)
    openrouter.queue_response(
        {"extracted_fields": {"headcount": 8}, "next_question": "На какую дату?"}
    )
    openrouter.queue_response({"text": "15 000 ₽ за группу."})

    await answerer.try_answer(question="8 человек, сколько стоит?", ctx=_ctx())

    # The just-extracted headcount reached the price lookup …
    assert len(price_lookup.calls) == 1
    assert price_lookup.calls[0]["intent"].headcount == 8
    # … and is persisted so the funnel never re-asks "сколько человек?".
    assert state_repo.upsert_calls[-1]["collected_intent"]["headcount"] == 8
    assert state_repo.upsert_calls[-1]["current_stage"] == STAGE_PRICING


@pytest.mark.asyncio
async def test_first_turn_price_miss_escalates_not_date_jump() -> None:
    """An unknown price on turn 1 → escalate (price_unknown), still not a date question."""
    answerer, _state_repo, openrouter, price_lookup = _build(
        price_result=PriceMissing(
            payload=PriceUnknownPayload(
                service=None,
                vehicle_type=None,
                hours=None,
                original_question="8 человек, сколько стоит?",
            )
        )
    )
    openrouter.queue_response(
        {"extracted_fields": {"headcount": 8}, "next_question": "На какую дату?"}
    )

    result = await answerer.try_answer(
        question="8 человек, сколько стоит?", ctx=_ctx()
    )

    assert result.response_mode == RESPONSE_MODE_SALES_ESCALATION
    assert result.metadata["escalate"] is True
    assert result.metadata["hitl_reason"] == HITL_REASON_PRICE_UNKNOWN
    assert result.metadata["stage_after"] == STAGE_AWAITING_OPERATOR_PRICE
    # The miss path skips the pricing LLM — only the greeting call happened.
    assert len(openrouter.calls) == 1


@pytest.mark.asyncio
async def test_first_turn_non_price_opener_still_scopes() -> None:
    """A booking opener with no price ask is unaffected — greeting → scoping."""
    answerer, _state_repo, openrouter, price_lookup = _build(
        price_result=PriceFound(text="x", source_chunk_id="1", snippet="x")
    )
    openrouter.queue_response(
        {
            "extracted_fields": {"dates": "завтра"},
            "next_question": "Сколько человек поедет?",
        }
    )

    result = await answerer.try_answer(
        question="хочу багги завтра", ctx=_ctx()
    )

    assert result.text == "Сколько человек поедет?"
    assert result.metadata["stage_after"] == STAGE_SCOPING
    assert price_lookup.calls == []  # price path never touched


@pytest.mark.asyncio
async def test_first_turn_price_ask_without_pricing_configured_falls_through() -> None:
    """No price lookup wired → greeting proceeds normally (guard, no crash)."""
    answerer, _state_repo, openrouter, _ = _build(with_pricing=False)
    openrouter.queue_response(
        {
            "extracted_fields": {"headcount": 8},
            "next_question": "На какую дату планируете?",
        }
    )

    result = await answerer.try_answer(
        question="8 человек, сколько стоит?", ctx=_ctx()
    )

    assert result.text == "На какую дату планируете?"
    assert result.metadata["stage_after"] == STAGE_SCOPING

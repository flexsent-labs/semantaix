"""Pricing-hit test for `SalesPersonaAnswerer` (Story 12.04).

A seeded ``pricing`` row (or a scoping row whose turn is a price ask) + a
``PriceLookup`` that returns ``PriceFound`` → the answerer renders a
one-sentence reply that contains the verbatim price token, persists the
``source_chunk_id`` in result metadata, and stays in pricing.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest

from services.api.app.answerers import AnswerContext
from services.api.app.russian_text import get_russian_normalizer
from services.api.app.sales.intent import Intent
from services.api.app.sales.price_lookup import PriceFound, PriceMissing
from services.api.app.sales.sales_persona_answerer import (
    STAGE_PRICING,
    STAGE_SCOPING,
    SalesPersonaAnswerer,
)


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
        self,
        *,
        project_id: int | None,
        intent: Intent,
        question: str,
    ):
        self.calls.append(
            {
                "project_id": project_id,
                "intent": intent,
                "question": question,
            }
        )
        return self.result


_NOW = datetime(2026, 4, 25, 9, 0, tzinfo=UTC)


def _ctx() -> AnswerContext:
    return AnswerContext(
        chat_id=7,
        customer_username="darya",
        trace_id="trace-pricing-hit",
        now=_NOW,
        project_id=1,
    )


def _build(*, price_result):
    state_repo = _FakeStateRepo()
    openrouter = _FakeOpenRouter()
    price_lookup = _StubPriceLookup(result=price_result)
    answerer = SalesPersonaAnswerer(
        state_repo=state_repo,
        services_repo=_FakeServicesRepo(),
        openrouter=openrouter,
        normalizer=get_russian_normalizer(),
        clock=lambda: _NOW,
        bot_persona_getter=lambda: "Николай",
        price_lookup=price_lookup,
    )
    return answerer, state_repo, openrouter, price_lookup


def _seed(state_repo: _FakeStateRepo, *, stage: str, intent: Intent) -> None:
    state_repo.rows[7] = {
        "chat_id": 7,
        "project_id": 1,
        "current_stage": stage,
        "collected_intent": intent.to_dict(),
        "last_proposal": None,
        "last_customer_msg_at": None,
        "last_bot_msg_at": None,
    }


@pytest.mark.asyncio
async def test_pricing_hit_renders_verbatim_price_and_stays_in_pricing() -> None:
    found = PriceFound(
        text="6 часов — 15 000 ₽",
        source_chunk_id="42",
        snippet="6 часов — 15 000 ₽",
    )
    answerer, state_repo, openrouter, price_lookup = _build(price_result=found)
    _seed(state_repo, stage=STAGE_PRICING, intent=Intent(dates="1 мая"))
    openrouter.queue_response({"text": "6 часов — 15 000 ₽."})

    result = await answerer.try_answer(
        question="сколько стоит 6 часов?", ctx=_ctx()
    )

    assert result.handled is True
    assert result.text == "6 часов — 15 000 ₽."
    # Verbatim price token preserved.
    assert "15 000 ₽" in (result.text or "")
    assert result.metadata["sales_turn_kind"] == "pricing_hit"
    assert result.metadata["stage_after"] == STAGE_PRICING
    assert result.metadata["sales_price_source_chunk_id"] == "42"

    # PriceLookup was called once with the customer's question + project id.
    assert len(price_lookup.calls) == 1
    assert price_lookup.calls[0]["project_id"] == 1
    assert price_lookup.calls[0]["question"] == "сколько стоит 6 часов?"

    # State persisted to pricing.
    assert state_repo.upsert_calls[-1]["current_stage"] == STAGE_PRICING


@pytest.mark.asyncio
async def test_pricing_hit_from_scoping_transitions_to_pricing() -> None:
    """A scoping customer who asks a price question lands in `pricing`."""
    found = PriceFound(
        text="Полдня каньонинга — 15 000 ₽ за группу.",
        source_chunk_id="11",
        snippet="Полдня каньонинга — 15 000 ₽ за группу.",
    )
    answerer, state_repo, openrouter, _ = _build(price_result=found)
    _seed(state_repo, stage=STAGE_SCOPING, intent=Intent(dates="1 мая"))
    openrouter.queue_response(
        {"text": "Полдня каньонинга — 15 000 ₽ за группу."}
    )

    result = await answerer.try_answer(
        question="а сколько стоит каньонинг?", ctx=_ctx()
    )

    assert result.handled is True
    assert "15 000 ₽" in (result.text or "")
    assert result.metadata["stage_before"] == STAGE_SCOPING
    assert result.metadata["stage_after"] == STAGE_PRICING
    assert state_repo.upsert_calls[-1]["current_stage"] == STAGE_PRICING


@pytest.mark.asyncio
async def test_pricing_hit_accepts_next_question_payload_shape() -> None:
    found = PriceFound(
        text="6 часов — 15 000 ₽",
        source_chunk_id="42",
        snippet="6 часов — 15 000 ₽",
    )
    answerer, state_repo, openrouter, _ = _build(price_result=found)
    _seed(state_repo, stage=STAGE_PRICING, intent=Intent(dates="1 мая"))
    openrouter.queue_response({"next_question": "Это 15 000 ₽ за группу."})

    result = await answerer.try_answer(
        question="сколько стоит?", ctx=_ctx()
    )

    assert result.handled is True
    assert "15 000 ₽" in (result.text or "")
    assert result.metadata["sales_turn_kind"] == "pricing_hit"


@pytest.mark.asyncio
async def test_pricing_hit_prompt_carries_persona_and_snippet() -> None:
    found = PriceFound(
        text="6 часов — 15 000 ₽",
        source_chunk_id="42",
        snippet="6 часов — 15 000 ₽",
    )
    answerer, state_repo, openrouter, _ = _build(price_result=found)
    _seed(state_repo, stage=STAGE_PRICING, intent=Intent())
    openrouter.queue_response({"text": "6 часов — 15 000 ₽."})

    await answerer.try_answer(question="сколько стоит?", ctx=_ctx())

    assert openrouter.calls
    system = openrouter.calls[-1]["system"]
    assert "Николай" in system
    assert "15 000 ₽" in system

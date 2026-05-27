"""Pricing-miss test for `SalesPersonaAnswerer` (Story 12.04).

Empty RAG → no LLM call, the customer-facing reply is the fixed Russian
``Уточню у коллег и сразу сообщу`` line, the answerer signals a HITL
ticket with ``reason='price_unknown'`` + the structured payload, and the
funnel transitions to ``awaiting_operator_price``.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest

from services.api.app.answerers import AnswerContext
from services.api.app.russian_text import get_russian_normalizer
from services.api.app.sales.intent import Intent
from services.api.app.sales.price_lookup import (
    PriceMissing,
    PriceUnknownPayload,
)
from services.api.app.sales.sales_persona_answerer import (
    HITL_REASON_PRICE_UNKNOWN,
    PRICING_MISS_FALLBACK,
    RESPONSE_MODE_SALES_ESCALATION,
    STAGE_AWAITING_OPERATOR_PRICE,
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


class _NeverCalledOpenRouter:
    async def complete_json(
        self, *, system: str, user: str, model: str | None = None
    ) -> Any:
        raise AssertionError("LLM must not be called on a pricing miss")


class _StubPriceLookup:
    def __init__(self, result: PriceMissing) -> None:
        self.result = result

    async def lookup(
        self, *, project_id: int | None, intent: Intent, question: str
    ):
        return self.result


_NOW = datetime(2026, 4, 25, 9, 0, tzinfo=UTC)


def _ctx() -> AnswerContext:
    return AnswerContext(
        chat_id=7,
        customer_username="darya",
        trace_id="trace-pricing-miss",
        now=_NOW,
        project_id=1,
    )


def _build(*, stage: str = STAGE_PRICING, question_for_payload: str = "сколько стоит 6 часов?"):
    state_repo = _FakeStateRepo()
    payload = PriceUnknownPayload(
        service=None,
        vehicle_type=None,
        hours=None,
        original_question=question_for_payload,
    )
    price_lookup = _StubPriceLookup(PriceMissing(payload=payload))
    answerer = SalesPersonaAnswerer(
        state_repo=state_repo,
        services_repo=_FakeServicesRepo(),
        openrouter=_NeverCalledOpenRouter(),
        normalizer=get_russian_normalizer(),
        clock=lambda: _NOW,
        bot_persona_getter=lambda: "Николай",
        price_lookup=price_lookup,
    )
    state_repo.rows[7] = {
        "chat_id": 7,
        "project_id": 1,
        "current_stage": stage,
        "collected_intent": Intent(dates="1 мая").to_dict(),
        "last_proposal": None,
        "last_customer_msg_at": None,
        "last_bot_msg_at": None,
    }
    return answerer, state_repo


@pytest.mark.asyncio
async def test_pricing_miss_returns_fixed_line_and_signals_hitl() -> None:
    answerer, state_repo = _build(
        question_for_payload="сколько стоит 6 часов?"
    )
    result = await answerer.try_answer(
        question="сколько стоит 6 часов?", ctx=_ctx()
    )

    assert result.handled is True
    # Fixed Russian line — no LLM in the loop.
    assert result.text == PRICING_MISS_FALLBACK
    assert result.response_mode == RESPONSE_MODE_SALES_ESCALATION
    assert result.metadata["escalate"] is True
    assert result.metadata["hitl_reason"] == HITL_REASON_PRICE_UNKNOWN
    assert result.metadata["sales_turn_kind"] == "pricing_miss"
    # Payload carries the verbatim question.
    payload = result.metadata["sales_price_unknown_payload"]
    assert payload["original_question"] == "сколько стоит 6 часов?"
    assert payload["service"] is None
    assert payload["vehicle_type"] is None
    assert payload["hours"] is None
    # Stage transitions to awaiting_operator_price.
    assert result.metadata["stage_after"] == STAGE_AWAITING_OPERATOR_PRICE
    assert state_repo.upsert_calls[-1]["current_stage"] == (
        STAGE_AWAITING_OPERATOR_PRICE
    )


@pytest.mark.asyncio
async def test_pricing_miss_from_scoping_also_parks_in_awaiting_operator_price() -> None:
    answerer, state_repo = _build(stage=STAGE_SCOPING)
    result = await answerer.try_answer(
        question="а сколько это стоит?", ctx=_ctx()
    )

    assert result.handled is True
    assert result.text == PRICING_MISS_FALLBACK
    assert result.metadata["stage_before"] == STAGE_SCOPING
    assert result.metadata["stage_after"] == STAGE_AWAITING_OPERATOR_PRICE
    assert state_repo.upsert_calls[-1]["current_stage"] == (
        STAGE_AWAITING_OPERATOR_PRICE
    )


@pytest.mark.asyncio
async def test_awaiting_operator_price_next_turn_reenters_pricing() -> None:
    """Customer turns in `awaiting_operator_price` route through pricing.

    The operator's actual price reply travels via the HITL reply path —
    NOT this answerer. If the customer follows up while still parked,
    the bot re-enters pricing (no "уточняю..." loop).
    """
    answerer, state_repo = _build(stage=STAGE_AWAITING_OPERATOR_PRICE)
    result = await answerer.try_answer(
        question="ну так сколько в итоге?", ctx=_ctx()
    )

    assert result.handled is True
    assert result.text == PRICING_MISS_FALLBACK
    # Stays parked in awaiting_operator_price (still a miss; same outcome).
    assert state_repo.upsert_calls[-1]["current_stage"] == (
        STAGE_AWAITING_OPERATOR_PRICE
    )

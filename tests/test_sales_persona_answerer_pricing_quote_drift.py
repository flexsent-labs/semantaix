"""Pricing-drift test for `SalesPersonaAnswerer` (Story 12.04).

If the LLM rewrites the price (or drops the currency, or invents a new
number), the answerer MUST NOT deliver that text. Instead it replies with
the fixed ``уточню у коллег…`` line, signals a HITL handoff with
``reason='price_unknown'``, and parks the conversation in
``awaiting_operator_price`` so the operator answers authoritatively.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest

from services.api.app.answerers import AnswerContext
from services.api.app.russian_text import get_russian_normalizer
from services.api.app.sales.intent import Intent
from services.api.app.sales.price_lookup import PriceFound
from services.api.app.sales.sales_persona_answerer import (
    HITL_REASON_PRICE_UNKNOWN,
    PRICING_MISS_FALLBACK,
    RESPONSE_MODE_SALES_ESCALATION,
    STAGE_AWAITING_OPERATOR_PRICE,
    STAGE_PRICING,
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


class _FixedOpenRouter:
    def __init__(self, payload: Any) -> None:
        self.payload = payload
        self.calls: list[dict[str, Any]] = []

    async def complete_json(
        self, *, system: str, user: str, model: str | None = None
    ) -> Any:
        self.calls.append({"system": system, "user": user})
        return self.payload


class _RaisingOpenRouter:
    async def complete_json(
        self, *, system: str, user: str, model: str | None = None
    ) -> Any:
        raise RuntimeError("transport down")


class _StubPriceLookup:
    def __init__(self, result: PriceFound) -> None:
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
        trace_id="trace-pricing-drift",
        now=_NOW,
        project_id=1,
    )


def _build(*, openrouter, found_text="6 часов — 15 000 ₽"):
    state_repo = _FakeStateRepo()
    answerer = SalesPersonaAnswerer(
        state_repo=state_repo,
        services_repo=_FakeServicesRepo(),
        openrouter=openrouter,
        normalizer=get_russian_normalizer(),
        clock=lambda: _NOW,
        bot_persona_getter=lambda: "Николай",
        price_lookup=_StubPriceLookup(
            PriceFound(
                text=found_text,
                source_chunk_id="42",
                snippet=found_text,
            )
        ),
    )
    state_repo.rows[7] = {
        "chat_id": 7,
        "project_id": 1,
        "current_stage": STAGE_PRICING,
        "collected_intent": Intent(dates="1 мая").to_dict(),
        "last_proposal": None,
        "last_customer_msg_at": None,
        "last_bot_msg_at": None,
    }
    return answerer, state_repo


@pytest.mark.asyncio
async def test_drift_in_price_number_escalates_with_fallback() -> None:
    # LLM made up a different number; the snippet token "15 000 ₽" never
    # appears in the reply → verifier rejects.
    openrouter = _FixedOpenRouter({"text": "Стоит около 12 000 ₽."})
    answerer, state_repo = _build(openrouter=openrouter)

    result = await answerer.try_answer(
        question="сколько стоит 6 часов?", ctx=_ctx()
    )

    assert result.handled is True
    assert result.text == PRICING_MISS_FALLBACK
    assert result.response_mode == RESPONSE_MODE_SALES_ESCALATION
    assert result.metadata["escalate"] is True
    assert result.metadata["hitl_reason"] == HITL_REASON_PRICE_UNKNOWN
    assert result.metadata["sales_turn_kind"] == "pricing_quote_drift"
    assert result.metadata["sales_price_source_chunk_id"] == "42"
    # Funnel parks in awaiting_operator_price.
    assert state_repo.upsert_calls[-1]["current_stage"] == (
        STAGE_AWAITING_OPERATOR_PRICE
    )


@pytest.mark.asyncio
async def test_empty_llm_text_escalates_as_drift() -> None:
    answerer, _ = _build(openrouter=_FixedOpenRouter({"text": ""}))
    result = await answerer.try_answer(question="сколько стоит?", ctx=_ctx())
    assert result.text == PRICING_MISS_FALLBACK
    assert result.metadata["sales_turn_kind"] == "pricing_quote_drift"
    assert result.metadata["hitl_reason"] == HITL_REASON_PRICE_UNKNOWN


@pytest.mark.asyncio
async def test_non_dict_payload_escalates_as_drift() -> None:
    answerer, _ = _build(openrouter=_FixedOpenRouter("not a dict"))
    result = await answerer.try_answer(question="сколько стоит?", ctx=_ctx())
    assert result.text == PRICING_MISS_FALLBACK
    assert result.metadata["sales_turn_kind"] == "pricing_quote_drift"


@pytest.mark.asyncio
async def test_llm_transport_failure_escalates_as_drift_without_text() -> None:
    state_repo = _FakeStateRepo()
    answerer = SalesPersonaAnswerer(
        state_repo=state_repo,
        services_repo=_FakeServicesRepo(),
        openrouter=_RaisingOpenRouter(),
        normalizer=get_russian_normalizer(),
        clock=lambda: _NOW,
        bot_persona_getter=lambda: "Николай",
        price_lookup=_StubPriceLookup(
            PriceFound(
                text="6 часов — 15 000 ₽",
                source_chunk_id="42",
                snippet="6 часов — 15 000 ₽",
            )
        ),
    )
    state_repo.rows[7] = {
        "chat_id": 7,
        "project_id": 1,
        "current_stage": STAGE_PRICING,
        "collected_intent": Intent(dates="1 мая").to_dict(),
        "last_proposal": None,
        "last_customer_msg_at": None,
        "last_bot_msg_at": None,
    }

    result = await answerer.try_answer(
        question="сколько стоит?", ctx=_ctx()
    )

    assert result.text == PRICING_MISS_FALLBACK
    assert result.metadata["hitl_reason"] == HITL_REASON_PRICE_UNKNOWN
    # No drift_text since the LLM never returned anything.
    assert result.metadata["drift_text"] is None

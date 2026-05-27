"""Pricing rag-unavailable test for `SalesPersonaAnswerer` (Story 12.04).

When the price lookup itself raises (RAG / sqlite transport failure),
the answerer must NOT escalate as price_unknown — that would create
a HITL ticket on every transient infra error. Instead it returns
``_skip("rag_unavailable")`` so downstream answerers (or the standard
HITL ack path) get a chance to handle the turn.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest

from services.api.app.answerers import AnswerContext
from services.api.app.russian_text import get_russian_normalizer
from services.api.app.sales.intent import Intent
from services.api.app.sales.sales_persona_answerer import (
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


class _NeverCalledOpenRouter:
    async def complete_json(
        self, *, system: str, user: str, model: str | None = None
    ) -> Any:
        raise AssertionError("LLM must not be called when RAG is unavailable")


class _BrokenPriceLookup:
    async def lookup(
        self, *, project_id: int | None, intent: Intent, question: str
    ):
        raise RuntimeError("sqlite locked")


_NOW = datetime(2026, 4, 25, 9, 0, tzinfo=UTC)


def _ctx() -> AnswerContext:
    return AnswerContext(
        chat_id=7,
        customer_username="darya",
        trace_id="trace-pricing-rag-down",
        now=_NOW,
        project_id=1,
    )


@pytest.mark.asyncio
async def test_price_lookup_exception_skips_with_rag_unavailable_reason() -> None:
    state_repo = _FakeStateRepo()
    answerer = SalesPersonaAnswerer(
        state_repo=state_repo,
        services_repo=_FakeServicesRepo(),
        openrouter=_NeverCalledOpenRouter(),
        normalizer=get_russian_normalizer(),
        clock=lambda: _NOW,
        bot_persona_getter=lambda: "Николай",
        price_lookup=_BrokenPriceLookup(),
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
        question="сколько стоит 6 часов?", ctx=_ctx()
    )

    assert result.handled is False
    assert result.metadata.get("skip_reason") == "rag_unavailable"
    # State must not change on a skip — the turn is deferred to downstream.
    assert state_repo.upsert_calls == []


@pytest.mark.asyncio
async def test_pricing_stage_with_no_price_lookup_skips() -> None:
    """If no `price_lookup` is wired, the pricing stage skips cleanly."""
    state_repo = _FakeStateRepo()
    answerer = SalesPersonaAnswerer(
        state_repo=state_repo,
        services_repo=_FakeServicesRepo(),
        openrouter=_NeverCalledOpenRouter(),
        normalizer=get_russian_normalizer(),
        clock=lambda: _NOW,
        bot_persona_getter=lambda: "Николай",
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

    result = await answerer.try_answer(question="сколько?", ctx=_ctx())

    assert result.handled is False
    assert result.metadata.get("skip_reason") == "pricing_not_configured"

"""LLM schema-violation handling for SalesPersonaAnswerer (Story 12.03).

When the LLM returns invalid JSON or a payload missing required keys, the
answerer logs `sales_llm_schema_violation` and falls through cleanly
(``handled=False``) so the message reaches the downstream RAG/HITL
answerers. It never raises into the pipeline.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest

from services.api.app.answerers import AnswerContext
from services.api.app.russian_text import get_russian_normalizer
from services.api.app.sales.sales_persona_answerer import (
    LlmSchemaViolation,
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


class _FakeServicesRepo:
    def count_active(self, *, project_id: int) -> int:  # pragma: no cover
        return 0


class _RaisingOpenRouter:
    def __init__(self, *, exc: BaseException) -> None:
        self._exc = exc
        self.calls = 0

    async def complete_json(
        self, *, system: str, user: str, model: str | None = None
    ) -> dict[str, Any]:
        self.calls += 1
        raise self._exc


_FIXED_NOW = datetime(2026, 5, 1, 13, 33, tzinfo=UTC)


def _clock() -> datetime:
    return _FIXED_NOW


def _ctx() -> AnswerContext:
    return AnswerContext(
        chat_id=7,
        customer_username="darya",
        trace_id="trace-bad-json",
        now=_FIXED_NOW,
        project_id=1,
    )


def _build(*, openrouter) -> tuple[SalesPersonaAnswerer, _FakeStateRepo]:
    state_repo = _FakeStateRepo()
    answerer = SalesPersonaAnswerer(
        state_repo=state_repo,
        services_repo=_FakeServicesRepo(),
        openrouter=openrouter,
        normalizer=get_russian_normalizer(),
        clock=_clock,
        bot_persona_getter=lambda: "Николай",
    )
    return answerer, state_repo


@pytest.mark.asyncio
async def test_schema_violation_returns_skip_and_logs(caplog) -> None:
    openrouter = _RaisingOpenRouter(exc=LlmSchemaViolation("missing next_question"))
    answerer, state_repo = _build(openrouter=openrouter)
    with caplog.at_level("WARNING"):
        result = await answerer.try_answer(
            question="Хочу прокат квадроциклов", ctx=_ctx()
        )
    assert result.handled is False
    assert result.metadata.get("skip_reason") == "llm_schema_violation"
    assert any(
        r.message == "sales_llm_schema_violation" for r in caplog.records
    )
    # No state must be persisted when the LLM call failed schema validation.
    assert state_repo.upsert_calls == []


@pytest.mark.asyncio
async def test_llm_transport_error_falls_through_to_pipeline(caplog) -> None:
    """Story 12.09: with sales as an always-on pipeline stage, a transient
    LLM transport failure must NOT crash the inbound pipeline. The answerer
    logs the error and skips so the message reaches RAG / HITL like any
    non-sales message — sales stays a thin gate, never a hard dependency."""
    openrouter = _RaisingOpenRouter(exc=RuntimeError("boom"))
    answerer, state_repo = _build(openrouter=openrouter)
    with caplog.at_level("WARNING"):
        result = await answerer.try_answer(
            question="Хочу прокат квадроциклов", ctx=_ctx()
        )
    assert result.handled is False
    assert result.metadata.get("skip_reason") == "llm_transport_error"
    assert any(
        r.message == "sales_llm_transport_error" for r in caplog.records
    )
    assert state_repo.upsert_calls == []


class _ScriptedOpenRouter:
    def __init__(self, payload: Any) -> None:
        self._payload = payload

    async def complete_json(
        self, *, system: str, user: str, model: str | None = None
    ) -> Any:
        return self._payload


@pytest.mark.asyncio
async def test_scoping_transport_error_falls_through_to_pipeline(caplog) -> None:
    """Same defensive contract as greeting: scoping must not crash the
    pipeline when the LLM transport is briefly unavailable."""
    from services.api.app.sales.intent import Intent

    state_repo = _FakeStateRepo()
    state_repo.rows[7] = {
        "chat_id": 7,
        "project_id": 1,
        "current_stage": "scoping",
        "collected_intent": Intent(dates="1 мая").to_dict(),
        "last_proposal": None,
        "last_customer_msg_at": None,
        "last_bot_msg_at": None,
    }
    openrouter = _RaisingOpenRouter(exc=RuntimeError("boom-scoping"))
    answerer = SalesPersonaAnswerer(
        state_repo=state_repo,
        services_repo=_FakeServicesRepo(),
        openrouter=openrouter,
        normalizer=get_russian_normalizer(),
        clock=_clock,
        bot_persona_getter=lambda: "Николай",
    )
    with caplog.at_level("WARNING"):
        result = await answerer.try_answer(question="нас 6", ctx=_ctx())
    assert result.handled is False
    assert result.metadata.get("skip_reason") == "llm_transport_error"
    assert any(
        getattr(r, "stage", None) == "scoping"
        and r.message == "sales_llm_transport_error"
        for r in caplog.records
    )
    assert state_repo.upsert_calls == []


@pytest.mark.asyncio
async def test_scoping_schema_violation_returns_skip(caplog) -> None:
    """Schema violation hit from the scoping stage (already in state)."""
    from services.api.app.sales.intent import Intent

    state_repo = _FakeStateRepo()
    state_repo.rows[7] = {
        "chat_id": 7,
        "project_id": 1,
        "current_stage": "scoping",
        "collected_intent": Intent(dates="1 мая").to_dict(),
        "last_proposal": None,
        "last_customer_msg_at": None,
        "last_bot_msg_at": None,
    }
    openrouter = _ScriptedOpenRouter(
        payload={"extracted_fields": {}, "next_question": None}
    )
    answerer = SalesPersonaAnswerer(
        state_repo=state_repo,
        services_repo=_FakeServicesRepo(),
        openrouter=openrouter,
        normalizer=get_russian_normalizer(),
        clock=_clock,
        bot_persona_getter=lambda: "Николай",
    )
    with caplog.at_level("WARNING"):
        result = await answerer.try_answer(question="нас 6", ctx=_ctx())
    assert result.handled is False
    assert result.metadata.get("skip_reason") == "llm_schema_violation"
    assert state_repo.upsert_calls == []


@pytest.mark.parametrize(
    "payload",
    [
        "not-a-dict",
        {"extracted_fields": "not-a-dict", "next_question": "hi"},
        {"extracted_fields": {}, "next_question": None},
        {"extracted_fields": {}},  # missing next_question
    ],
)
@pytest.mark.asyncio
async def test_greeting_schema_violation_each_shape(payload) -> None:
    openrouter = _ScriptedOpenRouter(payload=payload)
    answerer, state_repo = _build(openrouter=openrouter)
    result = await answerer.try_answer(
        question="Хочу прокат квадроциклов", ctx=_ctx()
    )
    assert result.handled is False
    assert result.metadata.get("skip_reason") == "llm_schema_violation"
    assert state_repo.upsert_calls == []

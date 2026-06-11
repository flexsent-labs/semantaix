"""Story 12.11 — graceful decline of a scoping field (no infinite re-ask)."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest

from services.api.app.answerers import AnswerContext
from services.api.app.russian_text import get_russian_normalizer
from services.api.app.sales.intent import Intent
from services.api.app.sales.sales_persona_answerer import (
    SCOPING_COMPLETE_HANDOFF_LINE,
    SCOPING_DECLINED_SENTINEL,
    SalesPersonaAnswerer,
)

_NOW = datetime(2026, 5, 30, 9, 0, tzinfo=UTC)
_CHAT_ID = 7
_PROJECT_ID = 1


class _FakeStateRepo:
    def __init__(self, *, initial: dict[str, Any]) -> None:
        self._state = initial
        self.upserts: list[dict[str, Any]] = []

    def get(self, chat_id: int) -> dict[str, Any] | None:
        return self._state

    def upsert(self, **kwargs: Any) -> None:
        self.upserts.append(kwargs)


class _NoOpServicesRepo:
    def count_active(self, *, project_id: int) -> int:  # pragma: no cover
        return 0

    def list_for_project(self, *, project_id: int) -> list[Any]:  # pragma: no cover
        return []

    def get_by_name(self, *, project_id, name):  # pragma: no cover
        return None


class _QueueOpenRouter:
    def __init__(self, *responses: dict[str, Any]) -> None:
        self._responses = list(responses)

    async def complete_json(self, *, system, user, model=None, **_kw: Any) -> dict[str, Any]:
        return self._responses.pop(0)


def _ctx() -> AnswerContext:
    return AnswerContext(
        chat_id=_CHAT_ID, customer_username="@a", trace_id="t",
        now=_NOW, project_id=_PROJECT_ID,
    )


def _state(intent: Intent) -> dict[str, Any]:
    return {
        "chat_id": _CHAT_ID, "project_id": _PROJECT_ID,
        "current_stage": "scoping",
        "collected_intent": intent.to_dict(), "last_proposal": None,
    }


def _build(intent: Intent, openrouter: Any) -> tuple[SalesPersonaAnswerer, _FakeStateRepo]:
    repo = _FakeStateRepo(initial=_state(intent))
    answerer = SalesPersonaAnswerer(
        state_repo=repo,
        services_repo=_NoOpServicesRepo(),
        openrouter=openrouter,
        normalizer=get_russian_normalizer(),
        clock=lambda: _NOW,
        bot_persona_getter=lambda: "Анна",
        calendar_settings_repo=None,  # no calendar → completion = plain handoff
        calendar_token_provider=None,
        calendar_freebusy_client=None,
        operator_chat_resolver=lambda op: 42,
    )
    return answerer, repo


@pytest.mark.asyncio
async def test_decline_last_field_completes_with_sentinel() -> None:
    # 4 of 5 fields filled, only `drivers` missing; customer declines it.
    intent = Intent(
        dates="завтра в 14:00", headcount=2, vehicle_count=1, difficulty="лёгкая"
    )
    # LLM omits the declined field (per the prompt) → nothing extracted.
    answerer, repo = _build(
        intent, _QueueOpenRouter({"extracted_fields": {}, "next_question": "?"})
    )

    result = await answerer.try_answer(question="не нужно", ctx=_ctx())

    assert result.text == SCOPING_COMPLETE_HANDOFF_LINE
    assert f"drivers={SCOPING_DECLINED_SENTINEL}" in result.metadata["escalation_context"]
    # Persisted intent records the sentinel (operator sees it).
    assert repo.upserts[-1]["collected_intent"]["drivers"] == SCOPING_DECLINED_SENTINEL


@pytest.mark.asyncio
async def test_decline_middle_field_asks_next_field() -> None:
    # Missing vehicle_count, difficulty, drivers; bot asked vehicle_count.
    intent = Intent(dates="завтра в 14:00", headcount=2)
    answerer, repo = _build(
        intent, _QueueOpenRouter({"extracted_fields": {}, "next_question": "?"})
    )

    result = await answerer.try_answer(question="не нужно", ctx=_ctx())

    # vehicle_count auto-filled; the bot now asks the NEXT field (difficulty)
    # with the deterministic fallback question, still in scoping.
    assert result.text == "Какой сложности маршрут предпочитаете?"
    assert result.metadata["stage_after"] == "scoping"
    last = repo.upserts[-1]["collected_intent"]
    assert last["vehicle_count"] == SCOPING_DECLINED_SENTINEL
    assert last["difficulty"] is None  # not yet filled


@pytest.mark.asyncio
async def test_negation_with_real_value_is_not_a_decline() -> None:
    # "нет, троих" → the LLM still extracts the count; must NOT be treated as a
    # decline (no sentinel), and the real value is kept.
    intent = Intent(dates="завтра в 14:00")
    answerer, repo = _build(
        intent,
        _QueueOpenRouter({"extracted_fields": {"headcount": 3}, "next_question": "Сколько багги?"}),
    )

    result = await answerer.try_answer(question="нет, троих", ctx=_ctx())

    assert result.text == "Сколько багги?"  # LLM's question, not a fallback
    last = repo.upserts[-1]["collected_intent"]
    assert last["headcount"] == 3
    assert last["vehicle_count"] is None  # NOT auto-filled with a sentinel

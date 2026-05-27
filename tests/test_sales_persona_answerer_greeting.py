"""Greeting-stage tests for SalesPersonaAnswerer (Story 12.03).

The greeting stage handles the very first sales-intent turn: produces a
warm Russian greeting under the configured persona, detects referrals,
asks the first scoping question, and persists state with `current_stage =
'scoping'`.
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
        trace_id="trace-greet",
        now=_FIXED_NOW,
        project_id=1,
    )


def _persona_getter() -> str:
    return "Николай"


def _build_answerer(
    *,
    state_repo: _FakeStateRepo | None = None,
    openrouter: _FakeOpenRouter | None = None,
    persona: str = "Николай",
) -> tuple[SalesPersonaAnswerer, _FakeStateRepo, _FakeOpenRouter]:
    state_repo = state_repo or _FakeStateRepo()
    openrouter = openrouter or _FakeOpenRouter()
    answerer = SalesPersonaAnswerer(
        state_repo=state_repo,
        services_repo=_FakeServicesRepo(),
        openrouter=openrouter,
        normalizer=get_russian_normalizer(),
        clock=_clock,
        bot_persona_getter=lambda: persona,
    )
    return answerer, state_repo, openrouter


@pytest.mark.asyncio
async def test_greeting_returns_handled_with_llm_text() -> None:
    answerer, _, openrouter = _build_answerer()
    openrouter.queue_response(
        {
            "extracted_fields": {},
            "next_question": "Здравствуйте! Меня зовут Николай. Какие даты вас интересуют?",
        }
    )
    result = await answerer.try_answer(
        question="Хочу прокат квадроциклов", ctx=_ctx()
    )
    assert result.handled is True
    assert result.text is not None
    assert "Николай" in result.text
    assert result.metadata.get("answerer") in (None, "sales_persona")
    assert result.metadata.get("stage_before") == "new"
    assert result.metadata.get("stage_after") == "scoping"


@pytest.mark.asyncio
async def test_greeting_persists_state_with_scoping_transition() -> None:
    answerer, state_repo, openrouter = _build_answerer()
    openrouter.queue_response(
        {
            "extracted_fields": {"dates": "1 мая"},
            "next_question": "Сколько человек поедет?",
        }
    )
    await answerer.try_answer(
        question="1 мая хочу тур, какие варианты есть?", ctx=_ctx()
    )

    assert len(state_repo.upsert_calls) == 1
    upsert = state_repo.upsert_calls[0]
    assert upsert["chat_id"] == 7
    assert upsert["project_id"] == 1
    assert upsert["current_stage"] == "scoping"
    assert upsert["collected_intent"] == Intent(dates="1 мая").to_dict()
    assert upsert["last_bot_msg_at"] == _FIXED_NOW
    assert upsert["now"] == _FIXED_NOW


@pytest.mark.asyncio
async def test_greeting_prompt_passes_persona_name_into_system() -> None:
    answerer, _, openrouter = _build_answerer(persona="Анна")
    openrouter.queue_response(
        {"extracted_fields": {}, "next_question": "Какие даты?"}
    )
    await answerer.try_answer(
        question="Хочу записаться на тур", ctx=_ctx()
    )

    assert len(openrouter.calls) == 1
    system = openrouter.calls[0]["system"]
    assert "Анна" in system
    # Never hard-code a different persona name.
    assert "Николай" not in system


@pytest.mark.asyncio
async def test_greeting_prompt_includes_referral_source_in_user_block() -> None:
    """When the customer mentions a referral, the user block sent to the LLM
    must include that context so the LLM can acknowledge it in turn one."""
    answerer, _, openrouter = _build_answerer()
    openrouter.queue_response(
        {
            "extracted_fields": {},
            "next_question": "Здравствуйте! Ваш контакт передали из Хиллс. Какие даты?",
        }
    )
    await answerer.try_answer(
        question="Здравствуйте, контакт передали из Хиллс. Хочу тур.",
        ctx=_ctx(),
    )

    assert len(openrouter.calls) == 1
    user = openrouter.calls[0]["user"]
    # The verbatim customer message reaches the LLM so it can pull the
    # referral source naturally — no separate "referral source: X" plumbing.
    assert "Хиллс" in user


@pytest.mark.asyncio
async def test_greeting_uses_persona_getter_lazily_per_call() -> None:
    """The persona name is read at call time, not bound at construction."""
    box = {"name": "Анна"}
    answerer = SalesPersonaAnswerer(
        state_repo=_FakeStateRepo(),
        services_repo=_FakeServicesRepo(),
        openrouter=_FakeOpenRouter(),
        normalizer=get_russian_normalizer(),
        clock=_clock,
        bot_persona_getter=lambda: box["name"],
    )
    # Swap the getter source between calls.
    answerer._openrouter.queue = [  # type: ignore[attr-defined]
        {"extracted_fields": {}, "next_question": "Какие даты?"},
        {"extracted_fields": {}, "next_question": "Какие даты?"},
    ]
    await answerer.try_answer(question="Хочу тур", ctx=_ctx())
    box["name"] = "Николай"
    await answerer.try_answer(question="Хочу тур", ctx=_ctx())

    calls = answerer._openrouter.calls  # type: ignore[attr-defined]
    assert "Анна" in calls[0]["system"]
    assert "Николай" in calls[1]["system"]


@pytest.mark.asyncio
async def test_greeting_logs_structured_event(caplog) -> None:
    answerer, _, openrouter = _build_answerer()
    openrouter.queue_response(
        {"extracted_fields": {}, "next_question": "Какие даты?"}
    )
    with caplog.at_level("INFO"):
        await answerer.try_answer(
            question="Хочу прокат квадроциклов", ctx=_ctx()
        )
    handled_records = [
        r for r in caplog.records if r.message == "sales_answerer_handled"
    ]
    assert handled_records, "expected at least one sales_answerer_handled log"
    record = handled_records[-1]
    assert getattr(record, "trace_id", None) == "trace-greet"
    assert getattr(record, "stage_before", None) == "new"
    assert getattr(record, "stage_after", None) == "scoping"

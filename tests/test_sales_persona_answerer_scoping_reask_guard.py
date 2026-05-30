"""Story 12.14 — scoping never re-asks the same field forever.

Real dialog (30 May 2026): the bot asked ``vehicle_count`` ("Сколько потребуется
автомобилей?"), the customer answered ``1``, the scoping LLM failed to bind the
terse reply (empty ``extracted_fields``) and re-asked the SAME question every
turn. Two guarantees here:

  * Layer A — the extractor is TOLD which field the customer is answering, so a
    bare value is bound by the LLM (asserted via the rendered system prompt).
  * Layer B — even when the LLM still returns nothing, a bare numeric reply to a
    numeric pending field is captured deterministically, so the funnel advances.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest

from services.api.app.answerers import AnswerContext
from services.api.app.russian_text import get_russian_normalizer
from services.api.app.sales.intent import Intent
from services.api.app.sales.sales_persona_answerer import (
    SalesPersonaAnswerer,
    _format_pending_instruction,
    _parse_count,
)

_NOW = datetime(2026, 5, 30, 9, 0, tzinfo=UTC)
_CHAT_ID = 7
_PROJECT_ID = 1
_CAR_QUESTION = "Сколько потребуется автомобилей?"


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


class _RecordingOpenRouter:
    """Records every call's system prompt and replays queued responses."""

    def __init__(self, *responses: dict[str, Any]) -> None:
        self._responses = list(responses)
        self.systems: list[str] = []

    async def complete_json(self, *, system, user, model=None) -> dict[str, Any]:
        self.systems.append(system)
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
        calendar_settings_repo=None,
        calendar_token_provider=None,
        calendar_freebusy_client=None,
        operator_chat_resolver=lambda op: 42,
    )
    return answerer, repo


@pytest.mark.asyncio
async def test_bare_numeric_answer_is_captured_not_reasked() -> None:
    # Bot asked vehicle_count; customer answers "1"; LLM whiffs (empty extraction)
    # and re-asks. The bare number must be captured so the funnel advances.
    intent = Intent(dates="завтра в 14:00", headcount=2)
    answerer, repo = _build(
        intent,
        _RecordingOpenRouter(
            {"extracted_fields": {}, "next_question": _CAR_QUESTION}
        ),
    )

    result = await answerer.try_answer(question="1", ctx=_ctx())

    last = repo.upserts[-1]["collected_intent"]
    assert last["vehicle_count"] == 1  # captured, not dropped
    assert result.text != _CAR_QUESTION  # advanced — does NOT re-ask
    assert result.metadata["stage_after"] == "scoping"


@pytest.mark.asyncio
async def test_scoping_prompt_tells_llm_which_field_is_pending() -> None:
    # Layer A: the extractor must explicitly name the field being answered, so a
    # bare reply is bound. (The plain missing-list bullet is not enough.)
    intent = Intent(dates="завтра в 14:00", headcount=2)
    openrouter = _RecordingOpenRouter(
        {"extracted_fields": {"vehicle_count": 1}, "next_question": "?"}
    )
    answerer, _repo = _build(intent, openrouter)

    await answerer.try_answer(question="1", ctx=_ctx())

    assert any(
        "отвечает на вопрос" in system and "vehicle_count" in system
        for system in openrouter.systems
    )


@pytest.mark.asyncio
async def test_numeric_capture_does_not_apply_to_a_text_field() -> None:
    # Pending field is a TEXT field (difficulty). A stray digit must NOT be
    # force-bound as its value — deterministic capture is numeric-only.
    intent = Intent(dates="завтра в 14:00", headcount=2, vehicle_count=1)
    answerer, repo = _build(
        intent,
        _RecordingOpenRouter({"extracted_fields": {}, "next_question": "?"}),
    )

    await answerer.try_answer(question="3", ctx=_ctx())

    last = repo.upserts[-1]["collected_intent"]
    assert last["difficulty"] is None  # not clobbered with the digit


def test_parse_count_extracts_first_integer() -> None:
    assert _parse_count("1") == 1
    assert _parse_count("нужно 2 машины") == 2
    assert _parse_count("троих") is None


def test_pending_instruction_is_empty_when_nothing_missing() -> None:
    complete = Intent(
        dates="x", headcount=1, vehicle_count=1, difficulty="лёгкая", drivers="1"
    )
    assert _format_pending_instruction(complete) == ""
    assert "headcount" in _format_pending_instruction(Intent(dates="x"))

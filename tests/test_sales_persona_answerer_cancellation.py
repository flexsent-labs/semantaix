"""Story 12.27 — a cancellation request routes to a human, not the funnel.

Before this story, "хочу отменить запись" was pulled INTO the booking funnel
(the positive seeds бронь/запись make is_sales_intent True) and a bare
"можно отменить?" fell through to generic RAG/HITL. Both surfaced the canned
"передам коллегам" handoff. Now a cancellation is caught first and escalated
with a cancellation-specific line + HITL reason, and the +24h nudge is
suppressed.

Live bug (багги, 31 May 2026, "Анна Иванова").
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest

from services.api.app.answerers import AnswerContext
from services.api.app.russian_text import get_russian_normalizer
from services.api.app.sales.intent import Intent
from services.api.app.sales.sales_persona_answerer import (
    CANCELLATION_HANDOFF_LINE,
    HITL_REASON_CANCELLATION,
    RESPONSE_MODE_SALES_ESCALATION,
    STAGE_CLOSING,
    STAGE_SCOPING,
    SalesPersonaAnswerer,
)

_NOW = datetime(2026, 5, 31, 9, 0, tzinfo=UTC)
_CHAT_ID = 7
_PROJECT_ID = 1


class _FakeStateRepo:
    def __init__(self, *, initial: dict[str, Any] | None = None) -> None:
        self.rows: dict[int, dict[str, Any]] = (
            {_CHAT_ID: initial} if initial is not None else {}
        )
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
        self, *, system: str, user: str, model: str | None = None, **_kw: Any
    ) -> dict[str, Any]:
        self.calls.append({"system": system, "user": user, "model": model})
        if not self.queue:
            raise AssertionError("LLM called without a queued payload")
        return self.queue.pop(0)


class _FakeFollowupRepo:
    def __init__(self) -> None:
        self.enqueue_calls = 0

    def enqueue(self, *, chat_id, project_id, fire_at, now) -> None:
        self.enqueue_calls += 1


def _ctx() -> AnswerContext:
    return AnswerContext(
        chat_id=_CHAT_ID,
        customer_username="anna",
        trace_id="trace-cancel",
        now=_NOW,
        project_id=_PROJECT_ID,
    )


def _scoping_state(intent: Intent) -> dict[str, Any]:
    return {
        "chat_id": _CHAT_ID,
        "project_id": _PROJECT_ID,
        "current_stage": STAGE_SCOPING,
        "collected_intent": intent.to_dict(),
        "last_proposal": None,
    }


def _build(*, state: dict[str, Any] | None = None):
    state_repo = _FakeStateRepo(initial=state)
    openrouter = _FakeOpenRouter()
    followup_repo = _FakeFollowupRepo()
    answerer = SalesPersonaAnswerer(
        state_repo=state_repo,
        services_repo=_FakeServicesRepo(),
        openrouter=openrouter,
        normalizer=get_russian_normalizer(),
        clock=lambda: _NOW,
        bot_persona_getter=lambda: "Анна",
        followup_repo=followup_repo,
    )
    return answerer, state_repo, openrouter, followup_repo


@pytest.mark.asyncio
async def test_cancellation_with_booking_noun_routes_to_human() -> None:
    """"хочу отменить запись" escalates instead of entering scoping."""
    answerer, _state_repo, openrouter, _ = _build(state=None)
    result = await answerer.try_answer(
        question="хочу отменить запись", ctx=_ctx()
    )
    assert result.handled is True
    assert result.text == CANCELLATION_HANDOFF_LINE
    assert result.response_mode == RESPONSE_MODE_SALES_ESCALATION
    assert result.metadata["escalate"] is True
    assert result.metadata["hitl_reason"] == HITL_REASON_CANCELLATION
    assert result.metadata["sales_turn_kind"] == "cancellation_request"
    # Caught before the greeting LLM — no extraction call was made.
    assert openrouter.calls == []


@pytest.mark.asyncio
async def test_bare_cancellation_routes_to_human() -> None:
    """"можно отменить?" (no booking noun → is_sales_intent False) still escalates."""
    answerer, _state_repo, _openrouter, _ = _build(state=None)
    result = await answerer.try_answer(question="можно отменить?", ctx=_ctx())
    assert result.response_mode == RESPONSE_MODE_SALES_ESCALATION
    assert result.metadata["hitl_reason"] == HITL_REASON_CANCELLATION
    assert result.text == CANCELLATION_HANDOFF_LINE


@pytest.mark.asyncio
async def test_cancellation_mid_scoping_routes_to_human() -> None:
    """A cancellation during scoping is not swallowed by the funnel."""
    answerer, state_repo, _openrouter, _ = _build(
        state=_scoping_state(Intent(headcount=2))
    )
    result = await answerer.try_answer(question="отмените бронь", ctx=_ctx())
    assert result.response_mode == RESPONSE_MODE_SALES_ESCALATION
    assert result.metadata["hitl_reason"] == HITL_REASON_CANCELLATION
    assert result.metadata["stage_before"] == STAGE_SCOPING
    # Funnel parked at the terminal closing stage (a human owns it now).
    assert state_repo.upsert_calls[-1]["current_stage"] == STAGE_CLOSING


@pytest.mark.asyncio
async def test_cancellation_operator_context_is_tagged() -> None:
    """The operator DM context marks this as a cancellation request."""
    answerer, _state_repo, _openrouter, _ = _build(state=None)
    result = await answerer.try_answer(question="хочу отменить", ctx=_ctx())
    assert "отмен" in result.metadata["escalation_context"].lower()


@pytest.mark.asyncio
async def test_cancellation_suppresses_followup_nudge() -> None:
    """No +24h "still thinking about booking?" nudge after a cancellation."""
    answerer, _state_repo, _openrouter, followup_repo = _build(state=None)
    await answerer.try_answer(question="хочу отменить запись", ctx=_ctx())
    assert followup_repo.enqueue_calls == 0


@pytest.mark.asyncio
async def test_normal_greeting_still_enqueues_followup() -> None:
    """Control: a non-cancellation booking turn still schedules the nudge."""
    answerer, _state_repo, openrouter, followup_repo = _build(state=None)
    openrouter.queue_response(
        {
            "extracted_fields": {"dates": "завтра"},
            "next_question": "Сколько человек поедет?",
        }
    )
    await answerer.try_answer(question="хочу багги завтра", ctx=_ctx())
    assert followup_repo.enqueue_calls == 1

"""Story 12.23 — a returning customer in terminal ``closing`` who sends a fresh
booking intent restarts the sales funnel instead of getting the sticky handoff.

Live bug (Артур, багги, 31 May 2026): the chat was parked in ``closing`` from a
prior conversation. The opener "хочу забронировать багги завтра в 14:00" routed
straight to ``_handle_closing`` (sticky) and replied "Передам коллегам для
подтверждения, на связи." — the funnel (and the 12.22 alternative-slot offer)
never ran. These tests pin the new behaviour:

* ``closing`` + a fresh sales intent → ``_handle_greeting`` (→ scoping), with the
  stale ``last_proposal`` cleared and a clean ``Intent``;
* ``closing`` + a non-sales reply ("спасибо") → unchanged sticky handoff.

Mid-funnel stages are intentionally NOT covered here — only terminal ``closing``
restarts (see the plan / Story 12.23 decision).
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest

from services.api.app.answerers import AnswerContext
from services.api.app.russian_text import get_russian_normalizer
from services.api.app.sales.intent import Intent
from services.api.app.sales.sales_persona_answerer import (
    CLOSING_HANDOFF_LINE,
    HITL_REASON_CLOSING_HANDOFF,
    STAGE_CLOSING,
    STAGE_SCOPING,
    SalesPersonaAnswerer,
)

_FIXED_NOW = datetime(2026, 5, 31, 9, 0, tzinfo=UTC)
_CHAT_ID = 5658965359
_PROJECT_ID = 1


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
    def count_active(self, *, project_id: int) -> int:  # pragma: no cover
        return 0


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


def _clock() -> datetime:
    return _FIXED_NOW


def _ctx() -> AnswerContext:
    return AnswerContext(
        chat_id=_CHAT_ID,
        customer_username="@artur",
        trace_id="trace-closing-restart",
        now=_FIXED_NOW,
        project_id=_PROJECT_ID,
    )


def _build(
    *, openrouter: _FakeOpenRouter | None = None
) -> tuple[SalesPersonaAnswerer, _FakeStateRepo, _FakeOpenRouter]:
    state_repo = _FakeStateRepo()
    openrouter = openrouter or _FakeOpenRouter()
    answerer = SalesPersonaAnswerer(
        state_repo=state_repo,
        services_repo=_FakeServicesRepo(),
        openrouter=openrouter,
        normalizer=get_russian_normalizer(),
        clock=_clock,
        bot_persona_getter=lambda: "Анна",
    )
    return answerer, state_repo, openrouter


def _seed_closing(state_repo: _FakeStateRepo) -> None:
    """A chat already handed off in ``closing`` with a stale offered slot."""
    state_repo.rows[_CHAT_ID] = {
        "chat_id": _CHAT_ID,
        "project_id": _PROJECT_ID,
        "current_stage": STAGE_CLOSING,
        "collected_intent": Intent(
            dates="завтра в 14:00",
            headcount=2,
            vehicle_count=1,
        ).to_dict(),
        "last_proposal": {"alternative_iso": "2026-06-01T08:00:00+03:00"},
        "last_customer_msg_at": None,
        "last_bot_msg_at": None,
    }


@pytest.mark.asyncio
async def test_closing_with_new_booking_intent_restarts_funnel() -> None:
    answerer, state_repo, openrouter = _build()
    _seed_closing(state_repo)
    # Greeting re-runs the LLM extraction for the fresh opener.
    openrouter.queue_response(
        {
            "extracted_fields": {"dates": "завтра в 14:00"},
            "next_question": "Сколько человек поедет?",
        }
    )

    result = await answerer.try_answer(
        question="хочу забронировать багги завтра в 14:00", ctx=_ctx()
    )

    # Routed through greeting → scoping, NOT the sticky closing handoff.
    assert result.handled is True
    assert result.text == "Сколько человек поедет?"
    assert result.text != CLOSING_HANDOFF_LINE
    assert result.metadata.get("stage_after") == STAGE_SCOPING
    assert result.metadata.get("hitl_reason") != HITL_REASON_CLOSING_HANDOFF
    # Fresh funnel: a clean Intent (only the LLM-extracted opener field) and the
    # stale offered slot cleared so pitching cannot resurrect it.
    upsert = state_repo.upsert_calls[-1]
    assert upsert["current_stage"] == STAGE_SCOPING
    assert upsert["collected_intent"] == Intent(dates="завтра в 14:00").to_dict()
    assert upsert["last_proposal"] is None


@pytest.mark.asyncio
async def test_closing_with_non_sales_reply_stays_sticky() -> None:
    # A non-booking, non-gratitude reply in closing keeps the existing sticky
    # handoff (the human still owns the conversation) — the LLM is never called.
    # (A pure thank-you is handled separately as gratitude — Story 16/R16-4.)
    answerer, state_repo, openrouter = _build()
    _seed_closing(state_repo)

    result = await answerer.try_answer(question="понятно", ctx=_ctx())

    assert result.text == CLOSING_HANDOFF_LINE
    assert result.metadata["sales_turn_kind"] == "closing_followup"
    assert result.metadata["stage_after"] == STAGE_CLOSING
    assert result.metadata["hitl_reason"] == HITL_REASON_CLOSING_HANDOFF
    assert openrouter.calls == []  # deterministic; no greeting LLM call
    assert state_repo.upsert_calls[-1]["current_stage"] == STAGE_CLOSING

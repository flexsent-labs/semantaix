"""Story 12.34 (D7) — decline out-of-scope requests, don't accept them as bookings.

`ScopeGuardAnswerer` runs last in the pipeline, so an out-of-scope turn in an
active sales funnel was claimed by `_handle_scoping`/`_handle_pitching` and got
the booking-acceptance line. A conservative `is_out_of_scope` detector, gated in
`_dispatch` only when the turn is NOT a sales intent, declines + redirects in any
stage without disturbing the funnel.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest

from services.api.app.answerers import AnswerContext
from services.api.app.russian_text import get_russian_normalizer
from services.api.app.sales.intent import Intent
from services.api.app.sales.out_of_scope import is_out_of_scope
from services.api.app.sales.sales_persona_answerer import (
    OUT_OF_SCOPE_DECLINE_LINE,
    SCOPING_COMPLETE_HANDOFF_LINE,
    STAGE_SCOPING,
    SalesPersonaAnswerer,
)

_NOW = datetime(2026, 6, 1, 9, 0, tzinfo=UTC)
_CHAT_ID = 7
_PROJECT_ID = 1
_NORM = get_russian_normalizer()


# --- detector unit -----------------------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        "посоветуйте хороший ресторан рядом с водопадом",
        "а есть кафе поблизости?",
        "какой отель посоветуете?",
        "нужна гостиница на ночь",
        # Story 12.93 (round-27 R27-1) — non-offered vehicles / activities.
        "А на вертолёте у вас полетать можно?",
        "можно яхту арендовать?",
        "а катер есть?",
        "хочу полетать на параплане",
    ],
)
def test_is_out_of_scope_positives(text: str) -> None:
    assert is_out_of_scope(text, normalizer=_NORM) is True


@pytest.mark.parametrize(
    "text",
    [
        "хочу забронировать багги завтра",  # in-scope booking
        "двое",  # a field answer
        "нас четверо, одна багги",  # field answers
        "отменить бронь",  # cancellation (its own intent)
        "хочу на квадроцикле покататься",  # quad IS offered — not out of scope
        "",  # empty
    ],
)
def test_is_out_of_scope_negatives(text: str) -> None:
    assert is_out_of_scope(text, normalizer=_NORM) is False


# --- dispatch behaviour ------------------------------------------------------


class _FakeStateRepo:
    def __init__(self, *, initial: dict[str, Any] | None = None) -> None:
        self._state = initial
        self.upserts: list[dict[str, Any]] = []

    def get(self, chat_id: int) -> dict[str, Any] | None:
        return self._state

    def upsert(self, **kwargs: Any) -> None:
        self.upserts.append(kwargs)
        self._state = dict(kwargs)


class _NoOpServicesRepo:
    def count_active(self, *, project_id: int) -> int:  # pragma: no cover
        return 0

    def list_for_project(self, *, project_id: int) -> list[Any]:  # pragma: no cover
        return []

    def get_by_name(self, *, project_id: int, name: str) -> Any | None:  # pragma: no cover
        return None


class _UnusedOpenRouter:
    async def complete_json(self, *, system, user, model=None):  # pragma: no cover
        raise AssertionError("LLM must not be called for an out-of-scope decline")


class _QueueOpenRouter:
    def __init__(self, *responses: dict[str, Any]) -> None:
        self._responses = list(responses)

    async def complete_json(self, *, system, user, model=None) -> dict[str, Any]:
        return self._responses.pop(0)


def _ctx() -> AnswerContext:
    return AnswerContext(
        chat_id=_CHAT_ID,
        customer_username="@artur",
        trace_id="trc",
        now=_NOW,
        project_id=_PROJECT_ID,
    )


def _build(
    *, state: dict[str, Any] | None, openrouter: Any | None = None
) -> tuple[SalesPersonaAnswerer, _FakeStateRepo]:
    repo = _FakeStateRepo(initial=state)
    answerer = SalesPersonaAnswerer(
        state_repo=repo,
        services_repo=_NoOpServicesRepo(),
        openrouter=openrouter if openrouter is not None else _UnusedOpenRouter(),
        normalizer=_NORM,
        clock=lambda: _NOW,
        bot_persona_getter=lambda: "Анна",
    )
    return answerer, repo


@pytest.mark.asyncio
async def test_out_of_scope_clean_state_declines() -> None:
    answerer, repo = _build(state=None)
    result = await answerer.try_answer(
        question="посоветуйте хороший ресторан рядом с водопадом", ctx=_ctx()
    )
    assert result.handled is True
    assert result.text == OUT_OF_SCOPE_DECLINE_LINE
    assert result.text != SCOPING_COMPLETE_HANDOFF_LINE
    assert result.metadata.get("escalate") is not True
    assert result.metadata.get("suppress_followup") is True
    assert repo.upserts == []  # no funnel state created


@pytest.mark.asyncio
async def test_out_of_scope_helicopter_declines_not_handoff() -> None:
    # Story 12.93 (round-27 R27-1) — a non-offered vehicle ("на вертолёте?")
    # is declined + redirected, never the booking handoff.
    answerer, repo = _build(state=None)
    result = await answerer.try_answer(
        question="А на вертолёте у вас полетать можно?", ctx=_ctx()
    )
    assert result.text == OUT_OF_SCOPE_DECLINE_LINE
    assert result.text != SCOPING_COMPLETE_HANDOFF_LINE
    assert result.metadata.get("escalate") is not True
    assert repo.upserts == []  # no booking funnel created


@pytest.mark.asyncio
async def test_out_of_scope_mid_scoping_declines_without_disturbing_funnel() -> None:
    state = {
        "chat_id": _CHAT_ID,
        "project_id": _PROJECT_ID,
        "current_stage": STAGE_SCOPING,
        "collected_intent": Intent(headcount=2).to_dict(),
        "last_proposal": None,
    }
    answerer, repo = _build(state=state)
    result = await answerer.try_answer(question="а есть кафе рядом?", ctx=_ctx())
    assert result.text == OUT_OF_SCOPE_DECLINE_LINE
    # The booking funnel is parked, not mutated.
    assert repo.upserts == []


@pytest.mark.asyncio
async def test_buggy_request_with_offtopic_word_is_not_declined() -> None:
    # A sales intent in the same turn → handled by the funnel (greeting LLM),
    # NOT declined.
    openrouter = _QueueOpenRouter(
        {"extracted_fields": {}, "next_question": "Сколько человек поедет?"}
    )
    answerer, _ = _build(state=None, openrouter=openrouter)
    result = await answerer.try_answer(
        question="хочу забронировать багги, а рядом есть ресторан?", ctx=_ctx()
    )
    assert result.text != OUT_OF_SCOPE_DECLINE_LINE


# --- Story 12.98 (round-28 D1): «хочу» on non-offered service → decline ------


@pytest.mark.asyncio
async def test_хочу_на_вертолёте_declined_not_mixed_intent() -> None:
    # «Хочу на вертолёте полетать» — is_sales_intent=True (from «хочу») but the
    # requested service (вертолёт) is not offered. The current gate
    # ``is_out_of_scope AND NOT is_sales_intent`` passes through this message
    # because is_sales_intent=True. After fix: gate checks _mentions_offered_service
    # instead, declining because вертолёт is not an offered service.
    openrouter = _QueueOpenRouter(
        {"extracted_fields": {}, "next_question": "Хочу помочь вам!"}
    )
    answerer, repo = _build(state=None, openrouter=openrouter)
    result = await answerer.try_answer(
        question="Хочу на вертолёте полетать", ctx=_ctx()
    )
    assert result.text == OUT_OF_SCOPE_DECLINE_LINE, (
        "«хочу на вертолёте» must be declined (not an offered service) "
        "even though is_sales_intent=True"
    )
    assert result.metadata.get("escalate") is not True
    assert repo.upserts == []  # no funnel state created


@pytest.mark.asyncio
async def test_хочу_на_параплане_declined() -> None:
    # Another non-offered «хочу» variant — same pattern.
    openrouter = _QueueOpenRouter(
        {"extracted_fields": {}, "next_question": "ок"}
    )
    answerer, repo = _build(state=None, openrouter=openrouter)
    result = await answerer.try_answer(
        question="хочу полетать на параплане", ctx=_ctx()
    )
    assert result.text == OUT_OF_SCOPE_DECLINE_LINE
    assert repo.upserts == []


@pytest.mark.asyncio
async def test_хочу_на_багги_not_declined() -> None:
    # «хочу на багги» — offered service → NOT declined (enters funnel).
    openrouter = _QueueOpenRouter(
        {"extracted_fields": {"service": "багги"}, "next_question": "На какую дату?"}
    )
    answerer, _ = _build(state=None, openrouter=openrouter)
    result = await answerer.try_answer(
        question="хочу на багги", ctx=_ctx()
    )
    assert result.text != OUT_OF_SCOPE_DECLINE_LINE, (
        "«хочу на багги» is an offered service → must enter funnel, not be declined"
    )

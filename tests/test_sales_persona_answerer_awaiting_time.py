"""Story 12.10 — ask the customer for a date/time when a booking has none.

When scoping completes (or a pitching turn arrives) with a calendar-actionable
booking but no concrete time, the answerer asks for the date+time and parks in
``awaiting_time``. The follow-up turn re-extracts the reply and runs the slot
check (confirm / offer alternative); if the customer still gives no time it
hands off — never a second ask.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime
from typing import Any
from zoneinfo import ZoneInfo

import pytest

from services.api.app.answerers import AnswerContext
from services.api.app.calendar.calendar_client import BusyInterval, FreeBusy
from services.api.app.calendar.settings_repository import ServiceRule
from services.api.app.russian_text import get_russian_normalizer
from services.api.app.sales.intent import Intent
from services.api.app.sales.sales_persona_answerer import (
    ASK_FOR_TIME_LINE,
    ASK_FOR_TIME_LINE_EN,
    MATERIAL_DISPATCH_FALLBACK_LINE,
    PITCHING_ACCEPT_CONFIRM_LINE_EN,
    SCOPING_COMPLETE_HANDOFF_LINE,
    SLOT_BUSY_LINE,
    SLOT_BUSY_LINE_EN,
    SLOT_FREE_HANDOFF_LINE,
    SLOT_FREE_HANDOFF_LINE_EN,
    STAGE_AWAITING_TIME,
    SalesPersonaAnswerer,
)

_NOW = datetime(2026, 5, 29, 9, 0, tzinfo=UTC)  # 12:00 Moscow, Fri 29 May
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


class _Settings:
    def __init__(self) -> None:
        self.calendar_operator = "@op"
        self.project_timezone = "Europe/Moscow"
        self.lookahead_days = 60


class _FakeCalSettings:
    def __init__(self, *, enabled: bool = True, rules=None) -> None:
        self._enabled = enabled
        self._settings = _Settings()
        self._rules = rules if rules is not None else [_rule()]

    def is_enabled(self, project_id: int) -> bool:
        return self._enabled

    def get(self, project_id: int):
        return self._settings

    def list_service_rules(self, project_id: int) -> list[ServiceRule]:
        return self._rules


def _rule() -> ServiceRule:
    week = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
    return ServiceRule(
        id=1,
        project_id=_PROJECT_ID,
        name="Багги",
        duration_minutes=60,
        working_hours={day: [["09:00", "20:00"]] for day in week},
        service_days=week,
        date_exceptions=[],
        updated_at=None,
    )


class _TokenProvider:
    async def get_access_token(
        self, project_id, operator, *, operator_chat_id, trace_id
    ) -> str:
        return "tok"


class _FreeBusy:
    def __init__(self, *, busy: tuple[BusyInterval, ...] = ()) -> None:
        self._busy = busy

    async def query_busy(
        self, *, access_token, time_min, time_max, trace_id, calendar_id="primary"
    ) -> FreeBusy:
        return FreeBusy(calendar_id="primary", busy=self._busy)


class _QueueOpenRouter:
    """Returns queued payloads; the awaiting-time turn re-extracts via this."""

    def __init__(self, *responses: dict[str, Any]) -> None:
        self._responses = list(responses)
        self.calls = 0

    async def complete_json(self, *, system, user, model=None) -> dict[str, Any]:
        self.calls += 1
        return self._responses.pop(0)


def _ctx() -> AnswerContext:
    return AnswerContext(
        chat_id=_CHAT_ID,
        customer_username="@artur",
        trace_id="trc",
        now=_NOW,
        project_id=_PROJECT_ID,
    )


def _state(*, dates: str | None, stage: str = STAGE_AWAITING_TIME) -> dict[str, Any]:
    return {
        "chat_id": _CHAT_ID,
        "project_id": _PROJECT_ID,
        "current_stage": stage,
        "collected_intent": Intent(
            dates=dates,
            headcount=2,
            vehicle_count=1,
            difficulty="новичок",
            drivers="сами",
        ).to_dict(),
        "last_proposal": None,
    }


def _build(
    *,
    state: dict[str, Any],
    openrouter: Any,
    cal_settings: _FakeCalSettings | None = None,
    freebusy: Any | None = None,
) -> tuple[SalesPersonaAnswerer, _FakeStateRepo]:
    state_repo = _FakeStateRepo(initial=state)
    answerer = SalesPersonaAnswerer(
        state_repo=state_repo,
        services_repo=_NoOpServicesRepo(),
        openrouter=openrouter,
        normalizer=get_russian_normalizer(),
        clock=lambda: _NOW,
        bot_persona_getter=lambda: "Анна",
        calendar_settings_repo=cal_settings,
        calendar_token_provider=_TokenProvider(),
        calendar_freebusy_client=freebusy if freebusy is not None else _FreeBusy(),
        operator_chat_resolver=lambda op: 42,
    )
    return answerer, state_repo


# --- awaiting_time follow-up turns ------------------------------------------


@pytest.mark.asyncio
async def test_awaiting_time_reply_with_time_free_confirms() -> None:
    openrouter = _QueueOpenRouter(
        {"extracted_fields": {"dates": "завтра в 14:00"}, "next_question": "ок"}
    )
    answerer, _ = _build(
        state=_state(dates=None),
        openrouter=openrouter,
        cal_settings=_FakeCalSettings(),
        freebusy=_FreeBusy(busy=()),
    )
    result = await answerer.try_answer(question="завтра в 14:00", ctx=_ctx())
    assert result.text == SLOT_FREE_HANDOFF_LINE
    assert openrouter.calls == 1  # the reply WAS re-extracted


@pytest.mark.asyncio
async def test_awaiting_time_reply_with_time_busy_offers_alternative() -> None:
    busy = (
        BusyInterval(
            start=datetime(2026, 5, 30, 13, 0, tzinfo=ZoneInfo("Europe/Moscow")),
            end=datetime(2026, 5, 30, 15, 0, tzinfo=ZoneInfo("Europe/Moscow")),
        ),
    )
    openrouter = _QueueOpenRouter(
        {"extracted_fields": {"dates": "завтра в 14:00"}, "next_question": "ок"}
    )
    answerer, _ = _build(
        state=_state(dates=None),
        openrouter=openrouter,
        cal_settings=_FakeCalSettings(),
        freebusy=_FreeBusy(busy=busy),
    )
    result = await answerer.try_answer(question="завтра в 14:00", ctx=_ctx())
    text = result.text or ""
    assert SLOT_BUSY_LINE in text
    assert "Ближайшее свободное время" in text
    # Story 12.22 — offering an alternative defers escalation until the
    # customer responds, even when reached via the awaiting_time path.
    assert result.response_mode is None
    assert result.metadata.get("escalate") is not True


@pytest.mark.asyncio
async def test_awaiting_time_still_no_time_hands_off_without_reasking() -> None:
    # Customer's reply carries no concrete time → the LLM extracts nothing
    # useful → we hand off (a human picks up). We do NOT ask a second time.
    openrouter = _QueueOpenRouter(
        {"extracted_fields": {}, "next_question": "Когда вам удобно?"}
    )
    answerer, _ = _build(
        state=_state(dates=None),
        openrouter=openrouter,
        cal_settings=_FakeCalSettings(),
    )
    result = await answerer.try_answer(question="не знаю пока", ctx=_ctx())
    assert result.text == SCOPING_COMPLETE_HANDOFF_LINE
    assert result.text != ASK_FOR_TIME_LINE


@pytest.mark.asyncio
async def test_awaiting_time_vague_date_still_no_time_hands_off() -> None:
    # Reply leaves a date-ish but time-less ``dates`` ("скоро") → the slot check
    # can't pin a concrete start → hand off (no second ask).
    openrouter = _QueueOpenRouter(
        {"extracted_fields": {}, "next_question": "Когда вам удобно?"}
    )
    answerer, _ = _build(
        state=_state(dates="скоро"),
        openrouter=openrouter,
        cal_settings=_FakeCalSettings(),
    )
    result = await answerer.try_answer(question="скоро как-нибудь", ctx=_ctx())
    assert result.text == SCOPING_COMPLETE_HANDOFF_LINE


@pytest.mark.asyncio
async def test_awaiting_time_llm_schema_violation_skips() -> None:
    # A malformed LLM payload → _extract_and_merge returns a skip; the turn
    # falls through to the rest of the pipeline (handled is False).
    openrouter = _QueueOpenRouter({"not": "a valid payload"})
    answerer, _ = _build(
        state=_state(dates=None),
        openrouter=openrouter,
        cal_settings=_FakeCalSettings(),
    )
    result = await answerer.try_answer(question="что-то", ctx=_ctx())
    assert result.handled is False


# --- the ask itself ---------------------------------------------------------


@pytest.mark.asyncio
async def test_ask_for_time_appends_media_fallback_line() -> None:
    # When the tour-preview media dispatch failed earlier in the same turn, the
    # ask still surfaces the textual media fallback (never a silent drop).
    answerer, _ = _build(
        state=_state(dates=None, stage="scoping"),
        openrouter=_QueueOpenRouter(),
        cal_settings=_FakeCalSettings(),
    )
    result = await answerer._ask_for_time(
        ctx=_ctx(),
        intent=Intent(dates=None),
        stage_before="scoping",
        base_metadata={},
        dispatch_fallback=True,
    )
    text = result.text or ""
    assert ASK_FOR_TIME_LINE in text
    assert MATERIAL_DISPATCH_FALLBACK_LINE in text


# --- gated off: not calendar-actionable → no ask ----------------------------


@pytest.mark.asyncio
async def test_no_time_calendar_disabled_hands_off_not_ask() -> None:
    # Calendar disabled → asking for a time is pointless (we can't check it) →
    # unchanged generic hand off, NOT the ask.
    answerer, _ = _build(
        state=_state(dates=None, stage="pitching"),
        openrouter=_QueueOpenRouter(),
        cal_settings=_FakeCalSettings(enabled=False),
    )
    result = await answerer.try_answer(question="ну что?", ctx=_ctx())
    assert result.text == SCOPING_COMPLETE_HANDOFF_LINE


# --- Story 12.47 (round-10 N3) — deterministic lines mirror the turn language -
# The LLM lines already mirror the customer's language; these guard that the
# DETERMINISTIC constants (ask-for-time, busy/free verdict, accept confirmation)
# do too, on every turn — an English thread no longer reverts to Russian the
# moment a fixed line fires.


def _en_ctx() -> AnswerContext:
    return replace(_ctx(), language="en")


@pytest.mark.asyncio
async def test_awaiting_time_busy_en_localizes_verdict_and_alternative() -> None:
    busy = (
        BusyInterval(
            start=datetime(2026, 5, 30, 13, 0, tzinfo=ZoneInfo("Europe/Moscow")),
            end=datetime(2026, 5, 30, 15, 0, tzinfo=ZoneInfo("Europe/Moscow")),
        ),
    )
    openrouter = _QueueOpenRouter(
        {"extracted_fields": {"dates": "завтра в 14:00"}, "next_question": "ок"}
    )
    answerer, _ = _build(
        state=_state(dates=None),
        openrouter=openrouter,
        cal_settings=_FakeCalSettings(),
        freebusy=_FreeBusy(busy=busy),
    )
    # English turn → English verdict + English nearest-free tail (May 30, 09:00).
    result = await answerer.try_answer(question="Tomorrow at 14:00 then.", ctx=_en_ctx())
    text = result.text or ""
    assert SLOT_BUSY_LINE_EN in text
    assert "The nearest available time is" in text
    assert "May 30" in text and "09:00" in text
    # No Russian leaks through on an English turn.
    assert SLOT_BUSY_LINE not in text
    assert "Ближайшее свободное" not in text


@pytest.mark.asyncio
async def test_awaiting_time_free_en_localizes_handoff() -> None:
    openrouter = _QueueOpenRouter(
        {"extracted_fields": {"dates": "завтра в 14:00"}, "next_question": "ок"}
    )
    answerer, _ = _build(
        state=_state(dates=None),
        openrouter=openrouter,
        cal_settings=_FakeCalSettings(),
        freebusy=_FreeBusy(busy=()),
    )
    result = await answerer.try_answer(question="Tomorrow at 14:00 then.", ctx=_en_ctx())
    assert result.text == SLOT_FREE_HANDOFF_LINE_EN


@pytest.mark.asyncio
async def test_awaiting_time_busy_ru_unchanged_regression() -> None:
    # A Russian turn keeps the Russian verdict byte-identical (no regression).
    busy = (
        BusyInterval(
            start=datetime(2026, 5, 30, 13, 0, tzinfo=ZoneInfo("Europe/Moscow")),
            end=datetime(2026, 5, 30, 15, 0, tzinfo=ZoneInfo("Europe/Moscow")),
        ),
    )
    openrouter = _QueueOpenRouter(
        {"extracted_fields": {"dates": "завтра в 14:00"}, "next_question": "ок"}
    )
    answerer, _ = _build(
        state=_state(dates=None),
        openrouter=openrouter,
        cal_settings=_FakeCalSettings(),
        freebusy=_FreeBusy(busy=busy),
    )
    result = await answerer.try_answer(question="завтра в 14:00", ctx=_ctx())
    text = result.text or ""
    assert SLOT_BUSY_LINE in text
    assert "Ближайшее свободное время" in text
    assert SLOT_BUSY_LINE_EN not in text


@pytest.mark.asyncio
async def test_ask_for_time_en_localizes() -> None:
    # The round-10 N3 culprit: the ask-for-time constant was Russian-only.
    answerer, _ = _build(
        state=_state(dates=None, stage="scoping"),
        openrouter=_QueueOpenRouter(),
        cal_settings=_FakeCalSettings(),
    )
    result = await answerer._ask_for_time(
        ctx=_en_ctx(),
        intent=Intent(dates=None),
        stage_before="scoping",
        base_metadata={},
        dispatch_fallback=False,
    )
    assert result.text == ASK_FOR_TIME_LINE_EN
    assert ASK_FOR_TIME_LINE not in (result.text or "")


@pytest.mark.asyncio
async def test_confirm_slot_en_names_slot_in_english() -> None:
    answerer, _ = _build(
        state=_state(dates=None, stage="pitching"),
        openrouter=_QueueOpenRouter(),
        cal_settings=_FakeCalSettings(),
    )
    slot_dt = datetime(2026, 5, 30, 8, 0, tzinfo=ZoneInfo("Europe/Moscow"))
    result = await answerer._confirm_slot(
        ctx=_en_ctx(), intent=Intent(dates="завтра в 8"), slot_dt=slot_dt
    )
    assert result.text == PITCHING_ACCEPT_CONFIRM_LINE_EN.format(
        day_month="May 30", time="08:00"
    )
    assert "мая" not in (result.text or "")

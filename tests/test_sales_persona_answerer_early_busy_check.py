"""Story 12.25 — early busy-check during scoping.

The bot must verify the customer's stated time the moment a concrete
``requested_start`` first parses out of ``intent.dates`` — at the greeting
opener or any mid-scoping turn that changes the time — and intercept the
funnel when the slot is busy (propose alternative, park in pitching).
Available / not-connected / error / calendar-disabled / multi-service all
fall through silently so the existing scoping flow runs unchanged.

Live bug (багги, 31 May 2026 13:36, Artur Yaskevich):

    Artur:  хочу забронировать багги завтра в 14:00
    Анна:   Сколько человек поедет?           ← never checks the calendar
    Artur:  2
    Анна:   Сколько багги вам потребуется?    ← still no check, funnel grinds on
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from zoneinfo import ZoneInfo

import pytest

from services.api.app.answerers import AnswerContext
from services.api.app.calendar.access_token_cache import CalendarReconnectNeeded
from services.api.app.calendar.calendar_client import BusyInterval, FreeBusy
from services.api.app.calendar.settings_repository import ServiceRule
from services.api.app.russian_text import get_russian_normalizer
from services.api.app.sales.intent import Intent
from services.api.app.sales.sales_persona_answerer import (
    HITL_REASON_SCOPING_COMPLETE,
    RESPONSE_MODE_SALES_ESCALATION,
    SCOPING_COMPLETE_HANDOFF_LINE,
    SLOT_BUSY_LINE,
    STAGE_NEW,
    STAGE_PITCHING,
    STAGE_PRICING,
    STAGE_SCOPING,
    SalesPersonaAnswerer,
)

_NOW = datetime(2026, 5, 29, 9, 0, tzinfo=UTC)  # 12:00 Moscow, Fri 29 May
_TOMORROW_MOSCOW = ZoneInfo("Europe/Moscow")
_CHAT_ID = 7
_PROJECT_ID = 1


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

    def list_for_project(self, *, project_id: int) -> list[Any]:
        return []

    def get_by_name(self, *, project_id: int, name: str) -> Any | None:
        return None


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


class _Settings:
    def __init__(self) -> None:
        self.calendar_operator = "@op"
        self.project_timezone = "Europe/Moscow"
        self.lookahead_days = 60


class _FakeCalSettings:
    def __init__(
        self,
        *,
        enabled: bool = True,
        rules: list[ServiceRule] | None = None,
    ) -> None:
        self._enabled = enabled
        self._settings = _Settings()
        self._rules = rules if rules is not None else [_rule()]

    def is_enabled(self, project_id: int) -> bool:
        return self._enabled

    def get(self, project_id: int):
        return self._settings

    def list_service_rules(self, project_id: int) -> list[ServiceRule]:
        return self._rules


def _rule(*, name: str = "Багги") -> ServiceRule:
    week = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
    return ServiceRule(
        id=1,
        project_id=_PROJECT_ID,
        name=name,
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


class _RaisingTokenProvider:
    async def get_access_token(
        self, project_id, operator, *, operator_chat_id, trace_id
    ) -> str:
        raise CalendarReconnectNeeded("reconnect-please")


class _FreeBusy:
    def __init__(self, *, busy: tuple[BusyInterval, ...] = ()) -> None:
        self._busy = busy
        self.calls = 0  # AC 5 — assert "no re-check" by counting query_busy calls

    async def query_busy(
        self, *, access_token, time_min, time_max, trace_id, calendar_id="primary"
    ) -> FreeBusy:
        self.calls += 1
        return FreeBusy(calendar_id="primary", busy=self._busy)


def _ctx() -> AnswerContext:
    return AnswerContext(
        chat_id=_CHAT_ID,
        customer_username="@artur",
        trace_id="trc",
        now=_NOW,
        project_id=_PROJECT_ID,
    )


def _scoping_state(*, intent: Intent) -> dict[str, Any]:
    return {
        "chat_id": _CHAT_ID,
        "project_id": _PROJECT_ID,
        "current_stage": STAGE_SCOPING,
        "collected_intent": intent.to_dict(),
        "last_proposal": None,
    }


def _build(
    *,
    state: dict[str, Any] | None = None,
    openrouter: _FakeOpenRouter | None = None,
    cal_settings: _FakeCalSettings | None = None,
    token_provider: Any | None = None,
    freebusy: _FreeBusy | None = None,
    operator_chat_resolver=None,
) -> tuple[SalesPersonaAnswerer, _FakeStateRepo, _FakeOpenRouter, _FreeBusy]:
    state_repo = _FakeStateRepo(initial=state)
    openrouter = openrouter or _FakeOpenRouter()
    freebusy = freebusy if freebusy is not None else _FreeBusy()
    answerer = SalesPersonaAnswerer(
        state_repo=state_repo,
        services_repo=_NoOpServicesRepo(),
        openrouter=openrouter,
        normalizer=get_russian_normalizer(),
        clock=lambda: _NOW,
        bot_persona_getter=lambda: "Анна",
        calendar_settings_repo=cal_settings,
        calendar_token_provider=token_provider,
        calendar_freebusy_client=freebusy,
        operator_chat_resolver=(
            operator_chat_resolver
            if operator_chat_resolver is not None
            else (lambda op: 42)
        ),
    )
    return answerer, state_repo, openrouter, freebusy


def _busy_blocks_tomorrow_14() -> tuple[BusyInterval, ...]:
    """14:00 tomorrow (Sat 30 May Moscow) blocked from 13:00 to 15:00 → 14:00 busy,
    nearest free slot from 09:00 same day is offered (working hours start 09:00)."""
    return (
        BusyInterval(
            start=datetime(2026, 5, 30, 13, 0, tzinfo=_TOMORROW_MOSCOW),
            end=datetime(2026, 5, 30, 15, 0, tzinfo=_TOMORROW_MOSCOW),
        ),
    )


def _busy_blocks_whole_window() -> tuple[BusyInterval, ...]:
    """Wall-to-wall busy across the entire 60-day lookahead — no alternative."""
    return (
        BusyInterval(
            start=datetime(2026, 5, 29, 9, 0, tzinfo=_TOMORROW_MOSCOW),
            end=datetime(2026, 8, 1, 9, 0, tzinfo=_TOMORROW_MOSCOW),
        ),
    )


# --- AC 1 -------------------------------------------------------------------


@pytest.mark.asyncio
async def test_greeting_busy_offers_alternative_immediately() -> None:
    """Opener carries `завтра в 14:00`; 14:00 busy, 09:00 free.

    The bot's FIRST reply names the busy line + the alternative, parks in
    pitching with `last_proposal`, and does NOT escalate this turn (the
    customer has to accept first per Story 12.22).
    """
    openrouter = _FakeOpenRouter()
    openrouter.queue_response(
        {
            "extracted_fields": {"dates": "завтра в 14:00"},
            "next_question": "Сколько человек поедет?",
        }
    )
    answerer, state_repo, _, freebusy = _build(
        state=None,
        openrouter=openrouter,
        cal_settings=_FakeCalSettings(),
        token_provider=_TokenProvider(),
        freebusy=_FreeBusy(busy=_busy_blocks_tomorrow_14()),
    )
    result = await answerer.try_answer(
        question="хочу забронировать багги завтра в 14:00", ctx=_ctx()
    )
    text = result.text or ""
    assert SLOT_BUSY_LINE in text
    assert "Ближайшее свободное время" in text
    assert "09:00" in text
    # Story 12.22 contract — non-escalating when an alternative is offered.
    assert result.response_mode is None
    assert result.metadata.get("escalate") is not True
    assert "hitl_reason" not in result.metadata
    assert result.metadata["stage_before"] == STAGE_NEW
    assert result.metadata["stage_after"] == STAGE_PITCHING
    assert (
        result.metadata["sales_turn_kind"] == "scoping_complete_busy_alternative"
    )
    # The bot never asks the next scoping field on this turn.
    assert "Сколько человек поедет?" not in text
    # Story 12.19 — offered slot remembered for the next-turn confirmation.
    assert state_repo.upserts[-1]["current_stage"] == STAGE_PITCHING
    assert state_repo.upserts[-1]["last_proposal"] == {
        "alternative_iso": "2026-05-30T09:00:00+03:00"
    }
    assert freebusy.calls == 1


# --- AC 2 -------------------------------------------------------------------


@pytest.mark.asyncio
async def test_greeting_busy_no_alternative_escalates_immediately() -> None:
    openrouter = _FakeOpenRouter()
    openrouter.queue_response(
        {
            "extracted_fields": {"dates": "завтра в 14:00"},
            "next_question": "Сколько человек поедет?",
        }
    )
    answerer, _state_repo, _, _ = _build(
        state=None,
        openrouter=openrouter,
        cal_settings=_FakeCalSettings(),
        token_provider=_TokenProvider(),
        freebusy=_FreeBusy(busy=_busy_blocks_whole_window()),
    )
    result = await answerer.try_answer(
        question="хочу забронировать багги завтра в 14:00", ctx=_ctx()
    )
    text = result.text or ""
    assert SLOT_BUSY_LINE in text
    assert SCOPING_COMPLETE_HANDOFF_LINE in text
    assert "Ближайшее свободное время" not in text
    assert result.response_mode == RESPONSE_MODE_SALES_ESCALATION
    assert result.metadata["escalate"] is True
    assert result.metadata["hitl_reason"] == HITL_REASON_SCOPING_COMPLETE
    assert result.metadata["stage_after"] == STAGE_PITCHING


# --- AC 3 -------------------------------------------------------------------


@pytest.mark.asyncio
async def test_greeting_available_falls_through_to_scoping() -> None:
    openrouter = _FakeOpenRouter()
    openrouter.queue_response(
        {
            "extracted_fields": {"dates": "завтра в 14:00"},
            "next_question": "Сколько человек поедет?",
        }
    )
    answerer, state_repo, _, freebusy = _build(
        state=None,
        openrouter=openrouter,
        cal_settings=_FakeCalSettings(),
        token_provider=_TokenProvider(),
        freebusy=_FreeBusy(busy=()),
    )
    result = await answerer.try_answer(
        question="хочу забронировать багги завтра в 14:00", ctx=_ctx()
    )
    # Original greeting flow — LLM next-field question delivered, no busy line.
    assert result.text == "Сколько человек поедет?"
    assert SLOT_BUSY_LINE not in (result.text or "")
    assert result.metadata["stage_after"] == STAGE_SCOPING
    assert "last_proposal" not in state_repo.upserts[-1] or (
        state_repo.upserts[-1].get("last_proposal") is None
    )
    # The check did run (AVAILABLE just means the helper returned None).
    assert freebusy.calls == 1


# --- N1 (round 8): numeric date busy check falls back to the raw text --------
# The greeting/scoping LLM sometimes stores a numeric date WITHOUT its
# co-located time ("03.06"); extract_requested_start can't parse a date-only
# string, so the slot was handed off UNCHECKED. The raw customer message always
# carries date+time, so the busy check must use it (verdict is phrasing/field
# independent — AC5).


def _busy_blocks_june3_afternoon() -> tuple[BusyInterval, ...]:
    """3 June 2026 (Wed) blocked 13:00–16:30 → 16:00 busy (the live N1 slot)."""
    return (
        BusyInterval(
            start=datetime(2026, 6, 3, 13, 0, tzinfo=_TOMORROW_MOSCOW),
            end=datetime(2026, 6, 3, 16, 30, tzinfo=_TOMORROW_MOSCOW),
        ),
    )


@pytest.mark.asyncio
async def test_numeric_opener_busy_check_uses_raw_text_when_llm_drops_time() -> None:
    openrouter = _FakeOpenRouter()
    openrouter.queue_response(
        {
            "extracted_fields": {"dates": "03.06"},  # LLM stored date, dropped time
            "next_question": "Сколько человек поедет?",
        }
    )
    answerer, _state, _, freebusy = _build(
        state=None,
        openrouter=openrouter,
        cal_settings=_FakeCalSettings(),
        token_provider=_TokenProvider(),
        freebusy=_FreeBusy(busy=_busy_blocks_june3_afternoon()),
    )
    result = await answerer.try_answer(
        question="Можно 03.06 в 16:00 на багги, нас двое?", ctx=_ctx()
    )
    text = result.text or ""
    assert SLOT_BUSY_LINE in text  # 16:00 busy → rejected, not handed off
    assert freebusy.calls == 1  # the calendar WAS consulted


@pytest.mark.asyncio
async def test_numeric_scoping_reply_busy_check_uses_raw_text() -> None:
    openrouter = _FakeOpenRouter()
    openrouter.queue_response(
        {
            "extracted_fields": {"dates": "03.06"},  # LLM stored date, dropped time
            "next_question": "Сколько багги вам потребуется?",
        }
    )
    answerer, _state, _, freebusy = _build(
        state=_scoping_state(intent=Intent()),
        openrouter=openrouter,
        cal_settings=_FakeCalSettings(),
        token_provider=_TokenProvider(),
        freebusy=_FreeBusy(busy=_busy_blocks_june3_afternoon()),
    )
    result = await answerer.try_answer(
        question="Можно 03.06 в 16:00 на багги, нас двое?", ctx=_ctx()
    )
    text = result.text or ""
    assert SLOT_BUSY_LINE in text
    assert freebusy.calls == 1  # the calendar WAS consulted
    # A busy intercept during scoping parks in pitching with the offered slot.
    assert result.metadata["stage_after"] == STAGE_PITCHING


# --- AC 4 -------------------------------------------------------------------


@pytest.mark.asyncio
async def test_scoping_time_first_appears_mid_funnel_busy_intercepts() -> None:
    """Turn 1 set headcount only; turn 2 customer adds a busy time."""
    openrouter = _FakeOpenRouter()
    openrouter.queue_response(
        {
            "extracted_fields": {"dates": "завтра в 14:00"},
            "next_question": "Сколько багги вам потребуется?",
        }
    )
    state = _scoping_state(intent=Intent(headcount=2))
    answerer, state_repo, _, freebusy = _build(
        state=state,
        openrouter=openrouter,
        cal_settings=_FakeCalSettings(),
        token_provider=_TokenProvider(),
        freebusy=_FreeBusy(busy=_busy_blocks_tomorrow_14()),
    )
    result = await answerer.try_answer(question="завтра в 14:00", ctx=_ctx())
    text = result.text or ""
    assert SLOT_BUSY_LINE in text
    assert "Ближайшее свободное время" in text
    assert "09:00" in text
    assert result.metadata["stage_before"] == STAGE_SCOPING
    assert result.metadata["stage_after"] == STAGE_PITCHING
    assert (
        result.metadata["sales_turn_kind"] == "scoping_complete_busy_alternative"
    )
    # Partial intent preserved (headcount=2 + dates="завтра в 14:00").
    persisted_intent = state_repo.upserts[-1]["collected_intent"]
    assert persisted_intent["headcount"] == 2
    assert persisted_intent["dates"] == "завтра в 14:00"
    assert freebusy.calls == 1


# --- Regression: absolute date must hit the busy check too ------------------


@pytest.mark.asyncio
async def test_absolute_date_in_intent_intercepts_busy() -> None:
    """Live bug (31 May 2026): `в понедельник в 13:00` was accepted as bookable
    even though Monday's slot was busy — because the scoping LLM resolves a
    weekday/relative reference to an absolute date (`30 мая`, `1 июня`) and the
    old extractor only parsed relative anchors, so the calendar check was
    silently skipped. An absolute date must now parse and intercept the same as
    `завтра`.
    """
    openrouter = _FakeOpenRouter()
    openrouter.queue_response(
        {
            "extracted_fields": {"dates": "30 мая в 14:00"},
            "next_question": "Сколько человек поедет?",
        }
    )
    answerer, _state_repo, _, freebusy = _build(
        state=None,
        openrouter=openrouter,
        cal_settings=_FakeCalSettings(),
        token_provider=_TokenProvider(),
        freebusy=_FreeBusy(busy=_busy_blocks_tomorrow_14()),
    )
    result = await answerer.try_answer(
        question="можно багги 30 мая в 14:00?", ctx=_ctx()
    )
    text = result.text or ""
    assert SLOT_BUSY_LINE in text
    assert "Ближайшее свободное время" in text
    assert "09:00" in text
    assert result.metadata["stage_after"] == STAGE_PITCHING
    assert "Сколько человек поедет?" not in text
    assert freebusy.calls == 1


# --- AC 5 -------------------------------------------------------------------


@pytest.mark.asyncio
async def test_scoping_time_unchanged_skips_recheck() -> None:
    """Turn 1 already had `завтра в 14:00` + headcount=2; turn 2 adds vehicle_count.
    The parsed `requested_start` is identical → no availability call this turn.
    """
    openrouter = _FakeOpenRouter()
    openrouter.queue_response(
        {
            "extracted_fields": {"vehicle_count": 1},
            "next_question": "Какой уровень сложности — для новичка или опытного?",
        }
    )
    state = _scoping_state(
        intent=Intent(dates="завтра в 14:00", headcount=2),
    )
    answerer, _state_repo, _, freebusy = _build(
        state=state,
        openrouter=openrouter,
        cal_settings=_FakeCalSettings(),
        token_provider=_TokenProvider(),
        freebusy=_FreeBusy(busy=_busy_blocks_tomorrow_14()),
    )
    result = await answerer.try_answer(question="одну", ctx=_ctx())
    # Scoping continues with the next-field question; the busy line is absent.
    assert result.text == "Какой уровень сложности — для новичка или опытного?"
    assert SLOT_BUSY_LINE not in (result.text or "")
    # The headline assertion: NO calendar check fired on this turn.
    assert freebusy.calls == 0
    assert result.metadata["stage_after"] == STAGE_SCOPING


# --- AC 6 -------------------------------------------------------------------


@pytest.mark.asyncio
async def test_calendar_disabled_falls_through() -> None:
    openrouter = _FakeOpenRouter()
    openrouter.queue_response(
        {
            "extracted_fields": {"dates": "завтра в 14:00"},
            "next_question": "Сколько человек поедет?",
        }
    )
    answerer, _state_repo, _, freebusy = _build(
        state=None,
        openrouter=openrouter,
        cal_settings=_FakeCalSettings(enabled=False),
        token_provider=_TokenProvider(),
        freebusy=_FreeBusy(busy=_busy_blocks_tomorrow_14()),
    )
    result = await answerer.try_answer(
        question="хочу забронировать багги завтра в 14:00", ctx=_ctx()
    )
    assert result.text == "Сколько человек поедет?"
    assert SLOT_BUSY_LINE not in (result.text or "")
    assert result.metadata["stage_after"] == STAGE_SCOPING
    assert freebusy.calls == 0


# --- AC 7 -------------------------------------------------------------------


@pytest.mark.asyncio
async def test_multi_service_falls_through() -> None:
    openrouter = _FakeOpenRouter()
    openrouter.queue_response(
        {
            "extracted_fields": {"dates": "завтра в 14:00"},
            "next_question": "Сколько человек поедет?",
        }
    )
    rules = [_rule(name="Багги"), _rule(name="Квадро")]
    answerer, _state_repo, _, freebusy = _build(
        state=None,
        openrouter=openrouter,
        cal_settings=_FakeCalSettings(rules=rules),
        token_provider=_TokenProvider(),
        freebusy=_FreeBusy(busy=_busy_blocks_tomorrow_14()),
    )
    result = await answerer.try_answer(
        question="хочу забронировать багги завтра в 14:00", ctx=_ctx()
    )
    # Multi-service projects skip the early gate — `_calendar_booking_context`
    # already bails when len(active) != 1, and a future story will route by
    # service. Scoping continues today.
    assert result.text == "Сколько человек поедет?"
    assert SLOT_BUSY_LINE not in (result.text or "")
    assert result.metadata["stage_after"] == STAGE_SCOPING
    assert freebusy.calls == 0


# --- AC 8: NOT_CONNECTED ---------------------------------------------------


@pytest.mark.asyncio
async def test_not_connected_falls_through() -> None:
    """Operator's calendar not connected → STATUS_NOT_CONNECTED → silent fallthrough
    (mirrors `_complete_booking:1112` — never escalate from the early gate)."""
    openrouter = _FakeOpenRouter()
    openrouter.queue_response(
        {
            "extracted_fields": {"dates": "завтра в 14:00"},
            "next_question": "Сколько человек поедет?",
        }
    )
    answerer, _state_repo, _, freebusy = _build(
        state=None,
        openrouter=openrouter,
        cal_settings=_FakeCalSettings(),
        token_provider=_TokenProvider(),
        freebusy=_FreeBusy(busy=_busy_blocks_tomorrow_14()),
        operator_chat_resolver=lambda op: None,  # → operator_chat_id is None → NOT_CONNECTED
    )
    result = await answerer.try_answer(
        question="хочу забронировать багги завтра в 14:00", ctx=_ctx()
    )
    assert result.text == "Сколько человек поедет?"
    assert SLOT_BUSY_LINE not in (result.text or "")
    assert result.metadata["stage_after"] == STAGE_SCOPING


# --- AC 8: ERROR ------------------------------------------------------------


@pytest.mark.asyncio
async def test_error_falls_through() -> None:
    """Token provider raises → STATUS_ERROR → silent fallthrough (same precedent)."""
    openrouter = _FakeOpenRouter()
    openrouter.queue_response(
        {
            "extracted_fields": {"dates": "завтра в 14:00"},
            "next_question": "Сколько человек поедет?",
        }
    )
    answerer, _state_repo, _, freebusy = _build(
        state=None,
        openrouter=openrouter,
        cal_settings=_FakeCalSettings(),
        token_provider=_RaisingTokenProvider(),
        freebusy=_FreeBusy(busy=_busy_blocks_tomorrow_14()),
    )
    result = await answerer.try_answer(
        question="хочу забронировать багги завтра в 14:00", ctx=_ctx()
    )
    assert result.text == "Сколько человек поедет?"
    assert SLOT_BUSY_LINE not in (result.text or "")
    assert result.metadata["stage_after"] == STAGE_SCOPING


# --- R9-1 (round 9): a price ask must not wedge the conversation --------------
# Once in a pricing stage, every later turn used to route to pricing, so a
# greeting/booking got a stale price reply. Now a "moved-on" turn (greeting or a
# parseable booking) re-enters the funnel; a price follow-up stays in pricing.


def _pricing_state() -> dict[str, Any]:
    return {
        "chat_id": _CHAT_ID,
        "project_id": _PROJECT_ID,
        "current_stage": STAGE_PRICING,
        "collected_intent": Intent().to_dict(),
        "last_proposal": None,
    }


@pytest.mark.asyncio
async def test_pricing_stage_greeting_reroutes_to_greeting_not_price() -> None:
    openrouter = _FakeOpenRouter()
    openrouter.queue_response(
        {"extracted_fields": {}, "next_question": "Здравствуйте! Какие даты?"}
    )
    answerer, _state, _, _ = _build(
        state=_pricing_state(),
        openrouter=openrouter,
        cal_settings=_FakeCalSettings(),
        token_provider=_TokenProvider(),
        freebusy=_FreeBusy(),
    )
    result = await answerer.try_answer(question="Здравствуйте!", ctx=_ctx())
    assert result.handled is True
    assert result.text == "Здравствуйте! Какие даты?"  # greeting, not a price line


@pytest.mark.asyncio
async def test_pricing_stage_booking_reroutes_to_availability_not_price() -> None:
    openrouter = _FakeOpenRouter()
    openrouter.queue_response(
        {
            "extracted_fields": {"dates": "завтра в 14:00"},
            "next_question": "Сколько человек?",
        }
    )
    answerer, _state, _, freebusy = _build(
        state=_pricing_state(),
        openrouter=openrouter,
        cal_settings=_FakeCalSettings(),
        token_provider=_TokenProvider(),
        freebusy=_FreeBusy(busy=_busy_blocks_tomorrow_14()),
    )
    result = await answerer.try_answer(
        question="А завтра в 14:00 свободно для багги?", ctx=_ctx()
    )
    assert SLOT_BUSY_LINE in (result.text or "")  # availability verdict, not price
    assert freebusy.calls == 1


# --- N1 round 10: numeric counter-offer in the PITCHING path -----------------
# After a busy intercept parks the funnel in pitching, a NEW numeric date
# ("Можно 03.06 в 16:00…") must be re-checked. The pitching counter-offer used
# date_parser (no numeric), so it was accepted unchecked.


def _pitching_state(*, intent: Intent) -> dict[str, Any]:
    return {
        "chat_id": _CHAT_ID,
        "project_id": _PROJECT_ID,
        "current_stage": STAGE_PITCHING,
        "collected_intent": intent.to_dict(),
        "last_proposal": {"alternative_iso": "2026-05-30T09:00:00+03:00"},
    }


@pytest.mark.asyncio
async def test_pitching_numeric_counteroffer_is_busy_checked() -> None:
    answerer, _state, _, freebusy = _build(
        state=_pitching_state(intent=Intent(dates="завтра в 14:00")),
        cal_settings=_FakeCalSettings(),
        token_provider=_TokenProvider(),
        freebusy=_FreeBusy(busy=_busy_blocks_june3_afternoon()),
    )
    result = await answerer.try_answer(
        question="Можно 03.06 в 16:00 на багги, нас двое?", ctx=_ctx()
    )
    assert SLOT_BUSY_LINE in (result.text or "")  # 16:00 on 3 June is busy
    assert freebusy.calls == 1  # the numeric counter-offer WAS checked

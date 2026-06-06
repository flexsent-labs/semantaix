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
from services.api.app.calendar.service_resolver import names_invalid_date
from services.api.app.calendar.settings_repository import ServiceRule
from services.api.app.rag import RagChunk
from services.api.app.russian_text import get_russian_normalizer
from services.api.app.sales.intent import Intent
from services.api.app.sales.price_lookup import PriceMissing, PriceUnknownPayload
from services.api.app.sales.sales_persona_answerer import (
    _RETURNING_NO_GREETING_DIRECTIVE,
    ASK_FOR_TIME_LINE,
    BUSY_NO_SLOT_HANDOFF_TAIL,
    BUSY_NO_SLOT_HANDOFF_TAIL_EN,
    CAPACITY_ESCALATION_LINE,
    CLOSING_HANDOFF_LINE_EN,
    COUNT_MISMATCH_CLARIFY_LINE,
    FAQ_DEFER_LINE,
    GIBBERISH_CLARIFY_LINE,
    GRATITUDE_ACK_LINE,
    HITL_REASON_CAPACITY,
    HITL_REASON_SCOPING_COMPLETE,
    INVALID_DATE_CLARIFY_LINE,
    MIXED_OUT_OF_SCOPE_SUFFIX,
    MIXED_SERVICE_CLARIFY_LINE,
    PAST_DATE_CLARIFY_LINE,
    PRICING_MISS_FALLBACK,
    RESPONSE_MODE_SALES_ESCALATION,
    SCOPING_COMPLETE_HANDOFF_LINE,
    SCOPING_COMPLETE_HANDOFF_LINE_EN,
    SLOT_BUSY_LINE,
    SLOT_FREE_HANDOFF_LINE,
    SLOT_FREE_INQUIRY_LINE,
    SLOT_TOO_FAR_LINE,
    STAGE_CLOSING,
    STAGE_NEW,
    STAGE_PITCHING,
    STAGE_PRICING,
    STAGE_SCOPING,
    WORKING_DAYS_LINE,
    WORKING_HOURS_LINE,
    SalesPersonaAnswerer,
    _as_positive_int,
    _format_working_days,
    _format_working_hours,
    _parse_buggy_seats,
    _parse_headcount,
    _strip_leading_greeting,
    detect_vague_window,
    is_availability_inquiry,
    is_capacity_question,
    is_count_inconsistent,
    is_duration_question,
    is_eligibility_question,
    is_gibberish,
    is_gratitude,
    is_info_faq_question,
    is_mixed_service_request,
    is_working_days_question,
    is_working_hours_question,
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


class _StubPriceLookup:
    """Minimal price lookup for the combined price+availability tests."""

    def __init__(self, result: Any) -> None:
        self.result = result
        self.calls: list[dict[str, Any]] = []

    async def lookup(self, *, project_id, intent, question, **_kwargs):
        self.calls.append({"project_id": project_id, "question": question})
        return self.result


def _build(
    *,
    state: dict[str, Any] | None = None,
    openrouter: _FakeOpenRouter | None = None,
    cal_settings: _FakeCalSettings | None = None,
    token_provider: Any | None = None,
    freebusy: _FreeBusy | None = None,
    operator_chat_resolver=None,
    price_lookup: Any | None = None,
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
        price_lookup=price_lookup,
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
    # Story 12.71 (round-17 R17-4) — busy + no alternative is ONE coherent
    # message: the busy verdict + a colleague-will-find-a-time clause. It must
    # NOT merge the booking-confirmation handoff ("передам … на подтверждение"),
    # which would contradict "занято".
    assert SLOT_BUSY_LINE in text
    assert text == SLOT_BUSY_LINE + BUSY_NO_SLOT_HANDOFF_TAIL
    assert SCOPING_COMPLETE_HANDOFF_LINE not in text
    assert "на подтверждение" not in text
    assert "Ближайшее свободное время" not in text
    assert result.response_mode == RESPONSE_MODE_SALES_ESCALATION
    assert result.metadata["escalate"] is True
    assert result.metadata["hitl_reason"] == HITL_REASON_SCOPING_COMPLETE
    assert result.metadata["stage_after"] == STAGE_PITCHING


@pytest.mark.asyncio
async def test_greeting_busy_no_alternative_en_coherent_tail() -> None:
    # The same coherent (non-contradictory) copy in English.
    openrouter = _FakeOpenRouter()
    openrouter.queue_response(
        {
            "extracted_fields": {"dates": "tomorrow at 2pm"},
            "next_question": "How many people?",
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
        question="I want to book a buggy tomorrow at 2pm", ctx=_ctx()
    )
    text = result.text or ""
    assert text.endswith(BUSY_NO_SLOT_HANDOFF_TAIL_EN)
    assert SCOPING_COMPLETE_HANDOFF_LINE_EN not in text


@pytest.mark.asyncio
async def test_far_future_free_booking_is_not_falsely_busy() -> None:
    # Story 12.71 (round-17 R17-4) — «15 декабря» (≈200 days out, no events) is
    # FREE: the early busy-intercept stays silent and the funnel proceeds. With
    # the old 60-day window it falsely reported "занято".
    openrouter = _FakeOpenRouter()
    openrouter.queue_response(
        {
            "extracted_fields": {"dates": "15 декабря в 14:00"},
            "next_question": "Сколько человек поедет?",
        }
    )
    answerer, _state_repo, _, freebusy = _build(
        state=None,
        openrouter=openrouter,
        cal_settings=_FakeCalSettings(),
        token_provider=_TokenProvider(),
        freebusy=_FreeBusy(busy=()),
    )
    result = await answerer.try_answer(
        question="можно 15 декабря в 14:00 на багги?", ctx=_ctx()
    )
    text = result.text or ""
    assert SLOT_BUSY_LINE not in text
    assert SLOT_TOO_FAR_LINE not in text
    assert text == "Сколько человек поедет?"
    assert result.metadata["stage_after"] == STAGE_SCOPING
    assert freebusy.calls == 1


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


# --- R11-1 (round 11): negotiation — re-check the customer's counter-time -----
# After a busy verdict offers an alternative (08:00), a follow-up naming a
# DIFFERENT time ("а давайте тогда в 12:00", date implied by context) must be
# checked and confirmed/declined — never silently booked at the offered 08:00.


def _pitch_state_0800(*, intent_dates: str = "завтра в 14:00") -> dict[str, Any]:
    """Pitching, having offered 30 May 08:00 as the alternative."""
    return {
        "chat_id": _CHAT_ID,
        "project_id": _PROJECT_ID,
        "current_stage": STAGE_PITCHING,
        "collected_intent": Intent(dates=intent_dates).to_dict(),
        "last_proposal": {"alternative_iso": "2026-05-30T08:00:00+03:00"},
    }


def _busy_blocks_may30_noon() -> tuple[BusyInterval, ...]:
    """30 May 2026 blocked 11:00–13:00 → 12:00 busy, 08:00 free."""
    return (
        BusyInterval(
            start=datetime(2026, 5, 30, 11, 0, tzinfo=_TOMORROW_MOSCOW),
            end=datetime(2026, 5, 30, 13, 0, tzinfo=_TOMORROW_MOSCOW),
        ),
    )


@pytest.mark.asyncio
async def test_pitching_timeonly_counteroffer_rechecks_new_time_not_proposal() -> None:
    answerer, _s, _, freebusy = _build(
        state=_pitch_state_0800(),
        cal_settings=_FakeCalSettings(),
        token_provider=_TokenProvider(),
        freebusy=_FreeBusy(busy=_busy_blocks_may30_noon()),  # 12:00 busy
    )
    result = await answerer.try_answer(question="а давайте тогда в 12:00", ctx=_ctx())
    text = result.text or ""
    assert SLOT_BUSY_LINE in text  # checked 12:00 (busy) — did NOT book 08:00
    assert "на 08:00" not in text  # never confirmed the bot's own slot
    assert freebusy.calls == 1


@pytest.mark.asyncio
async def test_pitching_correction_picks_nonproposal_time() -> None:
    answerer, _s, _, freebusy = _build(
        state=_pitch_state_0800(),
        cal_settings=_FakeCalSettings(),
        token_provider=_TokenProvider(),
        freebusy=_FreeBusy(busy=_busy_blocks_may30_noon()),
    )
    result = await answerer.try_answer(
        question="Нет, мне нужно именно в 12:00, а не в 08:00", ctx=_ctx()
    )
    text = result.text or ""
    assert SLOT_BUSY_LINE in text  # re-checked 12:00, the time they want
    assert freebusy.calls == 1


@pytest.mark.asyncio
async def test_pitching_timeonly_counteroffer_free_confirms_new_time() -> None:
    answerer, _s, _, freebusy = _build(
        state=_pitch_state_0800(),
        cal_settings=_FakeCalSettings(),
        token_provider=_TokenProvider(),
        freebusy=_FreeBusy(busy=()),  # 12:00 free
    )
    result = await answerer.try_answer(question="а давайте в 12:00", ctx=_ctx())
    assert result.text == SLOT_FREE_HANDOFF_LINE
    assert freebusy.calls == 1


@pytest.mark.asyncio
async def test_pitching_bare_acceptance_still_confirms_offered_slot() -> None:
    # No competing time → genuine acceptance of the offered 08:00 (no re-check).
    answerer, _s, _, freebusy = _build(
        state=_pitch_state_0800(),
        cal_settings=_FakeCalSettings(),
        token_provider=_TokenProvider(),
        freebusy=_FreeBusy(busy=_busy_blocks_may30_noon()),
    )
    result = await answerer.try_answer(question="да, отлично, давайте", ctx=_ctx())
    text = result.text or ""
    assert "08:00" in text  # confirmed the offered slot
    assert freebusy.calls == 0  # acceptance does not re-query the calendar


# --- N3 (round 11, Story 12.52): remaining funnel lines mirror the language ---
# 12.47 missed the pitching-followup and closing handoffs; an English thread
# that reaches them (a closure, or a non-counter follow-up) must stay English.


@pytest.mark.asyncio
async def test_pitching_followup_en_localizes_handoff() -> None:
    answerer, _s, _, freebusy = _build(
        state=_pitch_state_0800(),
        cal_settings=_FakeCalSettings(),
        token_provider=_TokenProvider(),
        freebusy=_FreeBusy(busy=()),
    )
    # English, no concrete time, not an acceptance → pitching-followup handoff.
    result = await answerer.try_answer(
        question="Thanks, I'll think about it.", ctx=_ctx()
    )
    assert result.text == SCOPING_COMPLETE_HANDOFF_LINE_EN
    assert freebusy.calls == 0  # no counter time → no re-check


@pytest.mark.asyncio
async def test_closing_en_localizes_handoff() -> None:
    state = {
        "chat_id": _CHAT_ID,
        "project_id": _PROJECT_ID,
        "current_stage": STAGE_CLOSING,
        "collected_intent": Intent().to_dict(),
        "last_proposal": None,
    }
    answerer, _s, _, _ = _build(
        state=state,
        cal_settings=_FakeCalSettings(),
        token_provider=_TokenProvider(),
    )
    result = await answerer.try_answer(question="Thank you!", ctx=_ctx())
    assert result.text == CLOSING_HANDOFF_LINE_EN


# --- D5 (round 12, Story 12.54): mixed-intent gets a one-line decline ---------
# A message mixing a booking with an out-of-scope ask is handled by the funnel
# (the booking), but the off-topic part was silently dropped. Append a brief
# decline — without re-asking booking fields.


@pytest.mark.asyncio
async def test_mixed_intent_booking_appends_out_of_scope_decline() -> None:
    openrouter = _FakeOpenRouter()
    openrouter.queue_response(
        {"extracted_fields": {"dates": "завтра в 14:00"}, "next_question": "ок"}
    )
    answerer, _s, _, _ = _build(
        state=None,
        openrouter=openrouter,
        cal_settings=_FakeCalSettings(),
        token_provider=_TokenProvider(),
        freebusy=_FreeBusy(busy=_busy_blocks_tomorrow_14()),
    )
    result = await answerer.try_answer(
        question="Посоветуйте ресторан и запишите на багги завтра в 14:00, нас двое.",
        ctx=_ctx(),
    )
    text = result.text or ""
    assert SLOT_BUSY_LINE in text  # the booking part is still handled
    assert MIXED_OUT_OF_SCOPE_SUFFIX in text  # and the restaurant ask is declined


@pytest.mark.asyncio
async def test_pure_booking_does_not_append_decline() -> None:
    openrouter = _FakeOpenRouter()
    openrouter.queue_response(
        {"extracted_fields": {"dates": "завтра в 14:00"}, "next_question": "ок"}
    )
    answerer, _s, _, _ = _build(
        state=None,
        openrouter=openrouter,
        cal_settings=_FakeCalSettings(),
        token_provider=_TokenProvider(),
        freebusy=_FreeBusy(busy=_busy_blocks_tomorrow_14()),
    )
    result = await answerer.try_answer(
        question="Запишите на багги завтра в 14:00, нас двое.", ctx=_ctx()
    )
    assert MIXED_OUT_OF_SCOPE_SUFFIX not in (result.text or "")  # no off-topic part


# --- Story 12.55 (round 12): no re-greeting on a mid-thread intent switch -----


@pytest.mark.asyncio
async def test_returning_customer_greeting_suppresses_hello() -> None:
    # A fresh booking after a prior handoff re-enters greeting; the prompt must
    # tell the LLM not to say "Здравствуйте" again.
    openrouter = _FakeOpenRouter()
    openrouter.queue_response({"extracted_fields": {}, "next_question": "ок"})
    state = {
        "chat_id": _CHAT_ID,
        "project_id": _PROJECT_ID,
        "current_stage": STAGE_CLOSING,
        "collected_intent": Intent().to_dict(),
        "last_proposal": None,
    }
    answerer, _s, _, _ = _build(
        state=state, openrouter=openrouter, cal_settings=_FakeCalSettings()
    )
    await answerer.try_answer(question="Хочу записаться на багги", ctx=_ctx())
    assert openrouter.calls  # the greeting LLM ran
    assert _RETURNING_NO_GREETING_DIRECTIVE in openrouter.calls[0]["system"]


@pytest.mark.asyncio
async def test_first_contact_greeting_keeps_hello() -> None:
    openrouter = _FakeOpenRouter()
    openrouter.queue_response({"extracted_fields": {}, "next_question": "ок"})
    answerer, _s, _, _ = _build(
        state=None, openrouter=openrouter, cal_settings=_FakeCalSettings()
    )
    await answerer.try_answer(question="Здравствуйте! Хочу багги", ctx=_ctx())
    assert openrouter.calls
    assert _RETURNING_NO_GREETING_DIRECTIVE not in openrouter.calls[0]["system"]


# --- Story 12.56 (round-13): deterministically strip a re-greeting -----------
# The soft no-greeting directive (12.55) isn't always obeyed; on a returning
# turn we strip a leading salutation from the reply so it can't slip through.


def test_strip_leading_greeting_removes_salutation() -> None:
    assert (
        _strip_leading_greeting("Здравствуйте! На какой день перенести бронь?")
        == "На какой день перенести бронь?"
    )
    assert _strip_leading_greeting("Привет, чем помочь?") == "Чем помочь?"
    assert _strip_leading_greeting("Добрый день! Уточните дату.") == "Уточните дату."


def test_strip_leading_greeting_keeps_non_greeting() -> None:
    assert (
        _strip_leading_greeting("На какой день перенести бронь?")
        == "На какой день перенести бронь?"
    )


def test_strip_leading_greeting_keeps_when_only_greeting() -> None:
    # Nothing would remain → keep the original rather than emit an empty reply.
    assert _strip_leading_greeting("Здравствуйте.") == "Здравствуйте."


@pytest.mark.asyncio
async def test_returning_reentry_strips_hello_from_reply() -> None:
    # «перенести бронь» is a sales intent → from closing it re-enters greeting
    # (returning=True). Even if the LLM greets, the reply must not.
    openrouter = _FakeOpenRouter()
    openrouter.queue_response(
        {
            "extracted_fields": {},
            "next_question": "Здравствуйте! На какой день перенести бронь?",
        }
    )
    state = {
        "chat_id": _CHAT_ID,
        "project_id": _PROJECT_ID,
        "current_stage": STAGE_CLOSING,
        "collected_intent": Intent().to_dict(),
        "last_proposal": None,
    }
    answerer, _s, _, _ = _build(
        state=state, openrouter=openrouter, cal_settings=_FakeCalSettings()
    )
    result = await answerer.try_answer(
        question="А можно перенести бронь на другой день?", ctx=_ctx()
    )
    text = result.text or ""
    assert "Здравствуйте" not in text
    assert text.startswith("На какой день")


@pytest.mark.asyncio
async def test_first_contact_reply_keeps_hello() -> None:
    openrouter = _FakeOpenRouter()
    openrouter.queue_response(
        {"extracted_fields": {}, "next_question": "Здравствуйте! Чем могу помочь?"}
    )
    answerer, _s, _, _ = _build(
        state=None, openrouter=openrouter, cal_settings=_FakeCalSettings()
    )
    result = await answerer.try_answer(
        question="Здравствуйте, хочу багги", ctx=_ctx()
    )
    assert "Здравствуйте" in (result.text or "")  # first contact still greets


# --- K1 (round-13): the nearest-free alternative is current-time aware --------
# Regression guard: when "now" is mid-morning and today is busy 13:00–16:30,
# the proposed alternative is the next free slot >= now (10:00), never the
# day's opening hour (which is already in the past). Pins `now` so it can't
# silently revert to a static opening-hour suggestion.


@pytest.mark.asyncio
async def test_nearest_free_alternative_is_now_aware() -> None:
    now = datetime(2026, 5, 29, 6, 41, tzinfo=UTC)  # 09:41 Moscow, Fri
    openrouter = _FakeOpenRouter()
    openrouter.queue_response(
        {"extracted_fields": {"dates": "сегодня в 14:00"}, "next_question": "?"}
    )
    busy = (
        BusyInterval(
            start=datetime(2026, 5, 29, 13, 0, tzinfo=_TOMORROW_MOSCOW),
            end=datetime(2026, 5, 29, 16, 30, tzinfo=_TOMORROW_MOSCOW),
        ),
    )
    answerer = SalesPersonaAnswerer(
        state_repo=_FakeStateRepo(initial=None),
        services_repo=_NoOpServicesRepo(),
        openrouter=openrouter,
        normalizer=get_russian_normalizer(),
        clock=lambda: now,
        bot_persona_getter=lambda: "Анна",
        calendar_settings_repo=_FakeCalSettings(),
        calendar_token_provider=_TokenProvider(),
        calendar_freebusy_client=_FreeBusy(busy=busy),
        operator_chat_resolver=lambda op: 42,
    )
    ctx = AnswerContext(
        chat_id=_CHAT_ID,
        customer_username="@artur",
        trace_id="trc",
        now=now,
        project_id=_PROJECT_ID,
    )
    result = await answerer.try_answer(
        question="Хочу сегодня в 14:00 на багги, нас двое, одна багги.", ctx=ctx
    )
    text = result.text or ""
    assert SLOT_BUSY_LINE in text
    assert "10:00" in text  # next free slot >= now
    assert "09:00" not in text  # never the pre-now opening hour


# --- Round-14: capacity (D/12.59), availability inquiry (B/12.58), dashes (12.57)


def test_is_capacity_question_matches_only_capacity() -> None:
    assert is_capacity_question("Нас восемь человек, сколько багги нам понадобится?")
    assert is_capacity_question("Сколько багги нужно на 12 человек?")
    assert not is_capacity_question("Сколько стоит покататься на багги?")
    assert not is_capacity_question("Запишите на багги завтра в 14:00, нас двое")


@pytest.mark.asyncio
async def test_capacity_question_escalates_with_checking_copy_not_thanks() -> None:
    answerer, _s, openrouter, freebusy = _build(
        state=None, cal_settings=_FakeCalSettings(), token_provider=_TokenProvider()
    )
    result = await answerer.try_answer(
        question="Нас восемь человек, сколько багги нам понадобится?", ctx=_ctx()
    )
    assert result.text == CAPACITY_ESCALATION_LINE  # "Уточняю у коллег…", not "Спасибо"
    assert "Спасибо" not in (result.text or "")
    assert result.metadata.get("escalate") is True
    assert result.metadata.get("hitl_reason") == HITL_REASON_CAPACITY
    assert freebusy.calls == 0
    assert openrouter.calls == []  # answered deterministically, no LLM


def test_is_availability_inquiry_distinguishes_question_from_request() -> None:
    assert is_availability_inquiry("А сегодня в 16:30 свободно для багги?")
    assert is_availability_inquiry("В 14:00 занято?")
    assert not is_availability_inquiry("Запишите на багги в 16:30")
    assert not is_availability_inquiry("Хочу забронировать багги завтра")


@pytest.mark.asyncio
async def test_availability_inquiry_free_gives_verdict_no_hitl() -> None:
    openrouter = _FakeOpenRouter()
    openrouter.queue_response(
        {"extracted_fields": {"dates": "завтра в 14:00"}, "next_question": "ок"}
    )
    answerer, _s, _, freebusy = _build(
        state=None,
        openrouter=openrouter,
        cal_settings=_FakeCalSettings(),
        token_provider=_TokenProvider(),
        freebusy=_FreeBusy(busy=()),  # free
    )
    result = await answerer.try_answer(
        question="А завтра в 14:00 свободно для багги?", ctx=_ctx()
    )
    assert result.text == SLOT_FREE_INQUIRY_LINE  # plain "Да, это время свободно."
    assert result.metadata.get("escalate") is not True  # NO HITL ticket
    assert "hitl_reason" not in result.metadata
    assert "передам" not in (result.text or "").lower()  # not a handoff
    assert freebusy.calls == 1  # the calendar WAS consulted


@pytest.mark.asyncio
async def test_availability_inquiry_busy_gives_verdict_with_alt_no_hitl() -> None:
    openrouter = _FakeOpenRouter()
    openrouter.queue_response(
        {"extracted_fields": {"dates": "завтра в 14:00"}, "next_question": "ок"}
    )
    answerer, _s, _, freebusy = _build(
        state=None,
        openrouter=openrouter,
        cal_settings=_FakeCalSettings(),
        token_provider=_TokenProvider(),
        freebusy=_FreeBusy(busy=_busy_blocks_tomorrow_14()),
    )
    result = await answerer.try_answer(
        question="А завтра в 14:00 свободно для багги?", ctx=_ctx()
    )
    text = result.text or ""
    assert SLOT_BUSY_LINE in text
    assert "Ближайшее свободное время" in text
    assert result.metadata.get("escalate") is not True  # still no HITL for a question


def test_customer_constants_use_plain_hyphen_not_emdash() -> None:
    # Story 12.57 — customer-facing copy uses "-", never "—"/"–".
    for line in (
        SLOT_FREE_HANDOFF_LINE,
        SCOPING_COMPLETE_HANDOFF_LINE,
        MIXED_OUT_OF_SCOPE_SUFFIX,
        CAPACITY_ESCALATION_LINE,
    ):
        assert "—" not in line and "–" not in line


# --- R14-1 (round-14, Story 12.60): vague time window → propose a slot --------


def test_detect_vague_window_maps_phrases() -> None:
    assert detect_vague_window("во второй половине дня") == (12, 18)
    assert detect_vague_window("утром") == (8, 12)
    assert detect_vague_window("вечером") == (16, 20)
    assert detect_vague_window("завтра в 14:00") is None  # concrete, not vague
    assert detect_vague_window("нас двое") is None


@pytest.mark.asyncio
async def test_vague_window_proposes_slot_not_decline() -> None:
    openrouter = _FakeOpenRouter()
    # The LLM might unhelpfully decline; the deterministic vague-window intercept
    # overrides it with a concrete proposal.
    openrouter.queue_response(
        {
            "extracted_fields": {"dates": "завтра", "headcount": 2},
            "next_question": "Не смогу тут помочь.",
        }
    )
    answerer, state_repo, _, freebusy = _build(
        state=None,
        openrouter=openrouter,
        cal_settings=_FakeCalSettings(),
        token_provider=_TokenProvider(),
        freebusy=_FreeBusy(busy=()),  # tomorrow afternoon free
    )
    result = await answerer.try_answer(
        question="Хотим завтра покататься на багги во второй половине дня, нас двое.",
        ctx=_ctx(),
    )
    text = result.text or ""
    assert "Не смогу" not in text  # never the flat decline
    assert text.startswith("Да, есть свободное время")  # the offer copy
    assert "12:00" in text  # a concrete slot from the afternoon window
    assert result.metadata.get("escalate") is not True  # no HITL on the offer turn
    assert result.metadata["stage_after"] == STAGE_PITCHING
    assert freebusy.calls == 1


@pytest.mark.asyncio
async def test_vague_window_followup_counteroffer_books_carried_date() -> None:
    # After the offer parks pitching with the proposed 30 May 12:00 slot, a
    # concrete counter-time books THAT day (date carried from the offer).
    state = {
        "chat_id": _CHAT_ID,
        "project_id": _PROJECT_ID,
        "current_stage": STAGE_PITCHING,
        "collected_intent": Intent(
            dates="завтра во второй половине дня", headcount=2
        ).to_dict(),
        "last_proposal": {"alternative_iso": "2026-05-30T12:00:00+03:00"},
    }
    answerer, _s, _, freebusy = _build(
        state=state,
        cal_settings=_FakeCalSettings(),
        token_provider=_TokenProvider(),
        freebusy=_FreeBusy(busy=()),  # 15:00 free
    )
    result = await answerer.try_answer(question="давайте в 15:00", ctx=_ctx())
    assert result.text == SLOT_FREE_HANDOFF_LINE  # booked 30 May 15:00
    assert freebusy.calls == 1


# --- Round-14 coverage: intercept guard branches + scoping-path hooks --------


@pytest.mark.asyncio
async def test_vague_window_no_date_falls_through() -> None:
    a, _s, _o, _f = _build(
        state=None, cal_settings=_FakeCalSettings(), token_provider=_TokenProvider()
    )
    r = await a._maybe_answer_vague_window(
        question="во второй половине дня", ctx=_ctx(),
        merged_intent=Intent(), stage_before=STAGE_SCOPING,
    )
    assert r is None  # vague window but no parseable date


@pytest.mark.asyncio
async def test_vague_window_concrete_time_falls_through() -> None:
    a, _s, _o, _f = _build(
        state=None, cal_settings=_FakeCalSettings(), token_provider=_TokenProvider()
    )
    r = await a._maybe_answer_vague_window(
        question="завтра в 14:00, можно во второй половине дня", ctx=_ctx(),
        merged_intent=Intent(dates="завтра в 14:00"), stage_before=STAGE_SCOPING,
    )
    assert r is None  # a concrete time is present → not a vague case


@pytest.mark.asyncio
async def test_vague_window_calendar_disabled_falls_through() -> None:
    a, _s, _o, _f = _build(
        state=None, cal_settings=_FakeCalSettings(enabled=False),
        token_provider=_TokenProvider(),
    )
    r = await a._maybe_answer_vague_window(
        question="завтра во второй половине дня", ctx=_ctx(),
        merged_intent=Intent(dates="завтра"), stage_before=STAGE_SCOPING,
    )
    assert r is None


@pytest.mark.asyncio
async def test_vague_window_busy_start_proposes_alternative() -> None:
    busy = (
        BusyInterval(
            start=datetime(2026, 5, 30, 11, 0, tzinfo=_TOMORROW_MOSCOW),
            end=datetime(2026, 5, 30, 15, 0, tzinfo=_TOMORROW_MOSCOW),
        ),
    )
    a, _s, _o, _f = _build(
        state=None, cal_settings=_FakeCalSettings(),
        token_provider=_TokenProvider(), freebusy=_FreeBusy(busy=busy),
    )
    r = await a._maybe_answer_vague_window(
        question="завтра во второй половине дня", ctx=_ctx(),
        merged_intent=Intent(dates="завтра"), stage_before=STAGE_SCOPING,
    )
    assert r is not None and "15:00" in (r.text or "")  # 12:00 busy → alt 15:00


@pytest.mark.asyncio
async def test_vague_window_no_free_slot_falls_through() -> None:
    a, _s, _o, _f = _build(
        state=None, cal_settings=_FakeCalSettings(), token_provider=_TokenProvider(),
        freebusy=_FreeBusy(busy=_busy_blocks_whole_window()),
    )
    r = await a._maybe_answer_vague_window(
        question="завтра во второй половине дня", ctx=_ctx(),
        merged_intent=Intent(dates="завтра"), stage_before=STAGE_SCOPING,
    )
    assert r is None  # nothing free to propose


@pytest.mark.asyncio
async def test_inquiry_no_concrete_time_falls_through() -> None:
    a, _s, _o, _f = _build(
        state=None, cal_settings=_FakeCalSettings(), token_provider=_TokenProvider()
    )
    r = await a._maybe_answer_availability_inquiry(
        question="а завтра свободно?", ctx=_ctx(), merged_intent=Intent()
    )
    assert r is None  # inquiry but no concrete time


@pytest.mark.asyncio
async def test_inquiry_calendar_disabled_falls_through() -> None:
    a, _s, _o, _f = _build(
        state=None, cal_settings=_FakeCalSettings(enabled=False),
        token_provider=_TokenProvider(),
    )
    r = await a._maybe_answer_availability_inquiry(
        question="завтра в 14:00 свободно?", ctx=_ctx(),
        merged_intent=Intent(dates="завтра в 14:00"),
    )
    assert r is None


@pytest.mark.asyncio
async def test_inquiry_not_connected_falls_through() -> None:
    a, _s, _o, _f = _build(
        state=None, cal_settings=_FakeCalSettings(),
        token_provider=_RaisingTokenProvider(),  # → not connected
    )
    r = await a._maybe_answer_availability_inquiry(
        question="завтра в 14:00 свободно?", ctx=_ctx(),
        merged_intent=Intent(dates="завтра в 14:00"),
    )
    assert r is None  # can't verify → fall through (no verdict invented)


@pytest.mark.asyncio
async def test_scoping_path_inquiry_returns_verdict() -> None:
    openrouter = _FakeOpenRouter()
    openrouter.queue_response(
        {"extracted_fields": {"dates": "завтра в 14:00"}, "next_question": "ок"}
    )
    a, _s, _o, _f = _build(
        state=_scoping_state(intent=Intent(headcount=2)),
        openrouter=openrouter, cal_settings=_FakeCalSettings(),
        token_provider=_TokenProvider(), freebusy=_FreeBusy(busy=()),
    )
    r = await a.try_answer(question="А завтра в 14:00 свободно?", ctx=_ctx())
    assert r.text == SLOT_FREE_INQUIRY_LINE  # mid-scoping inquiry → verdict


@pytest.mark.asyncio
async def test_scoping_path_vague_window_offers_slot() -> None:
    openrouter = _FakeOpenRouter()
    openrouter.queue_response(
        {"extracted_fields": {"headcount": 2}, "next_question": "ок"}
    )
    a, _s, _o, _f = _build(
        state=_scoping_state(intent=Intent(dates="завтра")),
        openrouter=openrouter, cal_settings=_FakeCalSettings(),
        token_provider=_TokenProvider(), freebusy=_FreeBusy(busy=()),
    )
    r = await a.try_answer(
        question="давайте во второй половине дня, нас двое", ctx=_ctx()
    )
    assert (r.text or "").startswith("Да, есть свободное время")  # mid-scoping vague


@pytest.mark.asyncio
async def test_vague_window_fully_busy_falls_back_to_nearest_free() -> None:
    # The whole afternoon window is busy, but the morning is free → propose the
    # day's nearest-free as a fallback (never decline).
    busy = (
        BusyInterval(
            start=datetime(2026, 5, 30, 12, 0, tzinfo=_TOMORROW_MOSCOW),
            end=datetime(2026, 5, 30, 19, 0, tzinfo=_TOMORROW_MOSCOW),
        ),
    )
    a, _s, _o, _f = _build(
        state=None, cal_settings=_FakeCalSettings(),
        token_provider=_TokenProvider(), freebusy=_FreeBusy(busy=busy),
    )
    r = await a._maybe_answer_vague_window(
        question="завтра во второй половине дня", ctx=_ctx(),
        merged_intent=Intent(dates="завтра"), stage_before=STAGE_SCOPING,
    )
    assert r is not None and "09:00" in (r.text or "")  # morning fallback


# --- Round-15: vague-window on complete (12.61), preserve date (12.62), capacity derive (12.63)


class _StubRag:
    def __init__(self, chunks: list[RagChunk]) -> None:
        self._chunks = chunks

    def retrieve(self, *, query: str, limit: int = 3, project_id=None) -> list[RagChunk]:
        return list(self._chunks)


def _complete_scoping_state() -> dict[str, Any]:
    intent = Intent(headcount=2, vehicle_count=1, difficulty="новичок", drivers="сами")
    return {
        "chat_id": _CHAT_ID,
        "project_id": _PROJECT_ID,
        "current_stage": STAGE_SCOPING,
        "collected_intent": intent.to_dict(),
        "last_proposal": None,
    }


@pytest.mark.asyncio
async def test_complete_booking_vague_window_proposes_slot() -> None:
    # Story 12.61 — a vague time on an ALREADY-COMPLETE booking proposes a
    # concrete window slot, not the generic ask-for-time.
    openrouter = _FakeOpenRouter()
    openrouter.queue_response(
        {"extracted_fields": {"dates": "завтра во второй половине дня"}, "next_question": "ок"}
    )
    answerer, _s, _, _ = _build(
        state=_complete_scoping_state(),
        openrouter=openrouter,
        cal_settings=_FakeCalSettings(),
        token_provider=_TokenProvider(),
        freebusy=_FreeBusy(busy=()),  # afternoon free
    )
    result = await answerer.try_answer(
        question="Хотим завтра во второй половине дня", ctx=_ctx()
    )
    text = result.text or ""
    assert "Да, есть свободное время" in text  # window-propose, not the bland ask
    assert text != ASK_FOR_TIME_LINE


@pytest.mark.asyncio
async def test_awaiting_time_bare_time_preserves_prior_date() -> None:
    # Story 12.62 — after asking only for the time (date known), a bare-time
    # reply must re-attach the prior date so the slot check still runs.
    openrouter = _FakeOpenRouter()
    openrouter.queue_response(
        {"extracted_fields": {"dates": "в 14:00"}, "next_question": "ок"}
    )
    state = {
        "chat_id": _CHAT_ID,
        "project_id": _PROJECT_ID,
        "current_stage": "awaiting_time",
        "collected_intent": Intent(dates="завтра", headcount=2).to_dict(),
        "last_proposal": None,
    }
    answerer, _s, _, freebusy = _build(
        state=state,
        openrouter=openrouter,
        cal_settings=_FakeCalSettings(),
        token_provider=_TokenProvider(),
        freebusy=_FreeBusy(busy=_busy_blocks_tomorrow_14()),  # завтра 14:00 busy
    )
    result = await answerer.try_answer(question="в 14:00", ctx=_ctx())
    # The date «завтра» was preserved + combined with 14:00 → busy verdict
    # (without preservation, "в 14:00" alone is unparseable → a blind handoff).
    assert SLOT_BUSY_LINE in (result.text or "")
    assert freebusy.calls == 1


def test_parse_headcount_words_and_digits() -> None:
    assert _parse_headcount("Нас восемь человек") == 8
    assert _parse_headcount("Нас 12 человек") == 12
    assert _parse_headcount("нас двое") == 2
    assert _parse_headcount("просто вопрос") is None


def test_parse_buggy_seats_is_buggy_specific() -> None:
    assert _parse_buggy_seats("Багги - до 4 человек, Ивановский водопад") == 4
    assert _parse_buggy_seats("4 человека на одну багги") == 4
    # A quadbike capacity must NOT be read as buggy capacity.
    assert _parse_buggy_seats("3 командных квадроцикла (2 по 2 чел.)") is None
    assert _parse_buggy_seats("Багги Yamaha Viking 700") is None


@pytest.mark.asyncio
async def test_capacity_derived_from_catalog_seats() -> None:
    # Story 12.63 — with a buggy capacity in the catalog, derive the count.
    answerer = SalesPersonaAnswerer(
        state_repo=_FakeStateRepo(initial=None),
        services_repo=_NoOpServicesRepo(),
        openrouter=_FakeOpenRouter(),
        normalizer=get_russian_normalizer(),
        clock=lambda: _NOW,
        bot_persona_getter=lambda: "Анна",
        rag_retriever=_StubRag(
            [RagChunk(id=1, source_id="kb", chunk_text="Багги - до 4 человек", score=0.9)]
        ),
        calendar_settings_repo=_FakeCalSettings(),
        calendar_token_provider=_TokenProvider(),
        calendar_freebusy_client=_FreeBusy(),
        operator_chat_resolver=lambda op: 42,
    )
    result = await answerer.try_answer(
        question="Нас восемь человек, сколько багги нам понадобится?", ctx=_ctx()
    )
    assert result.text == "На 8 человек понадобится примерно 2 багги."  # ceil(8/4)
    assert result.metadata.get("escalate") is not True  # answered, not escalated


@pytest.mark.asyncio
async def test_capacity_no_catalog_data_escalates() -> None:
    # No RAG / no buggy capacity → fall back to the HITL escalation.
    answerer, _s, _, _ = _build(state=None, cal_settings=_FakeCalSettings())
    result = await answerer.try_answer(
        question="Нас восемь человек, сколько багги нам понадобится?", ctx=_ctx()
    )
    assert result.text == CAPACITY_ESCALATION_LINE
    assert result.metadata.get("hitl_reason") == HITL_REASON_CAPACITY


@pytest.mark.asyncio
async def test_capacity_catalog_without_buggy_capacity_escalates() -> None:
    # RAG present but no buggy-capacity chunk → no derivation → HITL escalation.
    answerer = SalesPersonaAnswerer(
        state_repo=_FakeStateRepo(initial=None),
        services_repo=_NoOpServicesRepo(),
        openrouter=_FakeOpenRouter(),
        normalizer=get_russian_normalizer(),
        clock=lambda: _NOW,
        bot_persona_getter=lambda: "Анна",
        rag_retriever=_StubRag(
            [RagChunk(id=1, source_id="kb", chunk_text="Квадроцикл - 2 чел.", score=0.9)]
        ),
        calendar_settings_repo=_FakeCalSettings(),
        calendar_token_provider=_TokenProvider(),
        calendar_freebusy_client=_FreeBusy(),
        operator_chat_resolver=lambda op: 42,
    )
    result = await answerer.try_answer(
        question="Нас 8 человек, сколько багги нужно?", ctx=_ctx()
    )
    assert result.text == CAPACITY_ESCALATION_LINE  # derivation found no buggy seats
    assert result.metadata.get("hitl_reason") == HITL_REASON_CAPACITY


def test_preserve_prior_date_noop_when_no_prior_date() -> None:
    # A bare-time reply with no prior date to attach → returned unchanged.
    answerer, _s, _, _ = _build(state=None)
    merged = answerer._preserve_prior_date(
        merged=Intent(dates="в 14:00"),
        state={"collected_intent": Intent(dates=None).to_dict()},
        ctx=_ctx(),
    )
    assert merged.dates == "в 14:00"


@pytest.mark.asyncio
async def test_capacity_question_without_headcount_escalates() -> None:
    # A capacity question with no parseable headcount → can't derive → HITL.
    answerer, _s, _, _ = _build(state=None, cal_settings=_FakeCalSettings())
    result = await answerer.try_answer(
        question="Сколько багги нужно для поездки?", ctx=_ctx()
    )
    assert result.text == CAPACITY_ESCALATION_LINE


# --- Round-16: invalid date (R16-1), gratitude (R16-4), eligibility (R16-3), multi-date (R16-2)


def test_names_invalid_date_flags_impossible_only() -> None:
    tz = ZoneInfo("Europe/Moscow")
    now = datetime(2026, 6, 4, 9, 0, tzinfo=tz)

    def inv(t: str) -> bool:
        return names_invalid_date(t, now=now, project_tz=tz)

    assert inv("Можно записаться 31 июня в 14:00 на багги?")
    assert inv("31.06 в 14:00")
    assert inv("30 февраля в 10:00")
    assert not inv("5 июня в 14:00")  # valid
    assert not inv("завтра в 14.30")  # HH.MM clock, not a date
    assert not inv("31 мая в 14:00")  # May has 31 days


@pytest.mark.asyncio
async def test_invalid_date_clarifies_not_handoff() -> None:
    answerer, _s, openrouter, freebusy = _build(
        state=None, cal_settings=_FakeCalSettings()
    )
    result = await answerer.try_answer(
        question="Можно записаться 31 июня в 14:00 на багги, нас двое?", ctx=_ctx()
    )
    assert result.text == INVALID_DATE_CLARIFY_LINE
    assert "передам" not in (result.text or "").lower()  # not a booking handoff
    assert openrouter.calls == []  # deterministic, no LLM


def test_is_gratitude_pure_thanks_only() -> None:
    assert is_gratitude("Спасибо большое, вы очень помогли!")
    assert is_gratitude("спасибо")
    assert not is_gratitude("всё, спасибо")  # decline → closure, not chit-chat
    assert not is_gratitude("нет, спасибо")
    assert not is_gratitude("А завтра свободно?")


@pytest.mark.asyncio
async def test_gratitude_gets_ack_not_handoff() -> None:
    state = {
        "chat_id": _CHAT_ID,
        "project_id": _PROJECT_ID,
        "current_stage": STAGE_CLOSING,
        "collected_intent": Intent().to_dict(),
        "last_proposal": None,
    }
    answerer, _s, openrouter, _ = _build(state=state, cal_settings=_FakeCalSettings())
    result = await answerer.try_answer(
        question="Спасибо большое, вы очень помогли!", ctx=_ctx()
    )
    assert result.text == GRATITUDE_ACK_LINE
    assert result.metadata.get("escalate") is not True
    assert openrouter.calls == []


def test_is_eligibility_question_matches_policy_questions() -> None:
    assert is_eligibility_question("А можно кататься на багги с ребёнком 5 лет?")
    assert is_eligibility_question("Нужны ли права на багги?")
    assert not is_eligibility_question("Запишите багги завтра в 14:00, нас двое")
    assert not is_eligibility_question("А в 16:30 свободно?")


@pytest.mark.asyncio
async def test_eligibility_no_policy_defers_not_handoff() -> None:
    # No RAG policy → _answer_concept_via_rag escalates as unknown (handled=False),
    # so the inbound defers to a human — NOT a booking handoff.
    answerer, _s, _, _ = _build(state=None, cal_settings=_FakeCalSettings())
    result = await answerer.try_answer(
        question="А можно кататься на багги с ребёнком 5 лет?", ctx=_ctx()
    )
    assert result.handled is False
    assert result.metadata.get("skip_reason") == "concept_unknown"


@pytest.mark.asyncio
async def test_eligibility_grounded_when_policy_in_catalog() -> None:
    openrouter = _FakeOpenRouter()
    openrouter.queue_response({"text": "Да, с детьми от 7 лет можно."})
    answerer = SalesPersonaAnswerer(
        state_repo=_FakeStateRepo(initial=None),
        services_repo=_NoOpServicesRepo(),
        openrouter=openrouter,
        normalizer=get_russian_normalizer(),
        clock=lambda: _NOW,
        bot_persona_getter=lambda: "Анна",
        rag_retriever=_StubRag(
            [RagChunk(id=1, source_id="kb", chunk_text="Дети допускаются с 7 лет.", score=0.95)]
        ),
        grounding_threshold_getter=lambda: 0.6,
        calendar_settings_repo=_FakeCalSettings(),
        calendar_token_provider=_TokenProvider(),
        calendar_freebusy_client=_FreeBusy(),
        operator_chat_resolver=lambda op: 42,
    )
    result = await answerer.try_answer(
        question="Можно с ребёнком 5 лет?", ctx=_ctx()
    )
    assert result.text == "Да, с детьми от 7 лет можно."  # grounded from the catalog


@pytest.mark.asyncio
async def test_multi_date_gives_per_day_verdict() -> None:
    openrouter = _FakeOpenRouter()
    openrouter.queue_response({"extracted_fields": {}, "next_question": "ок"})
    answerer, _s, _, _ = _build(
        state=None,
        openrouter=openrouter,
        cal_settings=_FakeCalSettings(),
        token_provider=_TokenProvider(),
        freebusy=_FreeBusy(busy=()),  # both days free
    )
    result = await answerer.try_answer(
        question="Можно в субботу или в воскресенье в 12:00 на багги?", ctx=_ctx()
    )
    text = result.text or ""
    assert "30 мая" in text and "31 мая" in text  # both candidate days named
    assert "свободно" in text
    assert "Какой день" in text
    assert result.metadata.get("escalate") is not True


@pytest.mark.asyncio
async def test_multi_date_one_busy_one_free() -> None:
    openrouter = _FakeOpenRouter()
    openrouter.queue_response({"extracted_fields": {}, "next_question": "ок"})
    busy = (
        BusyInterval(
            start=datetime(2026, 5, 30, 11, 0, tzinfo=_TOMORROW_MOSCOW),
            end=datetime(2026, 5, 30, 13, 0, tzinfo=_TOMORROW_MOSCOW),
        ),
    )
    answerer, _s, _, _ = _build(
        state=None,
        openrouter=openrouter,
        cal_settings=_FakeCalSettings(),
        token_provider=_TokenProvider(),
        freebusy=_FreeBusy(busy=busy),  # 30 May 12:00 busy, 31 May free
    )
    result = await answerer.try_answer(
        question="Можно в субботу или в воскресенье в 12:00 на багги?", ctx=_ctx()
    )
    text = result.text or ""
    assert "занято" in text and "свободно" in text


@pytest.mark.asyncio
async def test_multi_date_no_time_falls_through() -> None:
    # «или» but no clock → not a multi-date verdict (slot-fill flow handles it).
    openrouter = _FakeOpenRouter()
    openrouter.queue_response({"extracted_fields": {}, "next_question": "Во сколько?"})
    answerer, _s, _, _ = _build(
        state=None, openrouter=openrouter, cal_settings=_FakeCalSettings(),
        token_provider=_TokenProvider(),
    )
    result = await answerer.try_answer(
        question="Можно в субботу или в воскресенье на багги?", ctx=_ctx()
    )
    assert "Какой день" not in (result.text or "")  # no per-day verdict without a time


@pytest.mark.asyncio
async def test_multi_date_mid_scoping_per_day_verdict() -> None:
    # Covers the scoping-stage multi-date hook.
    openrouter = _FakeOpenRouter()
    openrouter.queue_response({"extracted_fields": {}, "next_question": "ок"})
    answerer, _s, _, _ = _build(
        state=_scoping_state(intent=Intent(headcount=2)),
        openrouter=openrouter,
        cal_settings=_FakeCalSettings(),
        token_provider=_TokenProvider(),
        freebusy=_FreeBusy(busy=()),
    )
    result = await answerer.try_answer(
        question="Можно в субботу или в воскресенье в 12:00 на багги?", ctx=_ctx()
    )
    assert "Какой день" in (result.text or "")


@pytest.mark.asyncio
async def test_multi_date_calendar_disabled_falls_through() -> None:
    openrouter = _FakeOpenRouter()
    openrouter.queue_response({"extracted_fields": {}, "next_question": "ок"})
    answerer, _s, _, _ = _build(
        state=None,
        openrouter=openrouter,
        cal_settings=_FakeCalSettings(enabled=False),  # cal context is None
        token_provider=_TokenProvider(),
    )
    result = await answerer.try_answer(
        question="Можно в субботу или в воскресенье в 12:00 на багги?", ctx=_ctx()
    )
    assert "Какой день" not in (result.text or "")


@pytest.mark.asyncio
async def test_multi_date_single_date_falls_through() -> None:
    openrouter = _FakeOpenRouter()
    openrouter.queue_response(
        {"extracted_fields": {"dates": "завтра в 12:00"}, "next_question": "ок"}
    )
    answerer, _s, _, _ = _build(
        state=None,
        openrouter=openrouter,
        cal_settings=_FakeCalSettings(),
        token_provider=_TokenProvider(),
        freebusy=_FreeBusy(busy=()),
    )
    # «или» present but only one parseable date → not a multi-date verdict.
    result = await answerer.try_answer(
        question="Можно в субботу или попозже в 12:00 на багги?", ctx=_ctx()
    )
    assert "Какой день" not in (result.text or "")


@pytest.mark.asyncio
async def test_multi_date_unverifiable_falls_through() -> None:
    # Calendar can't verify (reconnect needed) → no per-day verdict.
    openrouter = _FakeOpenRouter()
    openrouter.queue_response({"extracted_fields": {}, "next_question": "ок"})
    answerer, _s, _, _ = _build(
        state=None,
        openrouter=openrouter,
        cal_settings=_FakeCalSettings(),
        token_provider=_RaisingTokenProvider(),  # → STATUS_NOT_CONNECTED
        freebusy=_FreeBusy(busy=()),
    )
    result = await answerer.try_answer(
        question="Можно в субботу или в воскресенье в 12:00 на багги?", ctx=_ctx()
    )
    assert "Какой день" not in (result.text or "")


# --- Story 12.73 (round-18 R18-1): explicit past date → reject, not handoff ----


@pytest.mark.asyncio
async def test_past_date_is_rejected_not_handed_off() -> None:
    # _NOW is Fri 29 May 2026; «вчера» = 28 May (past).
    answerer, _s, openrouter, _ = _build(state=None, cal_settings=_FakeCalSettings())
    result = await answerer.try_answer(
        question="запишите нас на вчера в 14:00 на багги", ctx=_ctx()
    )
    assert result.text == PAST_DATE_CLARIFY_LINE
    assert "передам" not in (result.text or "").lower()  # not a booking handoff
    assert result.metadata.get("suppress_followup") is True
    assert result.metadata.get("escalate") is not True
    assert openrouter.calls == []  # deterministic, no LLM


@pytest.mark.asyncio
async def test_future_date_not_treated_as_past() -> None:
    openrouter = _FakeOpenRouter()
    openrouter.queue_response(
        {"extracted_fields": {"dates": "завтра в 14:00"}, "next_question": "Сколько человек?"}
    )
    answerer, _s, _, _ = _build(
        state=None,
        openrouter=openrouter,
        cal_settings=_FakeCalSettings(),
        token_provider=_TokenProvider(),
        freebusy=_FreeBusy(busy=()),
    )
    result = await answerer.try_answer(
        question="запишите нас на завтра в 14:00 на багги", ctx=_ctx()
    )
    assert result.text != PAST_DATE_CLARIFY_LINE


# --- Story 12.74 (round-18 R18-6): gibberish → Russian clarification ----------


def test_is_gibberish_only_for_unintelligible_input() -> None:
    n = get_russian_normalizer()
    assert is_gibberish("asdfgh qwerty фыва 123 ???", normalizer=n)
    assert not is_gibberish("хочу багги завтра", normalizer=n)  # real words
    assert not is_gibberish("asdfgh qwerty 123", normalizer=n)  # no Cyrillic → skip
    assert not is_gibberish("заброниравать баги завтре", normalizer=n)  # «баги» known
    assert not is_gibberish("я 1 2", normalizer=n)  # Cyrillic but no 2+ letter token


@pytest.mark.asyncio
async def test_gibberish_gets_russian_clarification_not_handoff() -> None:
    answerer, _s, openrouter, _ = _build(state=None, cal_settings=_FakeCalSettings())
    result = await answerer.try_answer(
        question="asdfgh qwerty фыва 123 ???", ctx=_ctx()
    )
    assert result.text == GIBBERISH_CLARIFY_LINE
    assert "Thank you" not in (result.text or "")  # not the EN handoff
    assert result.metadata.get("escalate") is not True
    assert openrouter.calls == []


@pytest.mark.asyncio
async def test_gibberish_in_pitching_clarifies_not_handoff() -> None:
    # The live R18-6: chat parked in pitching, a gibberish reply must clarify,
    # not fall to the pitching-followup booking handoff (in English).
    state = {
        "chat_id": _CHAT_ID,
        "project_id": _PROJECT_ID,
        "current_stage": STAGE_PITCHING,
        "collected_intent": Intent(dates="завтра в 14:00", headcount=2).to_dict(),
        "last_proposal": {"alternative_iso": "2026-05-30T08:00:00+03:00"},
    }
    answerer, _s, openrouter, _ = _build(state=state, cal_settings=_FakeCalSettings())
    result = await answerer.try_answer(
        question="asdfgh qwerty фыва 123 ???", ctx=_ctx()
    )
    assert result.text == GIBBERISH_CLARIFY_LINE
    assert openrouter.calls == []


# --- Story 12.77 (round-18 R18-5): two bookings «…и…» → per-slot verdicts ------


@pytest.mark.asyncio
async def test_two_bookings_in_one_message_gets_both_verdicts() -> None:
    # «завтра в 12:00 и послезавтра в 15:00» — two concrete bookings, each its own
    # time. _NOW Fri 29 May → завтра 30 May, послезавтра 31 May (both free).
    openrouter = _FakeOpenRouter()
    openrouter.queue_response({"extracted_fields": {}, "next_question": "ок"})
    answerer, _s, _, _ = _build(
        state=None,
        openrouter=openrouter,
        cal_settings=_FakeCalSettings(),
        token_provider=_TokenProvider(),
        freebusy=_FreeBusy(busy=()),
    )
    result = await answerer.try_answer(
        question="можно завтра в 12:00 и послезавтра в 15:00 на багги?", ctx=_ctx()
    )
    text = result.text or ""
    assert "30 мая" in text and "12:00" in text
    assert "31 мая" in text and "15:00" in text
    assert text.count("свободно") == 2
    assert result.metadata.get("suppress_followup") is True
    assert result.metadata.get("escalate") is not True


@pytest.mark.asyncio
async def test_single_booking_not_treated_as_two() -> None:
    # A single booking with a stray «и» («я и друг») must NOT trigger the
    # two-bookings path (only one parseable date+time).
    openrouter = _FakeOpenRouter()
    openrouter.queue_response(
        {"extracted_fields": {"dates": "завтра в 12:00"}, "next_question": "Сколько человек?"}
    )
    answerer, _s, _, _ = _build(
        state=None,
        openrouter=openrouter,
        cal_settings=_FakeCalSettings(),
        token_provider=_TokenProvider(),
        freebusy=_FreeBusy(busy=()),
    )
    result = await answerer.try_answer(
        question="я и друг хотим багги завтра в 12:00", ctx=_ctx()
    )
    assert "Подтвердить оба" not in (result.text or "")


@pytest.mark.asyncio
async def test_two_bookings_one_busy_one_free() -> None:
    openrouter = _FakeOpenRouter()
    openrouter.queue_response({"extracted_fields": {}, "next_question": "ок"})
    busy = (
        BusyInterval(
            start=datetime(2026, 5, 30, 11, 0, tzinfo=_TOMORROW_MOSCOW),
            end=datetime(2026, 5, 30, 13, 0, tzinfo=_TOMORROW_MOSCOW),
        ),
    )
    answerer, _s, _, _ = _build(
        state=None,
        openrouter=openrouter,
        cal_settings=_FakeCalSettings(),
        token_provider=_TokenProvider(),
        freebusy=_FreeBusy(busy=busy),  # 30 May 12:00 busy, 31 May 15:00 free
    )
    result = await answerer.try_answer(
        question="можно завтра в 12:00 и послезавтра в 15:00 на багги?", ctx=_ctx()
    )
    text = result.text or ""
    assert "занято" in text and "свободно" in text


@pytest.mark.asyncio
async def test_two_bookings_calendar_disabled_falls_through() -> None:
    openrouter = _FakeOpenRouter()
    openrouter.queue_response(
        {"extracted_fields": {"dates": "завтра в 12:00"}, "next_question": "ок"}
    )
    answerer, _s, _, _ = _build(
        state=None,
        openrouter=openrouter,
        cal_settings=_FakeCalSettings(enabled=False),  # cal context is None
        token_provider=_TokenProvider(),
    )
    result = await answerer.try_answer(
        question="можно завтра в 12:00 и послезавтра в 15:00 на багги?", ctx=_ctx()
    )
    assert "Подтвердить оба" not in (result.text or "")


@pytest.mark.asyncio
async def test_two_bookings_unverifiable_falls_through() -> None:
    openrouter = _FakeOpenRouter()
    openrouter.queue_response({"extracted_fields": {}, "next_question": "ок"})
    answerer, _s, _, _ = _build(
        state=None,
        openrouter=openrouter,
        cal_settings=_FakeCalSettings(),
        token_provider=_RaisingTokenProvider(),  # → STATUS_NOT_CONNECTED
        freebusy=_FreeBusy(busy=()),
    )
    result = await answerer.try_answer(
        question="можно завтра в 12:00 и послезавтра в 15:00 на багги?", ctx=_ctx()
    )
    assert "Подтвердить оба" not in (result.text or "")


@pytest.mark.asyncio
async def test_two_bookings_mid_scoping() -> None:
    # Covers the scoping-stage two-bookings hook.
    openrouter = _FakeOpenRouter()
    openrouter.queue_response({"extracted_fields": {}, "next_question": "ок"})
    answerer, _s, _, _ = _build(
        state=_scoping_state(intent=Intent(headcount=2)),
        openrouter=openrouter,
        cal_settings=_FakeCalSettings(),
        token_provider=_TokenProvider(),
        freebusy=_FreeBusy(busy=()),
    )
    result = await answerer.try_answer(
        question="можно завтра в 12:00 и послезавтра в 15:00 на багги?", ctx=_ctx()
    )
    assert "Подтвердить оба" in (result.text or "")


# --- Story 12.78 (round-19 R19-3): vague lower/upper-bound time → clarify -------


def test_detect_vague_window_handles_open_bounds() -> None:
    assert detect_vague_window("завтра после 15:00") == (15, 22)
    assert detect_vague_window("завтра до 14:00") == (8, 14)
    # The existing word-window is unchanged.
    assert detect_vague_window("завтра во второй половине дня") == (12, 18)


@pytest.mark.asyncio
async def test_vague_lower_bound_proposes_slot_not_completes() -> None:
    # «после 15:00» must NOT be booked at a bare 15:00; the bot proposes a
    # concrete slot at/after 15:00 and asks — reusing the R14-1 clarify path.
    openrouter = _FakeOpenRouter()
    openrouter.queue_response(
        {"extracted_fields": {"dates": "завтра после 15:00"}, "next_question": "ок"}
    )
    answerer, _s, _, _ = _build(
        state=None,
        openrouter=openrouter,
        cal_settings=_FakeCalSettings(),
        token_provider=_TokenProvider(),
        freebusy=_FreeBusy(busy=()),  # 30 May fully free
    )
    result = await answerer.try_answer(
        question="хотим завтра на багги где-то после 15:00", ctx=_ctx()
    )
    text = result.text or ""
    assert "15:00" in text  # first free slot at/after 15:00 is proposed
    assert "удобное время" in text  # the clarify offer, not a bare completion
    assert result.metadata["stage_after"] == STAGE_PITCHING
    assert result.metadata.get("escalate") is not True  # not completed/escalated


@pytest.mark.asyncio
async def test_vague_upper_bound_proposes_slot() -> None:
    openrouter = _FakeOpenRouter()
    openrouter.queue_response(
        {"extracted_fields": {"dates": "завтра до 11:00"}, "next_question": "ок"}
    )
    answerer, _s, _, _ = _build(
        state=None,
        openrouter=openrouter,
        cal_settings=_FakeCalSettings(),
        token_provider=_TokenProvider(),
        freebusy=_FreeBusy(busy=()),
    )
    result = await answerer.try_answer(
        question="можно завтра на багги до 11:00?", ctx=_ctx()
    )
    text = result.text or ""
    assert "удобное время" in text  # proposes + clarifies, never a bare completion
    assert result.metadata["stage_after"] == STAGE_PITCHING


@pytest.mark.asyncio
async def test_global_calendar_block_is_shared_across_services() -> None:
    # R19-1 — confirmed business rule: calendar blocking is GLOBAL per day/time
    # (one operator, one activity at a time), not per-service. Any busy event
    # blocks the requested buggy slot → занято.
    openrouter = _FakeOpenRouter()
    openrouter.queue_response(
        {"extracted_fields": {"dates": "завтра в 12:00"}, "next_question": "Сколько человек?"}
    )
    busy = (
        BusyInterval(
            start=datetime(2026, 5, 30, 11, 0, tzinfo=_TOMORROW_MOSCOW),
            end=datetime(2026, 5, 30, 13, 0, tzinfo=_TOMORROW_MOSCOW),
        ),
    )
    answerer, _s, _, _ = _build(
        state=None,
        openrouter=openrouter,
        cal_settings=_FakeCalSettings(),
        token_provider=_TokenProvider(),
        freebusy=_FreeBusy(busy=busy),
    )
    result = await answerer.try_answer(
        question="можно завтра в 12:00 на багги?", ctx=_ctx()
    )
    assert SLOT_BUSY_LINE in (result.text or "")


# --- Story 12.80/12.81 (round-20 R20-1/R20-2): FAQ intents, not booking --------


def test_is_working_hours_question() -> None:
    assert is_working_hours_question("А до скольки вы вообще работаете?")
    assert is_working_hours_question("во сколько открываетесь?")
    assert is_working_hours_question("какой у вас график работы?")
    assert not is_working_hours_question("сколько стоит?")
    assert not is_working_hours_question("сколько по времени длится поездка?")


def test_is_duration_question() -> None:
    assert is_duration_question("Сколько по времени длится поездка на багги?")
    assert is_duration_question("как долго катаемся?")
    assert not is_duration_question("сколько стоит?")
    assert not is_duration_question("до скольки работаете?")


@pytest.mark.asyncio
async def test_working_hours_faq_answers_from_config() -> None:
    # The rule's working_hours are 09:00–20:00 → answered directly, not a handoff.
    answerer, _s, openrouter, _ = _build(state=None, cal_settings=_FakeCalSettings())
    result = await answerer.try_answer(
        question="А до скольки вы вообще работаете?", ctx=_ctx()
    )
    assert result.text == WORKING_HOURS_LINE.format(open="09:00", close="20:00")
    assert "передам" not in (result.text or "").lower()  # not a booking handoff
    assert result.metadata.get("escalate") is not True
    assert openrouter.calls == []  # deterministic, no LLM


@pytest.mark.asyncio
async def test_working_hours_faq_defers_when_calendar_off() -> None:
    answerer, _s, _, _ = _build(
        state=None, cal_settings=_FakeCalSettings(enabled=False)
    )
    result = await answerer.try_answer(
        question="до скольки работаете?", ctx=_ctx()
    )
    assert result.text == FAQ_DEFER_LINE  # no config → defer as a question
    assert "на какую дату" not in (result.text or "")


@pytest.mark.asyncio
async def test_duration_faq_defers_not_books() -> None:
    answerer, _s, openrouter, _ = _build(state=None, cal_settings=_FakeCalSettings())
    result = await answerer.try_answer(
        question="Сколько по времени длится поездка на багги?", ctx=_ctx()
    )
    assert result.text == FAQ_DEFER_LINE
    assert "на какую дату" not in (result.text or "")  # NOT a date/time ask
    assert "передам детали" not in (result.text or "").lower()  # not a booking handoff
    assert openrouter.calls == []


# --- Story 12.82 (round-20 R20-4): combined price + availability ---------------


@pytest.mark.asyncio
async def test_combined_price_and_availability_returns_both() -> None:
    # «Сколько стоит и свободно ли завтра в 12:00?» → BOTH the availability
    # verdict (занято) AND the price handling (here a defer), not price alone.
    busy = (
        BusyInterval(
            start=datetime(2026, 5, 30, 11, 0, tzinfo=_TOMORROW_MOSCOW),
            end=datetime(2026, 5, 30, 13, 0, tzinfo=_TOMORROW_MOSCOW),
        ),
    )
    price_lookup = _StubPriceLookup(
        PriceMissing(
            payload=PriceUnknownPayload(
                service=None,
                vehicle_type=None,
                hours=None,
                original_question="Сколько стоит и свободно ли завтра в 12:00 на багги?",
            )
        )
    )
    answerer, _s, openrouter, _ = _build(
        state=None,
        cal_settings=_FakeCalSettings(),
        token_provider=_TokenProvider(),
        freebusy=_FreeBusy(busy=busy),
        price_lookup=price_lookup,
    )
    result = await answerer.try_answer(
        question="Сколько стоит и свободно ли завтра в 12:00 на багги?", ctx=_ctx()
    )
    text = result.text or ""
    assert SLOT_BUSY_LINE in text  # availability verdict present (занято)
    assert PRICING_MISS_FALLBACK in text  # price handled (defer) in the same turn
    assert len(price_lookup.calls) == 1  # the price path actually ran


@pytest.mark.asyncio
async def test_price_ask_without_concrete_slot_is_price_only() -> None:
    # A price ask with no concrete time → price-only path (combined doesn't fire).
    price_lookup = _StubPriceLookup(
        PriceMissing(
            payload=PriceUnknownPayload(
                service=None, vehicle_type=None, hours=None,
                original_question="Сколько стоит покататься на багги?",
            )
        )
    )
    openrouter = _FakeOpenRouter()
    # First-turn price goes through greeting (LLM extraction) → price-intercept.
    openrouter.queue_response({"extracted_fields": {}, "next_question": "ок"})
    answerer, _s, _, _ = _build(
        state=None,
        openrouter=openrouter,
        cal_settings=_FakeCalSettings(),
        token_provider=_TokenProvider(),
        freebusy=_FreeBusy(busy=()),
        price_lookup=price_lookup,
    )
    result = await answerer.try_answer(
        question="Сколько стоит покататься на багги?", ctx=_ctx()
    )
    assert result.text == PRICING_MISS_FALLBACK  # price only, no verdict glued on
    assert SLOT_BUSY_LINE not in (result.text or "")


def test_format_working_hours_edges() -> None:
    assert _format_working_hours(None) is None
    assert _format_working_hours({"mon": [["09:00", "18:00"]]}) == ("09:00", "18:00")
    assert _format_working_hours({"mon": [[]]}) is None  # malformed window → None


@pytest.mark.asyncio
async def test_combined_falls_through_when_unverifiable() -> None:
    # Slot can't be verified (not connected) → the combined handler returns None
    # so the price-only path takes over (no half-answer with a bogus verdict).
    price_lookup = _StubPriceLookup(
        PriceMissing(
            payload=PriceUnknownPayload(
                service=None, vehicle_type=None, hours=None, original_question="q"
            )
        )
    )
    answerer, _s, _, _ = _build(
        state=None,
        cal_settings=_FakeCalSettings(),
        token_provider=_RaisingTokenProvider(),  # → STATUS_NOT_CONNECTED
        freebusy=_FreeBusy(busy=()),
        price_lookup=price_lookup,
    )
    result = await answerer._maybe_answer_price_and_availability(
        question="Сколько стоит и свободно ли завтра в 12:00 на багги?",
        ctx=_ctx(),
        state=None,
    )
    assert result is None


@pytest.mark.asyncio
async def test_combined_falls_through_when_price_unavailable() -> None:
    # The price lookup errors → _handle_pricing returns not-handled → the combined
    # handler falls through rather than emitting a verdict with no price.
    class _RaisingPrice:
        async def lookup(self, **_kwargs):
            raise RuntimeError("rag down")

    answerer, _s, _, _ = _build(
        state=None,
        cal_settings=_FakeCalSettings(),
        token_provider=_TokenProvider(),
        freebusy=_FreeBusy(busy=()),
        price_lookup=_RaisingPrice(),
    )
    result = await answerer._maybe_answer_price_and_availability(
        question="Сколько стоит и свободно ли завтра в 12:00 на багги?",
        ctx=_ctx(),
        state=None,
    )
    assert result is None


# --- Story 12.84 (round-21 R21-2): payment/location/what-to-bring FAQ defer ----


def test_is_info_faq_question() -> None:
    assert is_info_faq_question("А оплатить картой можно или только наличными?")
    assert is_info_faq_question("А где вы находитесь, как до вас добраться?")
    assert is_info_faq_question("А что взять с собой?")
    assert not is_info_faq_question("сколько стоит?")
    assert not is_info_faq_question("свободно ли завтра в 12:00?")
    assert not is_info_faq_question("хочу багги завтра в 14:00")


@pytest.mark.asyncio
async def test_payment_faq_defers_not_books() -> None:
    # Mid-funnel (closing) so the old path would have handed off; the FAQ branch
    # fires first → a question-style defer, never a booking handoff.
    state = {
        "chat_id": _CHAT_ID,
        "project_id": _PROJECT_ID,
        "current_stage": STAGE_CLOSING,
        "collected_intent": Intent().to_dict(),
        "last_proposal": None,
    }
    answerer, _s, openrouter, _ = _build(state=state, cal_settings=_FakeCalSettings())
    result = await answerer.try_answer(
        question="А оплатить картой можно или только наличными?", ctx=_ctx()
    )
    assert result.text == FAQ_DEFER_LINE
    assert "передам" not in (result.text or "").lower()  # not a booking handoff
    assert openrouter.calls == []


@pytest.mark.asyncio
async def test_location_faq_defers_not_books() -> None:
    state = {
        "chat_id": _CHAT_ID,
        "project_id": _PROJECT_ID,
        "current_stage": STAGE_PITCHING,
        "collected_intent": Intent(dates="завтра в 14:00").to_dict(),
        "last_proposal": {"alternative_iso": "2026-05-30T08:00:00+03:00"},
    }
    answerer, _s, openrouter, _ = _build(state=state, cal_settings=_FakeCalSettings())
    result = await answerer.try_answer(
        question="А где вы находитесь, как до вас добраться?", ctx=_ctx()
    )
    assert result.text == FAQ_DEFER_LINE
    assert "на какую дату" not in (result.text or "")
    assert openrouter.calls == []


# --- Story 12.85 (round-21 R21-1): mixed-service request → one-at-a-time clarify


def test_is_mixed_service_request() -> None:
    assert is_mixed_service_request(
        "завтра в 15:00 двоих на багги и ещё двоих на квадроциклах"
    )
    assert not is_mixed_service_request("хочу багги завтра в 15:00")  # one service
    assert not is_mixed_service_request("сколько стоит квадроцикл?")  # one service


@pytest.mark.asyncio
async def test_mixed_service_request_clarifies_one_at_a_time() -> None:
    openrouter = _FakeOpenRouter()
    openrouter.queue_response({"extracted_fields": {}, "next_question": "ок"})
    answerer, _s, _, _ = _build(
        state=None,
        openrouter=openrouter,
        cal_settings=_FakeCalSettings(),
        token_provider=_TokenProvider(),
        freebusy=_FreeBusy(busy=()),
    )
    result = await answerer.try_answer(
        question="Можно завтра в 15:00 двоих на багги и ещё двоих на квадроциклах?",
        ctx=_ctx(),
    )
    assert result.text == MIXED_SERVICE_CLARIFY_LINE
    assert SLOT_FREE_HANDOFF_LINE not in (result.text or "")  # not a single verdict
    assert result.metadata.get("escalate") is not True


@pytest.mark.asyncio
async def test_mixed_service_mid_scoping_clarifies() -> None:
    # Covers the scoping-stage mixed-service hook.
    openrouter = _FakeOpenRouter()
    openrouter.queue_response({"extracted_fields": {}, "next_question": "ок"})
    answerer, _s, _, _ = _build(
        state=_scoping_state(intent=Intent(headcount=2)),
        openrouter=openrouter,
        cal_settings=_FakeCalSettings(),
        token_provider=_TokenProvider(),
        freebusy=_FreeBusy(busy=()),
    )
    result = await answerer.try_answer(
        question="давайте двоих на багги и двоих на квадроциклах", ctx=_ctx()
    )
    assert result.text == MIXED_SERVICE_CLARIFY_LINE


# --- Story 12.86 (round-23 R23-3): multi-turn time change re-runs the check ----


@pytest.mark.asyncio
async def test_pitching_time_change_after_free_handoff_rechecks() -> None:
    # Turn 1 confirmed «9 июня 14:00» (free) → parked in pitching with NO offered
    # slot (last_proposal=None). Turn 2 «лучше в 12:00 в тот же день» must carry
    # the prior date (9 June) and RE-CHECK → 9 June 12:00 ∈ 11–13 → занято.
    state = {
        "chat_id": _CHAT_ID,
        "project_id": _PROJECT_ID,
        "current_stage": STAGE_PITCHING,
        "collected_intent": Intent(dates="9 июня в 14:00", headcount=2).to_dict(),
        "last_proposal": None,  # a free handoff offered no alternative
    }
    busy = (
        BusyInterval(
            start=datetime(2026, 6, 9, 11, 0, tzinfo=_TOMORROW_MOSCOW),
            end=datetime(2026, 6, 9, 13, 0, tzinfo=_TOMORROW_MOSCOW),
        ),
    )
    answerer, _s, _, freebusy = _build(
        state=state,
        cal_settings=_FakeCalSettings(),
        token_provider=_TokenProvider(),
        freebusy=_FreeBusy(busy=busy),
    )
    result = await answerer.try_answer(
        question="Ой, а давайте лучше в 12:00 в тот же день", ctx=_ctx()
    )
    text = result.text or ""
    assert SLOT_BUSY_LINE in text  # re-checked 9 June 12:00 → занято
    assert "Ближайшее свободное время" in text  # offered an alternative
    assert freebusy.calls >= 1  # the calendar was actually re-queried


@pytest.mark.asyncio
async def test_pitching_unchanged_reply_after_free_handoff_still_hands_off() -> None:
    # A non-time reply after a free handoff still hands off (no re-check, no crash).
    state = {
        "chat_id": _CHAT_ID,
        "project_id": _PROJECT_ID,
        "current_stage": STAGE_PITCHING,
        "collected_intent": Intent(dates="9 июня в 14:00", headcount=2).to_dict(),
        "last_proposal": None,
    }
    answerer, _s, _, _ = _build(
        state=state, cal_settings=_FakeCalSettings(), token_provider=_TokenProvider()
    )
    result = await answerer.try_answer(question="хорошо, спасибо", ctx=_ctx())
    assert SLOT_BUSY_LINE not in (result.text or "")


# --- Story 12.87 (round-23 R23-2): multi-option TIME «или» → per-time verdict ---


@pytest.mark.asyncio
async def test_multi_time_or_gives_per_time_verdict() -> None:
    # «завтра в 12:00 или в 16:00» (one date, two times) → a per-TIME verdict.
    openrouter = _FakeOpenRouter()
    openrouter.queue_response({"extracted_fields": {}, "next_question": "ок"})
    answerer, _s, _, _ = _build(
        state=None,
        openrouter=openrouter,
        cal_settings=_FakeCalSettings(),
        token_provider=_TokenProvider(),
        freebusy=_FreeBusy(busy=()),  # 30 May fully free
    )
    result = await answerer.try_answer(
        question="Можно завтра в 12:00 или в 16:00 на багги?", ctx=_ctx()
    )
    text = result.text or ""
    assert "12:00" in text and "16:00" in text  # both times stated
    assert "В какое время" in text
    assert text.count("свободно") == 2


@pytest.mark.asyncio
async def test_multi_time_or_one_busy_one_free() -> None:
    openrouter = _FakeOpenRouter()
    openrouter.queue_response({"extracted_fields": {}, "next_question": "ок"})
    busy = (
        BusyInterval(
            start=datetime(2026, 5, 30, 11, 0, tzinfo=_TOMORROW_MOSCOW),
            end=datetime(2026, 5, 30, 13, 0, tzinfo=_TOMORROW_MOSCOW),
        ),
    )
    answerer, _s, _, _ = _build(
        state=None,
        openrouter=openrouter,
        cal_settings=_FakeCalSettings(),
        token_provider=_TokenProvider(),
        freebusy=_FreeBusy(busy=busy),  # 30 May 12:00 busy, 16:00 free
    )
    result = await answerer.try_answer(
        question="Можно завтра в 12:00 или в 16:00 на багги?", ctx=_ctx()
    )
    text = result.text or ""
    assert "занято" in text and "свободно" in text


# --- Story R17-3 lock-in (Y3): «через час» busy case → занято -----------------


@pytest.mark.asyncio
async def test_relative_offset_through_hour_busy_returns_zanyato() -> None:
    # _NOW = 12:00 Moscow; «через час» = 13:00. Pin a busy block there → занято.
    openrouter = _FakeOpenRouter()
    openrouter.queue_response(
        {"extracted_fields": {"dates": "через час"}, "next_question": "ок"}
    )
    busy = (
        BusyInterval(
            start=datetime(2026, 5, 29, 12, 30, tzinfo=_TOMORROW_MOSCOW),
            end=datetime(2026, 5, 29, 13, 30, tzinfo=_TOMORROW_MOSCOW),
        ),
    )
    answerer, _s, _, _ = _build(
        state=None,
        openrouter=openrouter,
        cal_settings=_FakeCalSettings(),
        token_provider=_TokenProvider(),
        freebusy=_FreeBusy(busy=busy),
    )
    result = await answerer.try_answer(
        question="можно через час на багги, нас двое, одна багги?", ctx=_ctx()
    )
    assert SLOT_BUSY_LINE in (result.text or "")


# --- Story 12.88 (round-25 R25-2): working-DAYS FAQ → answer, not booking ------


def test_is_working_days_question() -> None:
    assert is_working_days_question("А вы по воскресеньям вообще работаете?")
    assert is_working_days_question("в выходные работаете?")
    assert is_working_days_question("какие дни вы работаете?")
    assert not is_working_days_question("до скольки работаете?")  # hours, not days
    assert not is_working_days_question("сколько стоит?")


@pytest.mark.asyncio
async def test_working_days_faq_answers_from_config() -> None:
    # The rule runs all 7 days, 09:00–20:00 → answered, never a booking handoff.
    state = {
        "chat_id": _CHAT_ID,
        "project_id": _PROJECT_ID,
        "current_stage": STAGE_CLOSING,
        "collected_intent": Intent().to_dict(),
        "last_proposal": None,
    }
    answerer, _s, openrouter, _ = _build(state=state, cal_settings=_FakeCalSettings())
    result = await answerer.try_answer(
        question="А вы по воскресеньям вообще работаете?", ctx=_ctx()
    )
    assert result.text == WORKING_DAYS_LINE.format(
        days="ежедневно", open="09:00", close="20:00"
    )
    assert "передам" not in (result.text or "").lower()  # not a booking handoff
    assert openrouter.calls == []


@pytest.mark.asyncio
async def test_working_days_faq_defers_when_calendar_off() -> None:
    answerer, _s, _, _ = _build(
        state=None, cal_settings=_FakeCalSettings(enabled=False)
    )
    result = await answerer.try_answer(
        question="по воскресеньям работаете?", ctx=_ctx()
    )
    assert result.text == FAQ_DEFER_LINE


# --- Story 12.89 (round-25 R25-1): contradictory count (vehicles > people) ------


def test_is_count_inconsistent() -> None:
    assert is_count_inconsistent(Intent(headcount=2, vehicle_count=5))  # 5 > 2
    assert not is_count_inconsistent(Intent(headcount=2, vehicle_count=1))
    assert not is_count_inconsistent(Intent(headcount=2, vehicle_count=2))
    assert not is_count_inconsistent(Intent(headcount=2))  # no vehicle_count
    assert not is_count_inconsistent(Intent(vehicle_count=5))  # no headcount


@pytest.mark.asyncio
async def test_contradictory_count_clarifies_before_confirming() -> None:
    # «двое, но 5 багги» → vehicle_count(5) > headcount(2) → clarify, not «свободно».
    openrouter = _FakeOpenRouter()
    openrouter.queue_response(
        {
            "extracted_fields": {"headcount": 2, "vehicle_count": 5},
            "next_question": "ок",
        }
    )
    answerer, _s, _, _ = _build(
        state=None,
        openrouter=openrouter,
        cal_settings=_FakeCalSettings(),
        token_provider=_TokenProvider(),
        freebusy=_FreeBusy(busy=()),
    )
    result = await answerer.try_answer(
        question="Нас всего двое, но хотим сразу 5 багги, можно завтра в 15:00?",
        ctx=_ctx(),
    )
    assert result.text == COUNT_MISMATCH_CLARIFY_LINE.format(vehicles=5, people=2)
    assert SLOT_FREE_HANDOFF_LINE not in (result.text or "")  # not silently confirmed
    assert result.metadata.get("escalate") is not True


def test_format_working_days_edges() -> None:
    assert _format_working_days(None) is None
    assert _format_working_days([]) is None
    assert _format_working_days(["xyz"]) is None  # no recognised day codes
    week = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
    assert _format_working_days(week) == "ежедневно"
    assert _format_working_days(["mon", "wed", "fri"]) == "пн, ср, пт"


def test_as_positive_int_edges() -> None:
    assert _as_positive_int(True) is None  # bool is not a count
    assert _as_positive_int(3) == 3
    assert _as_positive_int(0) is None
    assert _as_positive_int("4") == 4
    assert _as_positive_int("0") is None
    assert _as_positive_int("abc") is None
    assert _as_positive_int(None) is None


@pytest.mark.asyncio
async def test_count_mismatch_mid_scoping() -> None:
    # Covers the scoping-stage count-mismatch hook.
    openrouter = _FakeOpenRouter()
    openrouter.queue_response(
        {"extracted_fields": {"vehicle_count": 5}, "next_question": "ок"}
    )
    answerer, _s, _, _ = _build(
        state=_scoping_state(intent=Intent(headcount=2)),
        openrouter=openrouter,
        cal_settings=_FakeCalSettings(),
        token_provider=_TokenProvider(),
        freebusy=_FreeBusy(busy=()),
    )
    result = await answerer.try_answer(question="хотим сразу 5 багги", ctx=_ctx())
    assert result.text == COUNT_MISMATCH_CLARIFY_LINE.format(vehicles=5, people=2)

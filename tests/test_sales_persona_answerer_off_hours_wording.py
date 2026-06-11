"""Story 12.29 — off-hours / closed-day / past requests must not say "занято".

The pure availability engine already distinguishes the reason a slot is
unavailable (``outside_working_hours`` / ``wrong_service_day`` /
``date_exception`` / ``in_past`` / ``busy``) and ``RequestedAvailability``
carries it up. But the sales answerer collapsed every ``STATUS_UNAVAILABLE``
into ``SLOT_BUSY_LINE``, so a customer asking for 23:00 (outside working
hours) was told the time was "занято" (busy) — a falsehood.

Live bug (багги, 31 May 2026, "Анна Иванова"): a 23:00 request was labelled
"занято" instead of "вне рабочих часов".
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from zoneinfo import ZoneInfo

import pytest

from services.api.app.answerers import AnswerContext
from services.api.app.calendar.calendar_client import BusyInterval, FreeBusy
from services.api.app.calendar.settings_repository import ServiceRule
from services.api.app.russian_text import get_russian_normalizer
from services.api.app.sales.sales_persona_answerer import (
    SLOT_BUSY_LINE,
    SLOT_CLOSED_DATE_LINE,
    SLOT_IN_PAST_LINE,
    SLOT_OFF_HOURS_LINE,
    SLOT_WRONG_DAY_LINE,
    STAGE_PITCHING,
    SalesPersonaAnswerer,
)

_NOW = datetime(2026, 5, 29, 9, 0, tzinfo=UTC)  # 12:00 Moscow, Fri 29 May
_MOSCOW = ZoneInfo("Europe/Moscow")
_CHAT_ID = 7
_PROJECT_ID = 1
_WEEK = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")


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
        self.queue: list[dict[str, Any]] = []

    def queue_response(self, payload: dict[str, Any]) -> None:
        self.queue.append(payload)

    async def complete_json(
        self, *, system: str, user: str, model: str | None = None, **_kw: Any
    ) -> dict[str, Any]:
        if not self.queue:
            raise AssertionError("LLM called without a queued payload")
        return self.queue.pop(0)


class _Settings:
    def __init__(self) -> None:
        self.calendar_operator = "@op"
        self.project_timezone = "Europe/Moscow"
        self.lookahead_days = 60


class _FakeCalSettings:
    def __init__(self, *, rules: list[ServiceRule] | None = None) -> None:
        self._settings = _Settings()
        self._rules = rules if rules is not None else [_rule()]

    def is_enabled(self, project_id: int) -> bool:
        return True

    def get(self, project_id: int):
        return self._settings

    def list_service_rules(self, project_id: int) -> list[ServiceRule]:
        return self._rules


def _rule(
    *,
    service_days: tuple[str, ...] = _WEEK,
    date_exceptions: list[str] | None = None,
    working_hours: dict[str, list[list[str]]] | None = None,
) -> ServiceRule:
    return ServiceRule(
        id=1,
        project_id=_PROJECT_ID,
        name="Багги",
        duration_minutes=60,
        working_hours=working_hours
        or {day: [["09:00", "20:00"]] for day in _WEEK},
        service_days=list(service_days),
        date_exceptions=date_exceptions or [],
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
        self.calls = 0

    async def query_busy(
        self, *, access_token, time_min, time_max, trace_id, calendar_id="primary"
    ) -> FreeBusy:
        self.calls += 1
        return FreeBusy(calendar_id="primary", busy=self._busy)


def _ctx() -> AnswerContext:
    return AnswerContext(
        chat_id=_CHAT_ID,
        customer_username="@anna",
        trace_id="trc",
        now=_NOW,
        project_id=_PROJECT_ID,
    )


def _build(
    *,
    cal_settings: _FakeCalSettings,
    freebusy: _FreeBusy,
    extracted_dates: str,
) -> tuple[SalesPersonaAnswerer, _FreeBusy]:
    openrouter = _FakeOpenRouter()
    openrouter.queue_response(
        {
            "extracted_fields": {"dates": extracted_dates},
            "next_question": "Сколько человек поедет?",
        }
    )
    answerer = SalesPersonaAnswerer(
        state_repo=_FakeStateRepo(initial=None),
        services_repo=_NoOpServicesRepo(),
        openrouter=openrouter,
        normalizer=get_russian_normalizer(),
        clock=lambda: _NOW,
        bot_persona_getter=lambda: "Анна",
        calendar_settings_repo=cal_settings,
        calendar_token_provider=_TokenProvider(),
        calendar_freebusy_client=freebusy,
        operator_chat_resolver=lambda op: 42,
    )
    return answerer, freebusy


@pytest.mark.asyncio
async def test_off_hours_greeting_says_off_hours_not_busy() -> None:
    """23:00 is outside the 09:00–20:00 window → "вне рабочих часов", not "занято"."""
    freebusy = _FreeBusy(busy=())  # nothing booked — the time is closed, not busy
    answerer, fb = _build(
        cal_settings=_FakeCalSettings(),
        freebusy=freebusy,
        extracted_dates="завтра в 23:00",
    )
    result = await answerer.try_answer(
        question="хочу забронировать багги завтра в 23:00", ctx=_ctx()
    )
    text = result.text or ""
    assert SLOT_OFF_HOURS_LINE in text
    assert SLOT_BUSY_LINE not in text
    # Still offers the nearest in-hours slot (09:00 next day).
    assert "Ближайшее свободное время" in text
    assert "09:00" in text
    assert result.metadata["stage_after"] == STAGE_PITCHING
    assert fb.calls == 1


@pytest.mark.asyncio
async def test_wrong_service_day_says_unavailable_not_busy() -> None:
    """Saturday is not a service day (mon–fri only) → "недоступна", not "занято"."""
    answerer, _ = _build(
        cal_settings=_FakeCalSettings(
            rules=[_rule(service_days=("mon", "tue", "wed", "thu", "fri"))]
        ),
        freebusy=_FreeBusy(busy=()),
        extracted_dates="завтра в 14:00",  # Sat 30 May
    )
    result = await answerer.try_answer(
        question="можно багги завтра в 14:00?", ctx=_ctx()
    )
    text = result.text or ""
    assert SLOT_WRONG_DAY_LINE in text
    assert SLOT_BUSY_LINE not in text


@pytest.mark.asyncio
async def test_closed_date_exception_says_closed_not_busy() -> None:
    """30 May is an explicit closed date → "не работаем", not "занято"."""
    answerer, _ = _build(
        cal_settings=_FakeCalSettings(
            rules=[_rule(date_exceptions=["2026-05-30"])]
        ),
        freebusy=_FreeBusy(busy=()),
        extracted_dates="завтра в 14:00",  # Sat 30 May — the closed date
    )
    result = await answerer.try_answer(
        question="можно багги завтра в 14:00?", ctx=_ctx()
    )
    text = result.text or ""
    assert SLOT_CLOSED_DATE_LINE in text
    assert SLOT_BUSY_LINE not in text


@pytest.mark.asyncio
async def test_past_time_says_past_not_busy() -> None:
    """10:00 today is already gone (now is 12:00) → "уже прошло", not "занято"."""
    answerer, _ = _build(
        cal_settings=_FakeCalSettings(),
        freebusy=_FreeBusy(busy=()),
        extracted_dates="сегодня в 10:00",  # 10:00 < 12:00 now
    )
    result = await answerer.try_answer(
        question="можно багги сегодня в 10:00?", ctx=_ctx()
    )
    text = result.text or ""
    assert SLOT_IN_PAST_LINE in text
    assert SLOT_BUSY_LINE not in text


@pytest.mark.asyncio
async def test_genuinely_busy_still_says_busy() -> None:
    """Regression: a real conflict at an in-hours time keeps "занято"."""
    busy = (
        BusyInterval(
            start=datetime(2026, 5, 30, 13, 0, tzinfo=_MOSCOW),
            end=datetime(2026, 5, 30, 15, 0, tzinfo=_MOSCOW),
        ),
    )
    answerer, _ = _build(
        cal_settings=_FakeCalSettings(),
        freebusy=_FreeBusy(busy=busy),
        extracted_dates="завтра в 14:00",  # 14:00 is in-hours but booked
    )
    result = await answerer.try_answer(
        question="можно багги завтра в 14:00?", ctx=_ctx()
    )
    text = result.text or ""
    assert SLOT_BUSY_LINE in text
    assert SLOT_OFF_HOURS_LINE not in text

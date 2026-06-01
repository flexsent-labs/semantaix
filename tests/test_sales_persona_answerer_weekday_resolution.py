"""Story 12.33 (D9) — weekday/relative dates resolve against the right day.

The scoping/greeting LLM had no current-date context, so it resolved a weekday
like "в понедельник" to a guessed (often free, future) absolute date and stored
that in ``intent.dates`` — the calendar then checked the wrong day and a busy
slot was accepted. The fix injects today's project-local date into both prompts
and instructs the LLM to store the customer's date phrase verbatim, so the
deterministic ``extract_requested_start`` resolver (which gets "today counts"
right) owns weekday→date.
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
from services.api.app.sales.intent import Intent
from services.api.app.sales.sales_persona_answerer import (
    SLOT_BUSY_LINE,
    STAGE_SCOPING,
    SalesPersonaAnswerer,
    _format_today_ru,
)

_NOW = datetime(2026, 6, 1, 9, 0, tzinfo=UTC)  # 12:00 Moscow, Monday 1 June
_MOSCOW = ZoneInfo("Europe/Moscow")
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

    def get_by_name(self, *, project_id: int, name: str) -> Any | None:  # pragma: no cover
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
    def __init__(self) -> None:
        self._settings = _Settings()

    def is_enabled(self, project_id: int) -> bool:
        return True

    def get(self, project_id: int):
        return self._settings

    def list_service_rules(self, project_id: int) -> list[ServiceRule]:
        week = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
        return [
            ServiceRule(
                id=1,
                project_id=_PROJECT_ID,
                name="Багги",
                duration_minutes=60,
                working_hours={day: [["09:00", "20:00"]] for day in week},
                service_days=week,
                date_exceptions=[],
                updated_at=None,
            )
        ]


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


def _ctx() -> AnswerContext:
    return AnswerContext(
        chat_id=_CHAT_ID,
        customer_username="@artur",
        trace_id="trc",
        now=_NOW,
        project_id=_PROJECT_ID,
    )


def _build(
    *,
    state: dict[str, Any] | None,
    openrouter: _FakeOpenRouter,
    cal_settings: Any | None = None,
    freebusy: _FreeBusy | None = None,
) -> SalesPersonaAnswerer:
    return SalesPersonaAnswerer(
        state_repo=_FakeStateRepo(initial=state),
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


def test_format_today_ru_monday() -> None:
    # 2026-06-01 09:00 UTC == 12:00 Europe/Moscow, a Monday.
    assert _format_today_ru(_NOW) == "1 июня 2026 года, понедельник"


@pytest.mark.asyncio
async def test_greeting_prompt_carries_today_and_verbatim_directive() -> None:
    openrouter = _FakeOpenRouter()
    openrouter.queue_response(
        {"extracted_fields": {}, "next_question": "Сколько человек поедет?"}
    )
    answerer = _build(state=None, openrouter=openrouter)  # no calendar → no intercept
    await answerer.try_answer(question="хочу багги", ctx=_ctx())

    system = openrouter.calls[0]["system"]
    assert "1 июня 2026 года, понедельник" in system
    # Instructed to store the phrase verbatim, not pre-resolve the weekday.
    assert "дослов" in system.lower()


@pytest.mark.asyncio
async def test_scoping_prompt_carries_today() -> None:
    openrouter = _FakeOpenRouter()
    openrouter.queue_response(
        {"extracted_fields": {"headcount": 2}, "next_question": "Сколько багги?"}
    )
    state = {
        "chat_id": _CHAT_ID,
        "project_id": _PROJECT_ID,
        "current_stage": STAGE_SCOPING,
        "collected_intent": Intent(dates="завтра в 14:00").to_dict(),
        "last_proposal": None,
    }
    answerer = _build(state=state, openrouter=openrouter)
    await answerer.try_answer(question="нас двое", ctx=_ctx())

    system = openrouter.calls[0]["system"]
    assert "1 июня 2026 года, понедельник" in system


@pytest.mark.asyncio
async def test_raw_weekday_today_intercepts_busy_slot() -> None:
    """End-to-end: the LLM (as now instructed) stores the raw weekday phrase;
    the deterministic resolver maps "в понедельник" on a Monday to today, so a
    busy slot is intercepted — the verdict "сегодня"/"1 июня" already give."""
    busy = (
        BusyInterval(
            start=datetime(2026, 6, 1, 13, 0, tzinfo=_MOSCOW),
            end=datetime(2026, 6, 1, 15, 0, tzinfo=_MOSCOW),
        ),
    )
    openrouter = _FakeOpenRouter()
    openrouter.queue_response(
        {
            "extracted_fields": {"dates": "в понедельник в 14:00"},
            "next_question": "Сколько человек поедет?",
        }
    )
    answerer = _build(
        state=None,
        openrouter=openrouter,
        cal_settings=_FakeCalSettings(),
        freebusy=_FreeBusy(busy=busy),
    )
    result = await answerer.try_answer(
        question="можно в понедельник в 14:00 на багги?", ctx=_ctx()
    )
    assert SLOT_BUSY_LINE in (result.text or "")

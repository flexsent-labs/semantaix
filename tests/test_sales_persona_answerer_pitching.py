"""Pitching / completion-stage tests for ``SalesPersonaAnswerer`` (Epic-12.10).

Once scoping is complete the answerer enters ``pitching`` and runs the
completion logic: check the customer's concrete requested time against the
calendar, then either confirm + hand off (free / can't-verify) or report the
slot busy and offer the nearest free alternative.
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
    MATERIAL_DISPATCH_FALLBACK_LINE,
    SCOPING_COMPLETE_HANDOFF_LINE,
    SLOT_BUSY_LINE,
    SLOT_FREE_HANDOFF_LINE,
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

    def list_for_project(self, *, project_id: int) -> list[Any]:
        return []

    def get_by_name(self, *, project_id: int, name: str) -> Any | None:
        return None


class _Settings:
    def __init__(
        self,
        *,
        operator: str | None = "@op",
        tz: str = "Europe/Moscow",
        lookahead: int = 60,
    ) -> None:
        self.calendar_operator = operator
        self.project_timezone = tz
        self.lookahead_days = lookahead


class _FakeCalSettings:
    def __init__(
        self,
        *,
        enabled: bool = True,
        settings: _Settings | None = None,
        rules: list[ServiceRule] | None = None,
    ) -> None:
        self._enabled = enabled
        self._settings = settings if settings is not None else _Settings()
        self._rules = rules if rules is not None else [_rule()]

    def is_enabled(self, project_id: int) -> bool:
        return self._enabled

    def get(self, project_id: int):
        return self._settings

    def list_service_rules(self, project_id: int) -> list[ServiceRule]:
        return self._rules


def _rule(*, working=True) -> ServiceRule:
    week = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
    return ServiceRule(
        id=1,
        project_id=_PROJECT_ID,
        name="Багги",
        duration_minutes=60,
        working_hours=(
            {day: [["09:00", "20:00"]] for day in week} if working else {}
        ),
        service_days=week if working else [],
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


def _ctx() -> AnswerContext:
    return AnswerContext(
        chat_id=_CHAT_ID,
        customer_username="@artur",
        trace_id="trc",
        now=_NOW,
        project_id=_PROJECT_ID,
    )


def _state(*, dates: str | None) -> dict[str, Any]:
    return {
        "chat_id": _CHAT_ID,
        "project_id": _PROJECT_ID,
        "current_stage": "pitching",
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
    cal_settings: _FakeCalSettings | None = None,
    token_provider: Any | None = None,
    freebusy: Any | None = None,
) -> tuple[SalesPersonaAnswerer, _FakeStateRepo]:
    state_repo = _FakeStateRepo(initial=state)
    answerer = SalesPersonaAnswerer(
        state_repo=state_repo,
        services_repo=_NoOpServicesRepo(),
        openrouter=object(),  # never called in the pitching path
        normalizer=get_russian_normalizer(),
        clock=lambda: _NOW,
        bot_persona_getter=lambda: "Анна",
        calendar_settings_repo=cal_settings,
        calendar_token_provider=token_provider,
        calendar_freebusy_client=freebusy,
        operator_chat_resolver=lambda op: 42,
    )
    return answerer, state_repo


@pytest.mark.asyncio
async def test_requested_time_free_confirms_and_hands_off() -> None:
    answerer, _ = _build(
        state=_state(dates="завтра в 14:00"),
        cal_settings=_FakeCalSettings(),
        token_provider=_TokenProvider(),
        freebusy=_FreeBusy(busy=()),
    )
    result = await answerer.try_answer(question="ну что?", ctx=_ctx())
    assert result.text == SLOT_FREE_HANDOFF_LINE
    assert result.response_mode == "sales_escalation"
    assert result.metadata["hitl_reason"] == "sales_scoping_complete"


@pytest.mark.asyncio
async def test_requested_time_busy_offers_alternative() -> None:
    busy = (
        BusyInterval(
            start=datetime(2026, 5, 30, 13, 0, tzinfo=ZoneInfo("Europe/Moscow")),
            end=datetime(2026, 5, 30, 15, 0, tzinfo=ZoneInfo("Europe/Moscow")),
        ),
    )
    answerer, _ = _build(
        state=_state(dates="завтра в 14:00"),
        cal_settings=_FakeCalSettings(),
        token_provider=_TokenProvider(),
        freebusy=_FreeBusy(busy=busy),
    )
    result = await answerer.try_answer(question="ну что?", ctx=_ctx())
    text = result.text or ""
    assert SLOT_BUSY_LINE in text
    assert "Ближайшее свободное время" in text
    assert "09:00" in text  # earliest free slot that day
    assert result.response_mode == "sales_escalation"
    assert (
        result.metadata["sales_turn_kind"] == "scoping_complete_busy_alternative"
    )


@pytest.mark.asyncio
async def test_requested_time_busy_no_alternative_hands_off() -> None:
    # A rule with no working hours → requested time unavailable AND no slot
    # anywhere in the window.
    answerer, _ = _build(
        state=_state(dates="завтра в 14:00"),
        cal_settings=_FakeCalSettings(rules=[_rule(working=False)]),
        token_provider=_TokenProvider(),
        freebusy=_FreeBusy(busy=()),
    )
    result = await answerer.try_answer(question="ну что?", ctx=_ctx())
    text = result.text or ""
    assert SLOT_BUSY_LINE in text
    assert SCOPING_COMPLETE_HANDOFF_LINE in text
    assert result.metadata["sales_turn_kind"] == "scoping_complete_busy_no_slot"


@pytest.mark.asyncio
async def test_calendar_disabled_plain_handoff() -> None:
    answerer, _ = _build(
        state=_state(dates="завтра в 14:00"),
        cal_settings=_FakeCalSettings(enabled=False),
    )
    result = await answerer.try_answer(question="ну что?", ctx=_ctx())
    assert result.text == SCOPING_COMPLETE_HANDOFF_LINE


@pytest.mark.asyncio
async def test_settings_missing_plain_handoff() -> None:
    cal = _FakeCalSettings()
    cal._settings = None  # enabled but no settings row
    answerer, _ = _build(state=_state(dates="завтра в 14:00"), cal_settings=cal)
    result = await answerer.try_answer(question="ну что?", ctx=_ctx())
    assert result.text == SCOPING_COMPLETE_HANDOFF_LINE


@pytest.mark.asyncio
async def test_no_dates_plain_handoff() -> None:
    answerer, _ = _build(
        state=_state(dates=None), cal_settings=_FakeCalSettings()
    )
    result = await answerer.try_answer(question="ну что?", ctx=_ctx())
    assert result.text == SCOPING_COMPLETE_HANDOFF_LINE


@pytest.mark.asyncio
async def test_dates_without_time_plain_handoff() -> None:
    # "1 мая" has a day but no clock time → not a concrete requested start.
    answerer, _ = _build(
        state=_state(dates="скоро"), cal_settings=_FakeCalSettings()
    )
    result = await answerer.try_answer(question="ну что?", ctx=_ctx())
    assert result.text == SCOPING_COMPLETE_HANDOFF_LINE


@pytest.mark.asyncio
async def test_busy_alternative_appends_media_fallback_line() -> None:
    # When the tour-preview media dispatch failed earlier in the same turn,
    # the busy/alternative reply still appends the textual media fallback.
    answerer, _ = _build(
        state=_state(dates="завтра в 14:00"), cal_settings=_FakeCalSettings()
    )
    result = await answerer._propose_alternative_or_handoff(
        ctx=_ctx(),
        intent=Intent(dates="завтра в 14:00"),
        stage_before="pitching",
        alternative=datetime(2026, 5, 30, 9, 0, tzinfo=ZoneInfo("Europe/Moscow")),
        base_metadata={},
        dispatch_fallback=True,
    )
    text = result.text or ""
    assert SLOT_BUSY_LINE in text
    assert "09:00" in text
    assert MATERIAL_DISPATCH_FALLBACK_LINE in text


@pytest.mark.asyncio
async def test_multiple_services_cannot_pin_plain_handoff() -> None:
    two = _FakeCalSettings(rules=[_rule(), _rule()])
    answerer, _ = _build(
        state=_state(dates="завтра в 14:00"),
        cal_settings=two,
        token_provider=_TokenProvider(),
        freebusy=_FreeBusy(),
    )
    result = await answerer.try_answer(question="ну что?", ctx=_ctx())
    assert result.text == SCOPING_COMPLETE_HANDOFF_LINE

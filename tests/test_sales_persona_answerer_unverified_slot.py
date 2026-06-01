"""Story 12.32 (D1) — never silently accept a slot the calendar couldn't verify.

When a concrete requested time is given and scoping completes, but the calendar
free/busy check could not run (token not connected / reconnect needed / provider
error), the bot must NOT reply as if the time were checked. It hands off with a
distinct "I'll check this time and come back" line and flags the HITL ticket
``calendar_verified=False`` so the operator knows the calendar was not consulted.

The early gate (`_maybe_intercept_busy_slot`) keeps its silent fall-through on
NOT_CONNECTED/ERROR — escalation happens at completion, the single chokepoint —
so the Story 12.25 early-gate tests are unaffected.
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
    HITL_REASON_CALENDAR_UNVERIFIED,
    MATERIAL_DISPATCH_FALLBACK_LINE,
    RESPONSE_MODE_SALES_ESCALATION,
    SCOPING_COMPLETE_HANDOFF_LINE,
    SLOT_BUSY_LINE,
    SLOT_FREE_HANDOFF_LINE,
    SLOT_UNVERIFIED_HANDOFF_LINE,
    STAGE_AWAITING_TIME,
    STAGE_PITCHING,
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
    def __init__(self, *, enabled: bool = True) -> None:
        self._enabled = enabled
        self._settings = _Settings()

    def is_enabled(self, project_id: int) -> bool:
        return self._enabled

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


class _RaisingTokenProvider:
    async def get_access_token(
        self, project_id, operator, *, operator_chat_id, trace_id
    ) -> str:
        raise CalendarReconnectNeeded("reconnect-please")


class _FreeBusy:
    def __init__(self, *, busy: tuple[BusyInterval, ...] = ()) -> None:
        self._busy = busy

    async def query_busy(
        self, *, access_token, time_min, time_max, trace_id, calendar_id="primary"
    ) -> FreeBusy:
        return FreeBusy(calendar_id="primary", busy=self._busy)


class _QueueOpenRouter:
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


def _complete_state() -> dict[str, Any]:
    """Awaiting-time state with every OTHER scoping field already collected, so a
    date+time reply completes scoping and reaches ``_complete_booking``."""
    return {
        "chat_id": _CHAT_ID,
        "project_id": _PROJECT_ID,
        "current_stage": STAGE_AWAITING_TIME,
        "collected_intent": Intent(
            dates=None,
            headcount=2,
            vehicle_count=1,
            difficulty="новичок",
            drivers="сами",
        ).to_dict(),
        "last_proposal": None,
    }


def _build(
    *,
    token_provider: Any,
    operator_chat_resolver,
    freebusy: _FreeBusy | None = None,
) -> SalesPersonaAnswerer:
    return SalesPersonaAnswerer(
        state_repo=_FakeStateRepo(initial=_complete_state()),
        services_repo=_NoOpServicesRepo(),
        openrouter=_QueueOpenRouter(
            {"extracted_fields": {"dates": "завтра в 14:00"}, "next_question": "ок"}
        ),
        normalizer=get_russian_normalizer(),
        clock=lambda: _NOW,
        bot_persona_getter=lambda: "Анна",
        calendar_settings_repo=_FakeCalSettings(),
        calendar_token_provider=token_provider,
        calendar_freebusy_client=freebusy if freebusy is not None else _FreeBusy(),
        operator_chat_resolver=operator_chat_resolver,
    )


@pytest.mark.asyncio
async def test_completion_not_connected_hands_off_unverified() -> None:
    """operator_chat_id is None → STATUS_NOT_CONNECTED → unverified handoff."""
    answerer = _build(
        token_provider=_TokenProvider(),
        operator_chat_resolver=lambda op: None,  # → NOT_CONNECTED
        freebusy=_FreeBusy(busy=()),  # would be FREE if it could check — it can't
    )
    result = await answerer.try_answer(question="завтра в 14:00", ctx=_ctx())

    assert result.text == SLOT_UNVERIFIED_HANDOFF_LINE
    # Must NOT imply the time was checked/secured.
    assert result.text != SLOT_FREE_HANDOFF_LINE
    assert result.text != SCOPING_COMPLETE_HANDOFF_LINE
    # Escalates with the UNVERIFIED flag so a human confirms the exact time.
    assert result.response_mode == RESPONSE_MODE_SALES_ESCALATION
    assert result.metadata["escalate"] is True
    assert result.metadata["hitl_reason"] == HITL_REASON_CALENDAR_UNVERIFIED
    assert result.metadata["calendar_verified"] is False
    assert result.metadata["calendar_unverified_reason"] == "not_connected"
    assert result.metadata["stage_after"] == STAGE_PITCHING
    # Operator-visible context warns the calendar wasn't checked.
    assert "не проверен" in result.metadata["escalation_context"]


@pytest.mark.asyncio
async def test_completion_error_reconnect_hands_off_unverified() -> None:
    """Token raises CalendarReconnectNeeded → STATUS_ERROR → unverified handoff,
    reason marker ``error:reconnect_needed``."""
    answerer = _build(
        token_provider=_RaisingTokenProvider(),
        operator_chat_resolver=lambda op: 42,
        freebusy=_FreeBusy(busy=()),
    )
    result = await answerer.try_answer(question="завтра в 14:00", ctx=_ctx())

    assert result.text == SLOT_UNVERIFIED_HANDOFF_LINE
    assert result.metadata["escalate"] is True
    assert result.metadata["hitl_reason"] == HITL_REASON_CALENDAR_UNVERIFIED
    assert result.metadata["calendar_verified"] is False
    assert result.metadata["calendar_unverified_reason"] == "error:reconnect_needed"


@pytest.mark.asyncio
async def test_completion_free_slot_unchanged() -> None:
    """Regression (AC5): a reachable calendar with a free slot still confirms."""
    answerer = _build(
        token_provider=_TokenProvider(),
        operator_chat_resolver=lambda op: 42,
        freebusy=_FreeBusy(busy=()),
    )
    result = await answerer.try_answer(question="завтра в 14:00", ctx=_ctx())
    assert result.text == SLOT_FREE_HANDOFF_LINE
    assert result.metadata.get("calendar_verified") is not False


@pytest.mark.asyncio
async def test_completion_busy_slot_unchanged() -> None:
    """Regression (AC5): a reachable calendar with a busy slot still says занято
    + offers an alternative — the unverified branch must not catch UNAVAILABLE."""
    busy = (
        BusyInterval(
            start=datetime(2026, 5, 30, 13, 0, tzinfo=ZoneInfo("Europe/Moscow")),
            end=datetime(2026, 5, 30, 15, 0, tzinfo=ZoneInfo("Europe/Moscow")),
        ),
    )
    answerer = _build(
        token_provider=_TokenProvider(),
        operator_chat_resolver=lambda op: 42,
        freebusy=_FreeBusy(busy=busy),
    )
    result = await answerer.try_answer(question="завтра в 14:00", ctx=_ctx())
    text = result.text or ""
    assert SLOT_BUSY_LINE in text
    assert SLOT_UNVERIFIED_HANDOFF_LINE not in text


@pytest.mark.asyncio
async def test_unverified_handoff_appends_dispatch_fallback() -> None:
    """When a tour-preview media dispatch failed earlier in the turn, the
    unverified handoff still surfaces the media fallback line."""
    answerer = _build(
        token_provider=_TokenProvider(),
        operator_chat_resolver=lambda op: 42,
    )
    result = await answerer._handoff_unverified_slot(
        ctx=_ctx(),
        intent=Intent(dates="завтра в 14:00", headcount=2, vehicle_count=1),
        stage_before="scoping",
        status="not_connected",
        reason=None,
        base_metadata={},
        dispatch_fallback=True,
    )
    text = result.text or ""
    assert SLOT_UNVERIFIED_HANDOFF_LINE in text
    assert MATERIAL_DISPATCH_FALLBACK_LINE in text
    assert result.metadata["calendar_unverified_reason"] == "not_connected"

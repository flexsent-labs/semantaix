"""Story 12.22 — defer HITL escalation until the customer accepts the
offered alternative slot.

Live bug (Артур Яскевич, багги, 31 May 2026):

    Customer: хочу забронировать багги завтра в 14:00
    Bot:      Проверяю, минуточку… 🙂
    Bot:      К сожалению, это время уже занято. Ближайшее свободное время — 1 июня, 08:00.
    Bot:      Спасибо! Передам детали коллегам — подтвердят и вернутся с предложением.   ← BUG

The first-turn busy-with-alternative branch of ``_propose_alternative_or_handoff``
was returning ``response_mode='sales_escalation'`` together with the offer,
which created a HITL ticket BEFORE the customer had a chance to accept,
reject, or counter-offer the proposed slot. The acceptance second turn already
escalates via ``_confirm_slot`` (Story 12.19); the closure / non-acceptance
second turn already escalates via ``_handoff_after_pitching_followup``.

Contract after the fix:

* ``alternative is not None`` → ``handled=True`` but **no escalation**;
  the offer text is sent, ``last_proposal`` is persisted on PITCHING, and
  the next-turn handling decides whether to escalate. Recursive busy turns
  (the customer counter-offered another busy date) stay non-escalating.
* ``alternative is None`` → unchanged: still escalates (sales_scoping_complete);
  the customer has nothing to accept, so a human picks up.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
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
    PITCHING_ACCEPT_CONFIRM_LINE,
    SCOPING_COMPLETE_HANDOFF_LINE,
    SLOT_BUSY_LINE,
    STAGE_CLOSING,
    STAGE_PITCHING,
    SalesPersonaAnswerer,
)

_NOW = datetime(2026, 5, 29, 9, 0, tzinfo=UTC)  # 12:00 Moscow, Fri 29 May
_CHAT_ID = 7
_PROJECT_ID = 1
_MOSCOW = ZoneInfo("Europe/Moscow")


class _FakeStateRepo:
    """State repo whose ``get`` mirrors the last ``upsert`` — supports two-turn flows."""

    def __init__(self, *, initial: dict[str, Any]) -> None:
        self._state = dict(initial)
        self.upserts: list[dict[str, Any]] = []

    def get(self, chat_id: int) -> dict[str, Any] | None:
        return dict(self._state)

    def upsert(self, **kwargs: Any) -> None:
        self.upserts.append(kwargs)
        # Mirror the most recent persisted shape so a subsequent ``get`` sees
        # the new stage / intent / last_proposal — required for two-turn tests
        # to behave like the real repo.
        new_state = dict(self._state)
        new_state["current_stage"] = kwargs.get(
            "current_stage", new_state.get("current_stage")
        )
        if "intent" in kwargs:
            new_state["collected_intent"] = kwargs["intent"].to_dict()
        if "last_proposal" in kwargs:
            new_state["last_proposal"] = kwargs["last_proposal"]
        self._state = new_state


class _NoOpServicesRepo:
    def count_active(self, *, project_id: int) -> int:  # pragma: no cover
        return 0

    def list_for_project(self, *, project_id: int) -> list[Any]:
        return []

    def get_by_name(self, *, project_id: int, name: str) -> Any | None:
        return None


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


def _rule(*, working: bool = True) -> ServiceRule:
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


def _pitching_state(
    *, dates: str | None, last_proposal: dict[str, Any] | None
) -> dict[str, Any]:
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
        "last_proposal": last_proposal,
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
        openrouter=object(),  # never called on the pitching path
        normalizer=get_russian_normalizer(),
        clock=lambda: _NOW,
        bot_persona_getter=lambda: "Анна",
        calendar_settings_repo=cal_settings,
        calendar_token_provider=token_provider,
        calendar_freebusy_client=freebusy,
        operator_chat_resolver=lambda op: 42,
    )
    return answerer, state_repo


def _busy_intervals(
    day_offsets: list[int], *, span_hours: int = 2
) -> tuple[BusyInterval, ...]:
    """Block ``span_hours`` from 13:00 Moscow on each day offset from 30 May 2026.

    Offset 0 → 30 May; offset 2 → 1 June (crosses month boundary).
    """
    base = datetime(2026, 5, 30, 13, 0, tzinfo=_MOSCOW)
    return tuple(
        BusyInterval(
            start=base + timedelta(days=offset),
            end=base + timedelta(days=offset, hours=span_hours),
        )
        for offset in day_offsets
    )


# --- AC 1: alternative offered → no escalation -------------------------------


@pytest.mark.asyncio
async def test_busy_with_alternative_returns_handled_without_escalation() -> None:
    """Direct call: busy + alternative → handled, but NO escalation metadata."""
    answerer, state_repo = _build(
        state=_pitching_state(dates="завтра в 14:00", last_proposal=None),
        cal_settings=_FakeCalSettings(),
    )
    alternative = datetime(2026, 5, 30, 9, 0, tzinfo=_MOSCOW)
    result = await answerer._propose_alternative_or_handoff(
        ctx=_ctx(),
        intent=Intent(dates="завтра в 14:00", headcount=2, vehicle_count=1),
        stage_before=STAGE_PITCHING,
        alternative=alternative,
        base_metadata={},
        dispatch_fallback=False,
    )
    text = result.text or ""
    assert SLOT_BUSY_LINE in text
    assert "Ближайшее свободное время" in text
    assert "09:00" in text
    assert SCOPING_COMPLETE_HANDOFF_LINE not in text
    # The point of Story 12.22: no escalation while the customer is still
    # deciding whether to accept the offered alternative.
    assert result.handled is True
    assert result.response_mode is None
    assert result.metadata.get("escalate") is not True
    assert "hitl_reason" not in result.metadata
    assert "escalation_context" not in result.metadata
    # Stage + remembered slot still persisted so _handle_pitching can interpret
    # the next turn (Story 12.19 contract).
    assert state_repo.upserts[-1]["current_stage"] == STAGE_PITCHING
    assert state_repo.upserts[-1]["last_proposal"] == {
        "alternative_iso": alternative.isoformat()
    }


# --- AC 1 negative: no alternative → still escalates -------------------------


@pytest.mark.asyncio
async def test_busy_no_alternative_still_escalates() -> None:
    """``alternative=None`` keeps the escalation contract — nothing to accept."""
    answerer, _ = _build(
        state=_pitching_state(dates="завтра в 14:00", last_proposal=None),
        cal_settings=_FakeCalSettings(rules=[_rule(working=False)]),
    )
    result = await answerer._propose_alternative_or_handoff(
        ctx=_ctx(),
        intent=Intent(dates="завтра в 14:00", headcount=2, vehicle_count=1),
        stage_before=STAGE_PITCHING,
        alternative=None,
        base_metadata={},
        dispatch_fallback=False,
    )
    text = result.text or ""
    assert SLOT_BUSY_LINE in text
    assert SCOPING_COMPLETE_HANDOFF_LINE in text
    assert result.response_mode == "sales_escalation"
    assert result.metadata["escalate"] is True
    assert result.metadata["hitl_reason"] == "sales_scoping_complete"
    assert result.metadata["escalation_context"]
    assert result.metadata["sales_turn_kind"] == "scoping_complete_busy_no_slot"


# --- AC 2: accept on turn N+1 → exactly one escalation across both turns -----


@pytest.mark.asyncio
async def test_alternative_offered_then_accepted_escalates_once() -> None:
    """Two-turn flow: offer alt → customer says "да" → single escalation."""
    busy = _busy_intervals([0])  # block 30 May 13:00–15:00 Moscow
    answerer, state_repo = _build(
        state=_pitching_state(dates=None, last_proposal=None),
        cal_settings=_FakeCalSettings(),
        token_provider=_TokenProvider(),
        freebusy=_FreeBusy(busy=busy),
    )

    # Turn N — customer's counter-offer hits a busy slot; bot offers nearest.
    turn1 = await answerer.try_answer(question="завтра в 14:00", ctx=_ctx())
    assert turn1.handled is True
    assert turn1.response_mode is None
    assert turn1.metadata.get("escalate") is not True
    assert state_repo.upserts[-1]["last_proposal"] == {
        "alternative_iso": "2026-05-30T09:00:00+03:00"
    }

    # Turn N+1 — customer accepts the offered slot; this is the moment to escalate.
    turn2 = await answerer.try_answer(question="да", ctx=_ctx())
    assert turn2.text == PITCHING_ACCEPT_CONFIRM_LINE.format(
        day_month="30 мая", time="09:00"
    )
    assert turn2.response_mode == "sales_escalation"
    assert turn2.metadata["escalate"] is True
    assert turn2.metadata["hitl_reason"] == "sales_scoping_complete"
    assert turn2.metadata["stage_after"] == STAGE_CLOSING

    # Exactly one of the two turns carried an escalation signal.
    escalations = [t for t in (turn1, turn2) if t.response_mode == "sales_escalation"]
    assert len(escalations) == 1


# --- AC 3: recursive busy → no ticket spam ----------------------------------


@pytest.mark.asyncio
async def test_alternative_then_counter_offer_busy_again_no_escalation() -> None:
    """Counter-offered time also busy → fresh alt, still no escalation."""
    # Block 13:00–18:00 on "tomorrow" (30 May) so both 14:00 and 16:00 are busy.
    busy = _busy_intervals([0], span_hours=5)
    answerer, state_repo = _build(
        state=_pitching_state(dates=None, last_proposal=None),
        cal_settings=_FakeCalSettings(),
        token_provider=_TokenProvider(),
        freebusy=_FreeBusy(busy=busy),
    )

    turn1 = await answerer.try_answer(question="завтра в 14:00", ctx=_ctx())
    assert turn1.response_mode is None
    first_proposal = state_repo.upserts[-1]["last_proposal"]
    assert first_proposal is not None

    # Customer counter-offers a different time on the same day; still busy.
    turn2 = await answerer.try_answer(question="завтра в 16:00", ctx=_ctx())
    assert turn2.handled is True
    assert turn2.response_mode is None
    assert turn2.metadata.get("escalate") is not True
    text = turn2.text or ""
    assert SLOT_BUSY_LINE in text
    assert "Ближайшее свободное время" in text
    # A fresh offered slot was recorded — last_proposal updated, still no ticket.
    second_proposal = state_repo.upserts[-1]["last_proposal"]
    assert second_proposal is not None

    # Neither turn escalated.
    assert all(
        t.response_mode != "sales_escalation" for t in (turn1, turn2)
    )


# --- AC 4: closure / non-acceptance escalates via the handoff sink ----------


@pytest.mark.asyncio
async def test_alternative_offered_then_closure_escalates_via_handoff() -> None:
    """Customer says "всё, спасибо" after the offer → handoff + escalation."""
    busy = _busy_intervals([0])
    answerer, state_repo = _build(
        state=_pitching_state(dates=None, last_proposal=None),
        cal_settings=_FakeCalSettings(),
        token_provider=_TokenProvider(),
        freebusy=_FreeBusy(busy=busy),
    )

    turn1 = await answerer.try_answer(question="завтра в 14:00", ctx=_ctx())
    assert turn1.response_mode is None

    turn2 = await answerer.try_answer(question="всё, спасибо", ctx=_ctx())
    assert turn2.text == SCOPING_COMPLETE_HANDOFF_LINE
    assert turn2.response_mode == "sales_escalation"
    assert turn2.metadata["sales_turn_kind"] == "pitching_followup"
    assert turn2.metadata["sales_closure_detected"] is True
    assert turn2.metadata["escalate"] is True
    assert state_repo.upserts[-1]["current_stage"] == STAGE_CLOSING


# --- AC 6: dispatch fallback still composes textually, no escalation --------


@pytest.mark.asyncio
async def test_dispatch_fallback_on_busy_alternative_still_no_escalation() -> None:
    """A failed tour-preview dispatch appends the fallback line — still no ticket."""
    answerer, _ = _build(
        state=_pitching_state(dates="завтра в 14:00", last_proposal=None),
        cal_settings=_FakeCalSettings(),
    )
    alternative = datetime(2026, 5, 30, 9, 0, tzinfo=_MOSCOW)
    result = await answerer._propose_alternative_or_handoff(
        ctx=_ctx(),
        intent=Intent(dates="завтра в 14:00", headcount=2, vehicle_count=1),
        stage_before=STAGE_PITCHING,
        alternative=alternative,
        base_metadata={},
        dispatch_fallback=True,
    )
    text = result.text or ""
    assert SLOT_BUSY_LINE in text
    assert "09:00" in text
    assert MATERIAL_DISPATCH_FALLBACK_LINE in text
    assert result.response_mode is None
    assert result.metadata.get("escalate") is not True
    assert "hitl_reason" not in result.metadata

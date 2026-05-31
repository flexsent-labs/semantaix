"""Story 12.19 — recognise a slot confirmation in ``pitching`` and stop the
re-ask loop.

Live bug (багги, 30 May 2026): the bot offered a busy-slot alternative and
parked in ``pitching``; the customer's confirmation ("давайте на 31-ое в 8")
re-ran completion against the *stale* busy time and the bot repeated the
identical "time busy" line every turn. These tests pin the new behaviour:

* an acceptance of the offered slot confirms it (naming it) and closes;
* a parseable counter-offer re-runs completion (here: asks for the time);
* closure / any other reply hands off WITHOUT repeating the busy line;
* none of these ever re-emit ``SLOT_BUSY_LINE``.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest

from services.api.app.answerers import AnswerContext
from services.api.app.calendar.calendar_client import BusyInterval, FreeBusy
from services.api.app.calendar.settings_repository import ServiceRule
from services.api.app.russian_text import get_russian_normalizer
from services.api.app.sales.intent import Intent
from services.api.app.sales.sales_persona_answerer import (
    ASK_FOR_TIME_LINE,
    PITCHING_ACCEPT_CONFIRM_LINE,
    SCOPING_COMPLETE_HANDOFF_LINE,
    SLOT_BUSY_LINE,
    STAGE_AWAITING_TIME,
    STAGE_CLOSING,
    SalesPersonaAnswerer,
)

_NOW = datetime(2026, 5, 29, 9, 0, tzinfo=UTC)  # 12:00 Moscow, Fri 29 May
_CHAT_ID = 7
_PROJECT_ID = 1
_OFFERED_ISO = "2026-05-31T08:00:00+03:00"  # the slot the bot offered


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


def _ctx() -> AnswerContext:
    return AnswerContext(
        chat_id=_CHAT_ID,
        customer_username="@artur",
        trace_id="trc",
        now=_NOW,
        project_id=_PROJECT_ID,
    )


def _state(
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
        calendar_token_provider=_TokenProvider(),
        calendar_freebusy_client=_FreeBusy(),
        operator_chat_resolver=lambda op: 42,
    )
    return answerer, state_repo


# --- acceptance of the offered slot -----------------------------------------


@pytest.mark.asyncio
async def test_shorthand_confirm_reverifies_and_confirms_named() -> None:
    # The reported case. With Story 12.20 parsing, "давайте на 31-ое в 8" is
    # parsed (ordinal + bare hour) and re-verified; 31 May 08:00 is free → it is
    # confirmed by name and closes (same user-visible outcome as 12.19).
    week = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
    early_rule = ServiceRule(
        id=1,
        project_id=_PROJECT_ID,
        name="Багги",
        duration_minutes=60,
        working_hours={day: [["08:00", "20:00"]] for day in week},
        service_days=week,
        date_exceptions=[],
        updated_at=None,
    )
    answerer, state_repo = _build(
        state=_state(
            dates="завтра в 14:00", last_proposal={"alternative_iso": _OFFERED_ISO}
        ),
        cal_settings=_FakeCalSettings(rules=[early_rule]),
    )
    result = await answerer.try_answer(question="давайте на 31-ое в 8", ctx=_ctx())
    text = result.text or ""
    assert text == PITCHING_ACCEPT_CONFIRM_LINE.format(day_month="31 мая", time="08:00")
    assert SLOT_BUSY_LINE not in text
    assert "Ближайшее свободное время" not in text
    assert result.response_mode == "sales_escalation"
    assert result.metadata["stage_before"] == "pitching"
    assert result.metadata["stage_after"] == STAGE_CLOSING
    assert result.metadata["sales_turn_kind"] == "pitching_accept_confirmed"
    assert result.metadata["escalate"] is True
    assert result.metadata["hitl_reason"] == "sales_scoping_complete"
    assert "бронь" in result.metadata["escalation_context"]
    assert state_repo.upserts[-1]["current_stage"] == STAGE_CLOSING
    assert state_repo.upserts[-1]["last_proposal"] is None


@pytest.mark.asyncio
async def test_bare_da_confirms_offered_slot_and_closes() -> None:
    answerer, _ = _build(
        state=_state(dates="завтра в 14:00", last_proposal={"alternative_iso": _OFFERED_ISO}),
    )
    result = await answerer.try_answer(question="да", ctx=_ctx())
    assert result.text == PITCHING_ACCEPT_CONFIRM_LINE.format(
        day_month="31 мая", time="08:00"
    )
    assert result.metadata["stage_after"] == STAGE_CLOSING
    assert result.metadata["sales_turn_kind"] == "pitching_accept_confirmed"


@pytest.mark.asyncio
async def test_accept_with_unparseable_time_confirms_offered_slot() -> None:
    # "давайте в 8" — accept word + a time we cannot parse → confirm the OFFERED
    # slot and ship the verbatim text to the operator; never guess a new time.
    answerer, _ = _build(
        state=_state(dates="завтра в 14:00", last_proposal={"alternative_iso": _OFFERED_ISO}),
    )
    result = await answerer.try_answer(question="давайте в 8", ctx=_ctx())
    assert result.text == PITCHING_ACCEPT_CONFIRM_LINE.format(
        day_month="31 мая", time="08:00"
    )
    assert result.metadata["stage_after"] == STAGE_CLOSING
    assert SLOT_BUSY_LINE not in (result.text or "")


# --- counter-offer wins over acceptance -------------------------------------


@pytest.mark.asyncio
async def test_counter_offer_with_accept_word_reroutes_to_time_ask() -> None:
    # "давайте на 1 июня" is BOTH an acceptance lemma AND a parseable date.
    # The counter-offer must win: re-run completion (no clock → ask for time),
    # NOT confirm the originally-offered slot.
    answerer, state_repo = _build(
        state=_state(dates="завтра в 14:00", last_proposal={"alternative_iso": _OFFERED_ISO}),
        cal_settings=_FakeCalSettings(),
    )
    result = await answerer.try_answer(question="давайте на 1 июня", ctx=_ctx())
    assert result.text == ASK_FOR_TIME_LINE
    assert PITCHING_ACCEPT_CONFIRM_LINE.format(
        day_month="31 мая", time="08:00"
    ) != result.text
    assert result.metadata.get("escalate") is not True
    assert state_repo.upserts[-1]["current_stage"] == STAGE_AWAITING_TIME


# --- closure / no-op never repeat the busy line -----------------------------


@pytest.mark.asyncio
async def test_closure_hands_off_without_repeating_busy_line() -> None:
    answerer, state_repo = _build(
        state=_state(dates="завтра в 14:00", last_proposal={"alternative_iso": _OFFERED_ISO}),
    )
    result = await answerer.try_answer(question="всё, спасибо", ctx=_ctx())
    text = result.text or ""
    assert text == SCOPING_COMPLETE_HANDOFF_LINE
    assert SLOT_BUSY_LINE not in text
    assert "Ближайшее свободное время" not in text
    assert result.metadata["sales_turn_kind"] == "pitching_followup"
    assert result.metadata["sales_closure_detected"] is True
    assert result.metadata["escalate"] is True
    assert result.metadata["hitl_reason"] == "sales_scoping_complete"
    assert state_repo.upserts[-1]["current_stage"] == STAGE_CLOSING


@pytest.mark.asyncio
async def test_filler_non_acceptance_hands_off_no_loop() -> None:
    # The original infinite-loop trigger: a non-accept, non-closure, non-date
    # filler. Must hand off (no busy-line repeat), not re-check the stale slot.
    answerer, _ = _build(
        state=_state(dates="завтра в 14:00", last_proposal={"alternative_iso": _OFFERED_ISO}),
    )
    result = await answerer.try_answer(question="ну и как там?", ctx=_ctx())
    assert result.text == SCOPING_COMPLETE_HANDOFF_LINE
    assert SLOT_BUSY_LINE not in (result.text or "")
    assert result.metadata["sales_turn_kind"] == "pitching_followup"
    assert result.metadata["sales_closure_detected"] is False
    assert result.metadata["stage_after"] == STAGE_CLOSING


# --- pitching reached via the free / handoff path (no offered slot) ---------


@pytest.mark.asyncio
async def test_free_handoff_resume_then_da_does_not_falsely_confirm() -> None:
    # PITCHING reached via _handoff_after_scoping → last_proposal is None.
    # A bare "да" must NOT confirm a (non-existent) slot — it hands off.
    answerer, _ = _build(state=_state(dates="завтра в 14:00", last_proposal=None))
    result = await answerer.try_answer(question="да", ctx=_ctx())
    assert result.text == SCOPING_COMPLETE_HANDOFF_LINE
    assert result.metadata["sales_turn_kind"] == "pitching_followup"
    assert result.metadata["stage_after"] == STAGE_CLOSING


# --- the "accepted but nothing to name" branch ------------------------------


# --- Story 12.20 — autonomous re-verify of a restated (ordinal) time --------


@pytest.mark.asyncio
async def test_ordinal_counter_offer_reverifies_free_slot_and_confirms_named() -> None:
    # "давайте на 10-ое в 14:00" parses (ordinal + clock), re-verifies free
    # against the calendar, and confirms it BY NAME (not the offered slot).
    answerer, state_repo = _build(
        state=_state(
            dates="завтра в 14:00", last_proposal={"alternative_iso": _OFFERED_ISO}
        ),
        cal_settings=_FakeCalSettings(),
    )
    result = await answerer.try_answer(
        question="давайте на 10-ое в 14:00", ctx=_ctx()
    )
    assert result.text == PITCHING_ACCEPT_CONFIRM_LINE.format(
        day_month="10 июня", time="14:00"
    )
    assert result.metadata["stage_after"] == STAGE_CLOSING
    assert result.metadata["sales_turn_kind"] == "pitching_accept_confirmed"


@pytest.mark.asyncio
async def test_requested_start_for_without_calendar_is_none() -> None:
    # Defensive guard: no calendar wired → cannot anchor a concrete start.
    answerer, _ = _build(state=_state(dates="завтра в 14:00", last_proposal=None))
    result = await answerer._requested_start_for(
        ctx=_ctx(), intent=Intent(dates="завтра в 14:00")
    )
    assert result is None


@pytest.mark.asyncio
async def test_confirm_slot_without_datetime_uses_generic_handoff() -> None:
    answerer, _ = _build(state=_state(dates="завтра в 14:00", last_proposal=None))
    result = await answerer._confirm_slot(
        ctx=_ctx(),
        intent=Intent(dates="завтра в 14:00", headcount=2, vehicle_count=1),
        slot_dt=None,
    )
    assert result.text == SCOPING_COMPLETE_HANDOFF_LINE
    assert result.metadata["sales_turn_kind"] == "pitching_accept_no_slot"
    assert result.metadata["stage_after"] == STAGE_CLOSING

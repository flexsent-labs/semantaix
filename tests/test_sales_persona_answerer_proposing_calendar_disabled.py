"""``NoProposal(calendar_not_enabled)`` test for ``proposing`` (Story 12.07)."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest

from services.api.app.answerers import AnswerContext
from services.api.app.russian_text import get_russian_normalizer
from services.api.app.sales.date_proposer import (
    NO_PROPOSAL_AMBIGUOUS_SERVICE,
    NO_PROPOSAL_CALENDAR_NOT_ENABLED,
    NO_PROPOSAL_NO_DATE_HINT,
    NO_PROPOSAL_NO_SLOTS_IN_WINDOW,
    NO_PROPOSAL_PROVIDER_ERROR,
    NoProposal,
)
from services.api.app.sales.intent import Intent
from services.api.app.sales.sales_persona_answerer import (
    HITL_REASON_CALENDAR_DISABLED,
    HITL_REASON_PROPOSAL_FAILED,
    PROPOSAL_AMBIGUOUS_SERVICE_CLARIFIER,
    PROPOSAL_FALLBACK_CALENDAR_DISABLED,
    PROPOSAL_FALLBACK_UNAVAILABLE,
    RESPONSE_MODE_SALES_ESCALATION,
    STAGE_PROPOSING,
    SalesPersonaAnswerer,
)


class _FakeStateRepo:
    def __init__(self) -> None:
        self.rows: dict[int, dict[str, Any]] = {}
        self.upsert_calls: list[dict[str, Any]] = []

    def get(self, chat_id: int):
        return self.rows.get(chat_id)

    def upsert(self, **kwargs: Any) -> None:
        self.upsert_calls.append(kwargs)
        chat_id = int(kwargs["chat_id"])
        self.rows[chat_id] = dict(kwargs)


class _FakeServicesRepo:
    def count_active(self, *, project_id: int) -> int:  # pragma: no cover
        return 1

    def list_for_project(self, *, project_id: int) -> list:  # pragma: no cover
        return []


class _NeverCalledOpenRouter:
    async def complete_json(
        self, *, system: str, user: str, model: str | None = None
    ) -> dict[str, Any]:
        raise AssertionError(
            "openrouter must not be called on calendar-disabled path"
        )


class _StubDateProposer:
    def __init__(self, result) -> None:
        self.result = result

    async def propose(self, *, project_id: int, intent: Intent, now: datetime):
        return self.result


_NOW = datetime(2026, 4, 25, 9, 0, tzinfo=UTC)


def _ctx() -> AnswerContext:
    return AnswerContext(
        chat_id=7,
        customer_username="darya",
        trace_id="trace-cal-off",
        now=_NOW,
        project_id=1,
    )


def _build(*, proposer_result):
    state_repo = _FakeStateRepo()
    proposer = _StubDateProposer(result=proposer_result)
    answerer = SalesPersonaAnswerer(
        state_repo=state_repo,
        services_repo=_FakeServicesRepo(),
        openrouter=_NeverCalledOpenRouter(),
        normalizer=get_russian_normalizer(),
        clock=lambda: _NOW,
        bot_persona_getter=lambda: "Николай",
        date_proposer=proposer,
    )
    state_repo.rows[7] = {
        "chat_id": 7,
        "project_id": 1,
        "current_stage": STAGE_PROPOSING,
        "collected_intent": Intent(dates="1 мая").to_dict(),
        "last_proposal": None,
        "last_customer_msg_at": None,
        "last_bot_msg_at": None,
    }
    return answerer, state_repo


@pytest.mark.asyncio
async def test_calendar_disabled_sends_fixed_line_and_escalates() -> None:
    answerer, _ = _build(
        proposer_result=NoProposal(reason=NO_PROPOSAL_CALENDAR_NOT_ENABLED)
    )
    result = await answerer.try_answer(question="ну что?", ctx=_ctx())

    assert result.handled is True
    assert result.text == PROPOSAL_FALLBACK_CALENDAR_DISABLED
    assert result.response_mode == RESPONSE_MODE_SALES_ESCALATION
    assert result.metadata["escalate"] is True
    assert result.metadata["hitl_reason"] == HITL_REASON_CALENDAR_DISABLED
    assert result.metadata["sales_turn_kind"] == "proposal_calendar_disabled"


@pytest.mark.asyncio
async def test_provider_error_sends_uncertain_line_and_escalates() -> None:
    answerer, _ = _build(
        proposer_result=NoProposal(reason=NO_PROPOSAL_PROVIDER_ERROR)
    )
    result = await answerer.try_answer(question="ну что?", ctx=_ctx())

    assert result.handled is True
    assert result.text == PROPOSAL_FALLBACK_UNAVAILABLE
    assert result.response_mode == RESPONSE_MODE_SALES_ESCALATION
    assert result.metadata["hitl_reason"] == HITL_REASON_PROPOSAL_FAILED
    assert result.metadata["sales_turn_kind"] == "proposal_provider_error"


@pytest.mark.asyncio
async def test_no_slots_sends_uncertain_line_and_escalates() -> None:
    answerer, _ = _build(
        proposer_result=NoProposal(reason=NO_PROPOSAL_NO_SLOTS_IN_WINDOW)
    )
    result = await answerer.try_answer(question="ну что?", ctx=_ctx())

    assert result.handled is True
    assert result.text == PROPOSAL_FALLBACK_UNAVAILABLE
    assert result.metadata["hitl_reason"] == HITL_REASON_PROPOSAL_FAILED
    assert result.metadata["sales_turn_kind"] == "proposal_no_slots"


@pytest.mark.asyncio
async def test_ambiguous_service_asks_clarifier_no_escalation() -> None:
    answerer, state_repo = _build(
        proposer_result=NoProposal(reason=NO_PROPOSAL_AMBIGUOUS_SERVICE)
    )
    result = await answerer.try_answer(question="ну что?", ctx=_ctx())

    assert result.handled is True
    assert result.text == PROPOSAL_AMBIGUOUS_SERVICE_CLARIFIER
    # Not an escalation — the customer clarifies and we re-enter proposing.
    assert result.response_mode is None
    assert result.metadata.get("escalate") is None
    assert result.metadata["sales_turn_kind"] == "proposal_ambiguous_service"
    # Stage stays in proposing.
    assert state_repo.upsert_calls[-1]["current_stage"] == STAGE_PROPOSING


@pytest.mark.asyncio
async def test_no_date_hint_asks_for_a_date() -> None:
    answerer, _ = _build(
        proposer_result=NoProposal(reason=NO_PROPOSAL_NO_DATE_HINT)
    )
    # Customer message has no parseable date — proposer returns no_date_hint.
    result = await answerer.try_answer(
        question="расскажите подробнее", ctx=_ctx()
    )

    assert result.handled is True
    assert result.text == "Какую дату хотите?"
    assert result.response_mode is None
    assert result.metadata.get("escalate") is None
    assert result.metadata["sales_turn_kind"] == "proposal_no_date_hint"

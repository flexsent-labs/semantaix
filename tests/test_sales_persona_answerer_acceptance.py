"""Acceptance & counter-offer tests for the ``proposing`` stage (Story 12.07).

A customer reply matching an acceptance lemma transitions the chat to
``closing``, delivers the closing line, and escalates with
``hitl_reason='sales_closing_handoff'``. A non-acceptance reply that
carries a new date re-enters ``proposing`` with the updated intent.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest

from services.api.app.answerers import AnswerContext
from services.api.app.russian_text import get_russian_normalizer
from services.api.app.sales.date_proposer import (
    NO_PROPOSAL_NO_SLOTS_IN_WINDOW,
    NoProposal,
    Proposal,
)
from services.api.app.sales.intent import Intent
from services.api.app.sales.sales_persona_answerer import (
    CLOSING_HANDOFF_LINE,
    HITL_REASON_CLOSING_HANDOFF,
    RESPONSE_MODE_SALES_ESCALATION,
    STAGE_CLOSING,
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


class _ProgrammableProposer:
    def __init__(self) -> None:
        self.queue: list = []
        self.calls: list[dict[str, Any]] = []

    def queue_result(self, result) -> None:
        self.queue.append(result)

    async def propose(self, *, project_id: int, intent: Intent, now: datetime):
        self.calls.append(
            {"project_id": project_id, "intent": intent, "now": now}
        )
        if not self.queue:
            raise AssertionError("DateProposer called without a queued result")
        return self.queue.pop(0)


_NOW = datetime(2026, 4, 25, 9, 0, tzinfo=UTC)


def _ctx() -> AnswerContext:
    return AnswerContext(
        chat_id=7,
        customer_username="darya",
        trace_id="trace-acc",
        now=_NOW,
        project_id=1,
    )


def _build():
    state_repo = _FakeStateRepo()
    openrouter = _FakeOpenRouter()
    proposer = _ProgrammableProposer()
    answerer = SalesPersonaAnswerer(
        state_repo=state_repo,
        services_repo=_FakeServicesRepo(),
        openrouter=openrouter,
        normalizer=get_russian_normalizer(),
        clock=lambda: _NOW,
        bot_persona_getter=lambda: "Николай",
        date_proposer=proposer,
    )
    return answerer, state_repo, openrouter, proposer


def _seed_with_proposal(state_repo: _FakeStateRepo) -> None:
    proposal = Proposal(
        date_iso="2026-05-01",
        start_time_iso="14:00",
        end_time_iso="15:00",
        service_id=42,
        proposed_at="2026-04-25T09:00:00+00:00",
    )
    state_repo.rows[7] = {
        "chat_id": 7,
        "project_id": 1,
        "current_stage": STAGE_PROPOSING,
        "collected_intent": Intent(dates="1 мая").to_dict(),
        "last_proposal": proposal.as_dict(),
        "last_customer_msg_at": None,
        "last_bot_msg_at": None,
    }


@pytest.mark.asyncio
async def test_acceptance_transitions_to_closing_and_escalates() -> None:
    answerer, state_repo, _, _ = _build()
    _seed_with_proposal(state_repo)

    result = await answerer.try_answer(question="да, согласен", ctx=_ctx())

    assert result.handled is True
    assert result.text == CLOSING_HANDOFF_LINE
    assert result.response_mode == RESPONSE_MODE_SALES_ESCALATION
    assert result.metadata["stage_before"] == STAGE_PROPOSING
    assert result.metadata["stage_after"] == STAGE_CLOSING
    assert result.metadata["escalate"] is True
    assert result.metadata["hitl_reason"] == HITL_REASON_CLOSING_HANDOFF

    # State row moved into closing.
    last = state_repo.upsert_calls[-1]
    assert last["current_stage"] == STAGE_CLOSING


@pytest.mark.asyncio
async def test_short_da_alone_counts_as_acceptance() -> None:
    answerer, state_repo, _, _ = _build()
    _seed_with_proposal(state_repo)

    result = await answerer.try_answer(question="да", ctx=_ctx())

    assert result.metadata["hitl_reason"] == HITL_REASON_CLOSING_HANDOFF
    assert result.metadata["stage_after"] == STAGE_CLOSING


@pytest.mark.asyncio
async def test_counter_offer_with_new_date_re_proposes() -> None:
    answerer, state_repo, openrouter, proposer = _build()
    _seed_with_proposal(state_repo)
    new_proposal = Proposal(
        date_iso="2026-05-02",
        start_time_iso="10:00",
        end_time_iso="11:00",
        service_id=42,
        proposed_at="2026-04-25T09:00:00+00:00",
    )
    proposer.queue_result(new_proposal)
    openrouter.queue_response(
        {"text": "Предлагаю на 2 мая с началом в 10:00."}
    )

    result = await answerer.try_answer(question="лучше 2 мая", ctx=_ctx())

    assert result.handled is True
    assert "2 мая" in (result.text or "")
    assert "10:00" in (result.text or "")
    # Stays in proposing — operator has not yet been engaged.
    assert result.metadata["stage_after"] == STAGE_PROPOSING
    # Proposer saw the updated intent.dates.
    last_call = proposer.calls[-1]
    assert "2 мая" in (last_call["intent"].dates or "")


@pytest.mark.asyncio
async def test_acceptance_only_triggers_when_prior_proposal_exists() -> None:
    """A bare 'да' on the FIRST proposing turn must not jump to closing.

    Without a prior proposal there is nothing to accept; the answerer must
    treat the turn as a normal proposing dispatch.
    """
    answerer, state_repo, openrouter, proposer = _build()
    # Seed proposing WITHOUT a prior proposal.
    state_repo.rows[7] = {
        "chat_id": 7,
        "project_id": 1,
        "current_stage": STAGE_PROPOSING,
        "collected_intent": Intent(dates="1 мая").to_dict(),
        "last_proposal": None,
        "last_customer_msg_at": None,
        "last_bot_msg_at": None,
    }
    proposer.queue_result(
        NoProposal(reason=NO_PROPOSAL_NO_SLOTS_IN_WINDOW)
    )

    result = await answerer.try_answer(question="да", ctx=_ctx())

    # Must NOT have transitioned to closing.
    assert result.metadata["stage_after"] == STAGE_PROPOSING
    # The closing handoff reason is for the escalation kind only.
    assert result.metadata.get("hitl_reason") != HITL_REASON_CLOSING_HANDOFF


@pytest.mark.asyncio
async def test_closing_stage_follow_up_repeats_handoff_line() -> None:
    """A message arriving while the chat is in ``closing`` re-emits the
    handoff line and re-escalates so the operator stays in the loop."""
    answerer, state_repo, _, _ = _build()
    proposal_dict = Proposal(
        date_iso="2026-05-01",
        start_time_iso="14:00",
        end_time_iso="15:00",
        service_id=42,
        proposed_at="2026-04-25T09:00:00+00:00",
    ).as_dict()
    state_repo.rows[7] = {
        "chat_id": 7,
        "project_id": 1,
        "current_stage": "closing",
        "collected_intent": Intent(dates="1 мая").to_dict(),
        "last_proposal": proposal_dict,
        "last_customer_msg_at": None,
        "last_bot_msg_at": None,
    }

    result = await answerer.try_answer(
        question="когда подтвердите?", ctx=_ctx()
    )

    assert result.handled is True
    assert result.text == "Передам коллегам для подтверждения, на связи."
    assert result.metadata["hitl_reason"] == HITL_REASON_CLOSING_HANDOFF
    assert result.metadata["stage_before"] == "closing"
    assert result.metadata["stage_after"] == "closing"
    assert result.metadata["sales_turn_kind"] == "closing_followup"
    # State stayed in closing.
    assert state_repo.upsert_calls[-1]["current_stage"] == "closing"


@pytest.mark.asyncio
async def test_no_date_proposer_falls_back_to_skip() -> None:
    """When the answerer is built without a DateProposer (legacy main.py
    wiring), the proposing stage falls through to the existing
    ``stage_not_implemented_yet`` skip — unchanged from earlier stories."""
    state_repo = _FakeStateRepo()
    answerer = SalesPersonaAnswerer(
        state_repo=state_repo,
        services_repo=_FakeServicesRepo(),
        openrouter=_FakeOpenRouter(),
        normalizer=get_russian_normalizer(),
        clock=lambda: _NOW,
        bot_persona_getter=lambda: "Николай",
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

    result = await answerer.try_answer(question="ну?", ctx=_ctx())

    assert result.handled is False
    assert result.metadata.get("skip_reason") == "stage_not_implemented_yet"

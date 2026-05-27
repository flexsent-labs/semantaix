"""Happy-path test for the ``proposing`` stage (Story 12.07).

A seeded ``proposing`` row + a ``DateProposer`` that returns a slot →
the answerer renders the Russian sentence with verbatim date + time
values, persists ``state.last_proposal``, and stays in proposing.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest

from services.api.app.answerers import AnswerContext
from services.api.app.russian_text import get_russian_normalizer
from services.api.app.sales.date_proposer import Proposal
from services.api.app.sales.intent import Intent
from services.api.app.sales.sales_persona_answerer import (
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


class _StubDateProposer:
    def __init__(self, result) -> None:
        self.result = result
        self.calls: list[dict[str, Any]] = []

    async def propose(self, *, project_id: int, intent: Intent, now: datetime):
        self.calls.append(
            {"project_id": project_id, "intent": intent, "now": now}
        )
        return self.result


_NOW = datetime(2026, 4, 25, 9, 0, tzinfo=UTC)


def _ctx() -> AnswerContext:
    return AnswerContext(
        chat_id=7,
        customer_username="darya",
        trace_id="trace-propose",
        now=_NOW,
        project_id=1,
    )


def _build(*, proposer_result):
    state_repo = _FakeStateRepo()
    openrouter = _FakeOpenRouter()
    proposer = _StubDateProposer(result=proposer_result)
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


def _seed_proposing(state_repo: _FakeStateRepo, *, intent: Intent) -> None:
    state_repo.rows[7] = {
        "chat_id": 7,
        "project_id": 1,
        "current_stage": STAGE_PROPOSING,
        "collected_intent": intent.to_dict(),
        "last_proposal": None,
        "last_customer_msg_at": None,
        "last_bot_msg_at": None,
    }


@pytest.mark.asyncio
async def test_proposing_renders_proposal_and_persists_last_proposal() -> None:
    proposal = Proposal(
        date_iso="2026-05-01",
        start_time_iso="14:00",
        end_time_iso="15:00",
        service_id=42,
        proposed_at="2026-04-25T09:00:00+00:00",
    )
    answerer, state_repo, openrouter, _ = _build(proposer_result=proposal)
    _seed_proposing(state_repo, intent=Intent(dates="1 мая"))
    openrouter.queue_response(
        {"text": "Предлагаю на 1 мая с началом в 14:00."}
    )

    result = await answerer.try_answer(
        question="ну что предложите?", ctx=_ctx()
    )

    assert result.handled is True
    assert result.text == "Предлагаю на 1 мая с началом в 14:00."
    assert result.metadata["stage_after"] == STAGE_PROPOSING
    assert result.metadata["sales_turn_kind"] == "proposal"
    assert result.metadata["proposal"]["date_iso"] == "2026-05-01"
    assert result.metadata["proposal"]["start_time_iso"] == "14:00"

    # state.last_proposal is the canonical dict shape from Proposal.as_dict.
    assert state_repo.upsert_calls
    last = state_repo.upsert_calls[-1]
    assert last["current_stage"] == STAGE_PROPOSING
    assert last["last_proposal"] == proposal.as_dict()


@pytest.mark.asyncio
async def test_proposing_prompt_carries_date_and_time_as_fixed_values() -> None:
    proposal = Proposal(
        date_iso="2026-06-15",
        start_time_iso="10:30",
        end_time_iso="11:30",
        service_id=42,
        proposed_at="2026-04-25T09:00:00+00:00",
    )
    answerer, state_repo, openrouter, _ = _build(proposer_result=proposal)
    _seed_proposing(state_repo, intent=Intent(dates="15 июня"))
    openrouter.queue_response(
        {"text": "Предлагаю на 15 июня с началом в 10:30."}
    )

    await answerer.try_answer(question="ну что?", ctx=_ctx())

    system = openrouter.calls[-1]["system"]
    assert "15 июня" in system
    assert "10:30" in system


@pytest.mark.asyncio
async def test_proposing_accepts_next_question_payload_shape() -> None:
    """The proposal LLM may return ``next_question`` instead of ``text``."""
    proposal = Proposal(
        date_iso="2026-05-01",
        start_time_iso="14:00",
        end_time_iso="15:00",
        service_id=42,
        proposed_at="2026-04-25T09:00:00+00:00",
    )
    answerer, state_repo, openrouter, _ = _build(proposer_result=proposal)
    _seed_proposing(state_repo, intent=Intent(dates="1 мая"))
    openrouter.queue_response(
        {"next_question": "Предлагаю на 1 мая с началом в 14:00."}
    )

    result = await answerer.try_answer(question="ну?", ctx=_ctx())

    assert result.handled is True
    assert "1 мая" in (result.text or "")
    assert "14:00" in (result.text or "")

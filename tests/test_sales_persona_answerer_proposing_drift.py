"""Drift-detection test for the ``proposing`` stage (Story 12.07).

If the LLM rewrites the date or the time, the answerer MUST NOT deliver
that text. Instead it escalates with ``reason='sales_proposal_drift'``
and replies with the generic ``Уточню свободные даты…`` fallback.
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
    HITL_REASON_PROPOSAL_DRIFT,
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


class _FakeOpenRouter:
    def __init__(self, payload: dict[str, Any]) -> None:
        self.payload = payload

    async def complete_json(
        self, *, system: str, user: str, model: str | None = None
    ) -> dict[str, Any]:
        return self.payload


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
        trace_id="trace-drift",
        now=_NOW,
        project_id=1,
    )


def _build(*, openrouter_payload: dict[str, Any]):
    state_repo = _FakeStateRepo()
    proposal = Proposal(
        date_iso="2026-05-01",
        start_time_iso="14:00",
        end_time_iso="15:00",
        service_id=42,
        proposed_at="2026-04-25T09:00:00+00:00",
    )
    proposer = _StubDateProposer(result=proposal)
    answerer = SalesPersonaAnswerer(
        state_repo=state_repo,
        services_repo=_FakeServicesRepo(),
        openrouter=_FakeOpenRouter(openrouter_payload),
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
async def test_drift_in_time_escalates_with_fallback_line() -> None:
    # LLM hallucinated a different start time.
    answerer, state_repo = _build(
        openrouter_payload={"text": "Предлагаю на 1 мая с началом в 15:30."}
    )
    result = await answerer.try_answer(
        question="ну что предложите?", ctx=_ctx()
    )

    assert result.handled is True
    assert result.text == PROPOSAL_FALLBACK_UNAVAILABLE
    assert result.response_mode == RESPONSE_MODE_SALES_ESCALATION
    assert result.metadata["escalate"] is True
    assert result.metadata["hitl_reason"] == HITL_REASON_PROPOSAL_DRIFT
    assert result.metadata["expected_date"] == "1 мая"
    assert result.metadata["expected_time"] == "14:00"

    # Drift means the proposal is NOT persisted as ``last_proposal``.
    last = state_repo.upsert_calls[-1]
    assert last.get("last_proposal") is None


@pytest.mark.asyncio
async def test_drift_in_date_escalates() -> None:
    answerer, _ = _build(
        openrouter_payload={"text": "Предлагаю на 2 мая с началом в 14:00."}
    )
    result = await answerer.try_answer(question="ну?", ctx=_ctx())

    assert result.handled is True
    assert result.text == PROPOSAL_FALLBACK_UNAVAILABLE
    assert result.metadata["hitl_reason"] == HITL_REASON_PROPOSAL_DRIFT


@pytest.mark.asyncio
async def test_empty_llm_output_escalates() -> None:
    answerer, _ = _build(openrouter_payload={"text": ""})
    result = await answerer.try_answer(question="ну?", ctx=_ctx())

    assert result.handled is True
    assert result.metadata["hitl_reason"] == HITL_REASON_PROPOSAL_DRIFT


class _RaisingOpenRouter:
    async def complete_json(
        self, *, system: str, user: str, model: str | None = None
    ) -> dict[str, Any]:
        raise RuntimeError("transport down")


class _StubProposer:
    def __init__(self, proposal: Proposal) -> None:
        self.proposal = proposal

    async def propose(self, *, project_id: int, intent: Intent, now: datetime):
        return self.proposal


@pytest.mark.asyncio
async def test_llm_transport_failure_escalates_with_drift_reason() -> None:
    state_repo = _FakeStateRepo()
    proposal = Proposal(
        date_iso="2026-05-01",
        start_time_iso="14:00",
        end_time_iso="15:00",
        service_id=42,
        proposed_at="2026-04-25T09:00:00+00:00",
    )
    answerer = SalesPersonaAnswerer(
        state_repo=state_repo,
        services_repo=_FakeServicesRepo(),
        openrouter=_RaisingOpenRouter(),
        normalizer=get_russian_normalizer(),
        clock=lambda: _NOW,
        bot_persona_getter=lambda: "Николай",
        date_proposer=_StubProposer(proposal),
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

    assert result.handled is True
    assert result.text == PROPOSAL_FALLBACK_UNAVAILABLE
    assert result.metadata["hitl_reason"] == HITL_REASON_PROPOSAL_DRIFT
    # drift_text is ``None`` when the LLM never returned anything.
    assert result.metadata["drift_text"] is None


@pytest.mark.asyncio
async def test_non_dict_payload_escalates() -> None:
    class _NonDictOpenRouter:
        async def complete_json(
            self, *, system: str, user: str, model: str | None = None
        ) -> Any:
            return "not a dict"  # type: ignore[return-value]

    state_repo = _FakeStateRepo()
    proposal = Proposal(
        date_iso="2026-05-01",
        start_time_iso="14:00",
        end_time_iso="15:00",
        service_id=42,
        proposed_at="2026-04-25T09:00:00+00:00",
    )
    answerer = SalesPersonaAnswerer(
        state_repo=state_repo,
        services_repo=_FakeServicesRepo(),
        openrouter=_NonDictOpenRouter(),
        normalizer=get_russian_normalizer(),
        clock=lambda: _NOW,
        bot_persona_getter=lambda: "Николай",
        date_proposer=_StubProposer(proposal),
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

    assert result.metadata["hitl_reason"] == HITL_REASON_PROPOSAL_DRIFT


@pytest.mark.asyncio
async def test_extra_time_string_in_text_still_drifts() -> None:
    # Canonical "14:00" is present BUT the LLM also emitted "14:30" — the
    # verifier extracts every H:MM and rejects when any disagrees.
    answerer, _ = _build(
        openrouter_payload={
            "text": "Предлагаю на 1 мая с началом в 14:00 или в 14:30."
        }
    )
    result = await answerer.try_answer(question="ну?", ctx=_ctx())

    assert result.metadata["hitl_reason"] == HITL_REASON_PROPOSAL_DRIFT

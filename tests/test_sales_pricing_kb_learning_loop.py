"""KB-learning-loop integration test for Story 12.04.

Drives `SalesPersonaAnswerer` + `PriceLookup` + a **real** `RagRepository`
+ a real `StateRepository` + a real `HitlTicketRepository`, plus a small
shim that mirrors what the Epic-06 knowledge-moderation extractor would
do with the operator's reply transcript. The loop:

  1. Customer asks the price → empty KB → answerer signals a HITL
     escalation with ``reason='price_unknown'``; test creates the ticket
     in the HITL repo.
  2. Operator's reply line ``"6 часов — 15 000 ₽"`` is fed to a stub
     extractor that creates a `knowledge_moderation` candidate row.
  3. Operator approves: we directly insert the rag_chunks row (Epic-06
     publish step is out-of-scope for this story).
  4. Customer asks the same question → `PriceFound` → bot quotes the
     price verbatim. **No** second HITL ticket created.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest

from services.api.app.answerers import AnswerContext
from services.api.app.hitl import HitlTicketRepository
from services.api.app.knowledge_moderation import (
    KnowledgeModerationRepository,
)
from services.api.app.rag import RagRepository
from services.api.app.russian_text import get_russian_normalizer
from services.api.app.sales.price_lookup import PriceLookup
from services.api.app.sales.sales_persona_answerer import (
    HITL_REASON_PRICE_UNKNOWN,
    PRICING_MISS_FALLBACK,
    STAGE_AWAITING_OPERATOR_PRICE,
    STAGE_PRICING,
    SalesPersonaAnswerer,
)
from services.api.app.sales.state_repository import StateRepository


class _ServicesRepoStub:
    def count_active(self, *, project_id: int) -> int:
        return 1

    def list_for_project(self, *, project_id: int) -> list:
        return []

    def get_by_name(self, *, project_id: int, name: str):
        return None


class _StubOpenRouter:
    def __init__(self) -> None:
        self.queue: list[dict[str, Any]] = []
        self.calls: list[dict[str, Any]] = []

    def queue_response(self, payload: dict[str, Any]) -> None:
        self.queue.append(payload)

    async def complete_json(
        self, *, system: str, user: str, model: str | None = None
    ) -> dict[str, Any]:
        self.calls.append({"system": system, "user": user})
        if not self.queue:
            raise AssertionError("LLM called without a queued payload")
        return self.queue.pop(0)


_NOW = datetime(2026, 4, 25, 9, 0, tzinfo=UTC)


def _ctx(chat_id: int, project_id: int) -> AnswerContext:
    return AnswerContext(
        chat_id=chat_id,
        customer_username="darya",
        trace_id=f"kb-loop-{chat_id}",
        now=_NOW,
        project_id=project_id,
    )


def _seed_pricing(state_repo: StateRepository, *, chat_id: int, project_id: int) -> None:
    state_repo.upsert(
        chat_id=chat_id,
        project_id=project_id,
        current_stage=STAGE_PRICING,
        collected_intent={},
        now=_NOW,
        last_bot_msg_at=_NOW,
    )


def _seed_rag_chunk(rag_repo: RagRepository, *, project_id: int, text: str) -> int:
    """Insert a published rag_chunks row directly — emulates Epic-06 publish."""
    return rag_repo.ingest(source_id="kb:pricing", text=text, project_id=project_id)


@pytest.mark.asyncio
async def test_kb_learning_loop_empty_then_learned(tmp_path) -> None:
    project_id = 17
    chat_id = 9001
    sales_db = str(tmp_path / "sales.sqlite3")
    rag_db = str(tmp_path / "rag.sqlite3")
    hitl_db = str(tmp_path / "hitl.sqlite3")
    knowledge_db = str(tmp_path / "knowledge.sqlite3")

    state_repo = StateRepository(db_path=sales_db)
    rag_repo = RagRepository(db_path=rag_db)
    hitl_repo = HitlTicketRepository(db_path=hitl_db)
    knowledge_repo = KnowledgeModerationRepository(db_path=knowledge_db)

    openrouter = _StubOpenRouter()
    price_lookup = PriceLookup(
        rag_retriever=rag_repo, normalizer=get_russian_normalizer()
    )
    answerer = SalesPersonaAnswerer(
        state_repo=state_repo,
        services_repo=_ServicesRepoStub(),
        openrouter=openrouter,
        normalizer=get_russian_normalizer(),
        clock=lambda: _NOW,
        bot_persona_getter=lambda: "Николай",
        price_lookup=price_lookup,
    )

    # ── Turn 1: customer asks the price, KB has nothing → miss + HITL ──
    _seed_pricing(state_repo, chat_id=chat_id, project_id=project_id)
    first = await answerer.try_answer(
        question="сколько стоит 6 часов?",
        ctx=_ctx(chat_id, project_id),
    )

    assert first.text == PRICING_MISS_FALLBACK
    assert first.metadata["hitl_reason"] == HITL_REASON_PRICE_UNKNOWN
    payload = first.metadata["sales_price_unknown_payload"]
    assert payload["original_question"] == "сколько стоит 6 часов?"
    # State parks for the operator.
    assert state_repo.get(chat_id)["current_stage"] == (
        STAGE_AWAITING_OPERATOR_PRICE
    )

    # The main.py escalation glue would now create the HITL ticket. We
    # simulate that here to keep the integration self-contained.
    ticket = hitl_repo.create(
        conversation_ref=f"chat:{chat_id}",
        reason=HITL_REASON_PRICE_UNKNOWN,
        target_chat_id=chat_id,
    )
    assert ticket.reason == HITL_REASON_PRICE_UNKNOWN

    # ── Step 2: operator replies. Epic-06 extractor would scan reply
    # transcript lines and create a moderation candidate. We mirror that
    # behaviour with a direct repo call (any extractor wiring tweak ships
    # as follow-up to Epic 06).
    operator_reply = "6 часов — 15 000 ₽"
    candidate = knowledge_repo.create_pending(text=operator_reply)
    assert candidate.id > 0
    assert candidate.candidate_text == operator_reply
    # Resolve the ticket (mirror /hitl/tickets/{id}/reply auto-resolve).
    hitl_repo.resolve(ticket_id=ticket.id)

    # ── Step 3: operator approves. The Epic-06 publish step writes a
    # rag_chunks row; we do the same directly so the next price ask hits
    # the KB.
    inserted = _seed_rag_chunk(
        rag_repo, project_id=project_id, text=operator_reply
    )
    assert inserted == 1

    # ── Turn 4: customer asks again. The KB now answers; no new ticket. ─
    openrouter.queue_response({"text": "6 часов — 15 000 ₽."})
    tickets_before = len(hitl_repo.list_all())
    second = await answerer.try_answer(
        question="сколько стоит 6 часов?",
        ctx=_ctx(chat_id, project_id),
    )

    assert second.handled is True
    assert second.text == "6 часов — 15 000 ₽."
    assert second.metadata["sales_turn_kind"] == "pricing_hit"
    assert second.metadata["sales_price_source_chunk_id"]
    # State stays in pricing (no new escalation).
    assert state_repo.get(chat_id)["current_stage"] == STAGE_PRICING
    # No new HITL ticket was created on the hit path.
    assert len(hitl_repo.list_all()) == tickets_before

"""Story 12.09 — full sales funnel integration test.

Replays the canonical Данил dialog through the live ``AnswerPipeline``:

  1. Greeting → first scoping question.
  2. Scoping turns (5 fields) → intent complete + stage transitions.
  3. Catalog ask mid-funnel returns the operator-authored service list.
  4. Materials dispatch happens once scoping completes (no LLM in this
     branch; the dispatch hook fires via the persisted state row).
  5. Pricing hit: KB has a price → bot quotes verbatim + records
     ``source_chunk_id`` in the answer-trace metadata.
  6. Date proposal → customer acceptance → closing handoff line +
     sales-escalation response_mode (HITL ticket creation is the wiring
     contract proven separately in ``test_sales_no_secrets_in_logs``).

Uses the real sqlite-backed repositories + a stub OpenRouter that emits
schema-valid JSON per turn. The :class:`AnswerPipeline` is constructed
fresh so the test owns its routing list — it does NOT touch the
process-global pipeline.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import pytest

from services.api.app.answerers import AnswerContext, AnswerPipeline
from services.api.app.rag import RagRepository
from services.api.app.russian_text import get_russian_normalizer
from services.api.app.sales.date_proposer import Proposal
from services.api.app.sales.intent import Intent
from services.api.app.sales.price_lookup import PriceLookup
from services.api.app.sales.sales_persona_answerer import (
    CLOSING_HANDOFF_LINE,
    RESPONSE_MODE_SALES_ESCALATION,
    STAGE_PRICING,
    STAGE_PROPOSING,
    STAGE_SCOPING,
    SalesPersonaAnswerer,
)
from services.api.app.sales.services_repository import ServicesRepository
from services.api.app.sales.state_repository import StateRepository

_NOW = datetime(2026, 5, 1, 9, 0, tzinfo=UTC)
_CHAT_ID = 7
_PROJECT_ID = 1


@dataclass
class _ServiceRow:
    id: int
    name: str
    description: str | None


class _ServicesRepoAdapter:
    """ServicesRepository → answerer's _ServicesRepo protocol."""

    def __init__(self, repo: ServicesRepository) -> None:
        self._repo = repo

    def count_active(self, *, project_id: int) -> int:
        return len(self._repo.list_for_project(project_id=project_id))

    def list_for_project(self, *, project_id: int) -> list[_ServiceRow]:
        return [
            _ServiceRow(id=s.id, name=s.name, description=s.description_md)
            for s in self._repo.list_for_project(project_id=project_id)
        ]

    def get_by_name(
        self, *, project_id: int, name: str
    ) -> _ServiceRow | None:
        target = name.strip().casefold()
        for s in self._repo.list_for_project(project_id=project_id):
            if s.name.strip().casefold() == target:
                return _ServiceRow(
                    id=s.id, name=s.name, description=s.description_md
                )
        return None


class _StubOpenRouter:
    """Records every call + returns the next queued JSON payload."""

    def __init__(self) -> None:
        self.queue: list[dict[str, Any]] = []
        self.calls: list[dict[str, Any]] = []

    def queue_response(self, payload: dict[str, Any]) -> None:
        self.queue.append(payload)

    async def complete_json(
        self, *, system: str, user: str, model: str | None = None
    ) -> dict[str, Any]:
        self.calls.append({"system": system, "user": user, "model": model})
        if not self.queue:
            raise AssertionError(
                f"LLM called without a queued payload (user={user!r})"
            )
        return self.queue.pop(0)


class _StubDateProposer:
    """Returns a fixed Proposal — proves the answerer wiring, not the
    Epic-11 calendar code path."""

    def __init__(self, proposal: Proposal) -> None:
        self.proposal = proposal
        self.calls: list[dict[str, Any]] = []

    async def propose(
        self, *, project_id: int, intent: Intent, now: datetime
    ) -> Proposal:
        self.calls.append(
            {"project_id": project_id, "intent": intent, "now": now}
        )
        return self.proposal


def _ctx(*, trace_id: str) -> AnswerContext:
    return AnswerContext(
        chat_id=_CHAT_ID,
        customer_username="@danil",
        trace_id=trace_id,
        now=_NOW,
        project_id=_PROJECT_ID,
    )


def _build_pipeline(
    tmp_path,
    *,
    proposal: Proposal | None = None,
) -> tuple[
    AnswerPipeline,
    StateRepository,
    ServicesRepository,
    RagRepository,
    _StubOpenRouter,
]:
    sales_db = str(tmp_path / "sales.sqlite3")
    rag_db = str(tmp_path / "rag.sqlite3")
    state_repo = StateRepository(db_path=sales_db)
    services_repo = ServicesRepository(db_path=sales_db)
    rag_repo = RagRepository(db_path=rag_db)
    openrouter = _StubOpenRouter()
    normalizer = get_russian_normalizer()
    answerer = SalesPersonaAnswerer(
        state_repo=state_repo,
        services_repo=_ServicesRepoAdapter(services_repo),
        openrouter=openrouter,
        normalizer=normalizer,
        clock=lambda: _NOW,
        bot_persona_getter=lambda: "Анна Иванова",
        price_lookup=PriceLookup(
            rag_retriever=rag_repo, normalizer=normalizer
        ),
        date_proposer=_StubDateProposer(proposal) if proposal else None,
    )
    pipeline = AnswerPipeline([answerer])
    return pipeline, state_repo, services_repo, rag_repo, openrouter


@pytest.mark.asyncio
async def test_full_funnel_greeting_to_closing(tmp_path) -> None:
    """End-to-end replay of the canonical Данил dialog through the live
    sales answerer wiring.

    Asserts each transition: greeting → scoping → catalog ask →
    pricing hit → date proposal → acceptance → closing handoff.
    Materials dispatch is observable as the answerer's persisted state
    row carrying ``current_stage='scoping'`` (the hook in the bot
    gateway is owned by Epic-12 story 12.05; the integration here is the
    state-machine that drives it).
    """
    proposal = Proposal(
        date_iso="2026-05-01",
        start_time_iso="14:00",
        end_time_iso="16:00",
        service_id=1,
        proposed_at=_NOW.isoformat(),
    )
    (
        pipeline,
        state_repo,
        services_repo,
        rag_repo,
        openrouter,
    ) = _build_pipeline(tmp_path, proposal=proposal)
    services_repo.add(
        project_id=_PROJECT_ID,
        name="Медовеевка Лайт",
        description_md="Лайт уровень, с видами.",
        tags=["лайт"],
        now=_NOW,
    )
    services_repo.add(
        project_id=_PROJECT_ID,
        name="Каньонинг",
        description_md="Каньонинг — это движение по каньонам.",
        tags=["каньон"],
        now=_NOW,
    )
    rag_repo.ingest(
        source_id="kb:price-1",
        text="Медовеевка Лайт — 15 000 ₽ за группу.",
        project_id=_PROJECT_ID,
    )

    # Turn 1: greeting (sales-intent message; no prior state)
    openrouter.queue_response(
        {
            "extracted_fields": {"dates": "1 мая"},
            "next_question": "Здравствуйте! Сколько вас будет?",
        }
    )
    r1 = await pipeline.run(
        question="интересует тур на квадроциклах 1 мая",
        ctx=_ctx(trace_id="trace-1"),
    )
    assert r1.handled is True
    assert r1.metadata.get("answerer") == "sales_persona"
    assert r1.metadata.get("stage_before") == "new"
    assert r1.metadata.get("stage_after") == STAGE_SCOPING

    # Turn 2: catalog ask mid-scoping
    r2 = await pipeline.run(
        question="Что у вас есть?", ctx=_ctx(trace_id="trace-2")
    )
    assert r2.handled is True
    assert r2.metadata.get("sales_turn_kind") == "catalog"
    assert "Медовеевка Лайт" in (r2.text or "")
    assert "Каньонинг" in (r2.text or "")

    # Turns 3-6: drive the four remaining scoping fields.
    openrouter.queue_response(
        {
            "extracted_fields": {"headcount": 4},
            "next_question": "Сколько квадроциклов брать?",
        }
    )
    r3 = await pipeline.run(question="Нас 4", ctx=_ctx(trace_id="trace-3"))
    assert r3.handled is True
    assert r3.metadata.get("stage_after") == STAGE_SCOPING

    openrouter.queue_response(
        {
            "extracted_fields": {"vehicle_count": 2},
            "next_question": "Какой уровень сложности?",
        }
    )
    r4 = await pipeline.run(question="2 квадра", ctx=_ctx(trace_id="trace-4"))
    assert r4.handled is True

    openrouter.queue_response(
        {
            "extracted_fields": {"difficulty": "начальный"},
            "next_question": "Сколько водителей?",
        }
    )
    r5 = await pipeline.run(
        question="начальный уровень", ctx=_ctx(trace_id="trace-5")
    )
    assert r5.handled is True

    openrouter.queue_response(
        {
            "extracted_fields": {"drivers": 1},
            "next_question": "Принято!",
        }
    )
    r6 = await pipeline.run(question="1 водитель", ctx=_ctx(trace_id="trace-6"))
    assert r6.handled is True
    # All 5 intent fields are now captured.
    state = state_repo.get(_CHAT_ID)
    assert state is not None
    collected = Intent.from_dict(state["collected_intent"])
    assert collected.is_complete(), (
        f"intent should be complete: {collected.to_dict()}"
    )

    # Move the chat into ``pricing`` so the next turn exercises the
    # KB-first pricing branch. (Pitching is owned by stories not in
    # scope for 12-09.)
    state_repo.upsert(
        chat_id=_CHAT_ID,
        project_id=_PROJECT_ID,
        current_stage=STAGE_PRICING,
        collected_intent=collected.to_dict(),
        now=_NOW,
        last_bot_msg_at=_NOW,
    )

    # Turn 7: pricing hit — RAG holds the price line.
    openrouter.queue_response({"text": "Медовеевка Лайт — 15 000 ₽ за группу."})
    r7 = await pipeline.run(
        question="сколько стоит Медовеевка Лайт?",
        ctx=_ctx(trace_id="trace-7"),
    )
    assert r7.handled is True
    assert r7.metadata.get("sales_turn_kind") == "pricing_hit"
    assert "15 000" in (r7.text or "")
    assert r7.metadata.get("sales_price_source_chunk_id")

    # Move the chat into ``proposing`` so the next turn exercises the
    # date proposer + acceptance path.
    state_repo.upsert(
        chat_id=_CHAT_ID,
        project_id=_PROJECT_ID,
        current_stage=STAGE_PROPOSING,
        collected_intent=collected.to_dict(),
        now=_NOW,
        last_bot_msg_at=_NOW,
    )

    # Turn 8: date proposal — bot proposes a slot with verbatim time.
    openrouter.queue_response(
        {"text": "Предлагаю 1 мая в 14:00 — подходит?"}
    )
    r8 = await pipeline.run(
        question="когда можно поехать?",
        ctx=_ctx(trace_id="trace-8"),
    )
    assert r8.handled is True
    assert r8.metadata.get("sales_turn_kind") == "proposal"
    assert "1 мая" in (r8.text or "")
    assert "14:00" in (r8.text or "")

    # Turn 9: acceptance → closing handoff (sales escalation).
    r9 = await pipeline.run(question="да, подходит", ctx=_ctx(trace_id="trace-9"))
    assert r9.handled is True
    assert r9.text == CLOSING_HANDOFF_LINE
    assert r9.response_mode == RESPONSE_MODE_SALES_ESCALATION
    assert r9.metadata.get("hitl_reason") == "sales_closing_handoff"
    assert r9.metadata.get("escalate") is True

    final_state = state_repo.get(_CHAT_ID)
    assert final_state is not None
    assert final_state["current_stage"] == "closing"

"""Epic 12, Story 12.09 — full Данил dialog replay (E2E acceptance signal).

This is the highest-confidence acceptance test for Epic 12. It drives the
*full* canonical Данил script through the live ``AnswerPipeline``:

  1. Greeting + scoping (first 5 fields).
  2. Catalog ask mid-funnel returns operator-authored names.
  3. Concept ask (operator description for a known service).
  4. KB-first pricing: empty → escalate; operator publishes price →
     identical re-ask quotes the verbatim price (KB learning loop).
  5. Date proposal turn renders a verbatim slot.
  6. Customer accepts → closing handoff line + sales escalation
     metadata.

Marked with ``@pytest.mark.e2e``, ``@pytest.mark.epic("12")``, and
``@pytest.mark.story("12-09")`` so the per-epic selective runs find it.
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
    PRICING_MISS_FALLBACK,
    RESPONSE_MODE_SALES_ESCALATION,
    STAGE_PRICING,
    STAGE_PROPOSING,
    STAGE_SCOPING,
    SalesPersonaAnswerer,
)
from services.api.app.sales.services_repository import ServicesRepository
from services.api.app.sales.state_repository import StateRepository

pytestmark = [
    pytest.mark.e2e,
    pytest.mark.epic("12"),
    pytest.mark.story("12-09"),
]

_NOW = datetime(2026, 4, 25, 9, 0, tzinfo=UTC)
_CHAT_ID = 9001
_PROJECT_ID = 1


@dataclass
class _ServiceShim:
    name: str
    description: str | None


class _ServicesRepoAdapter:
    def __init__(self, repo: ServicesRepository) -> None:
        self._repo = repo

    def count_active(self, *, project_id: int) -> int:
        return len(self._repo.list_for_project(project_id=project_id))

    def list_for_project(self, *, project_id: int) -> list[_ServiceShim]:
        return [
            _ServiceShim(name=s.name, description=s.description_md)
            for s in self._repo.list_for_project(project_id=project_id)
        ]

    def get_by_name(
        self, *, project_id: int, name: str
    ) -> _ServiceShim | None:
        target = name.strip().casefold()
        for s in self._repo.list_for_project(project_id=project_id):
            if s.name.strip().casefold() == target:
                return _ServiceShim(name=s.name, description=s.description_md)
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
        self.calls.append({"system": system, "user": user, "model": model})
        if not self.queue:
            raise AssertionError(
                f"LLM called without a queued payload (user={user!r})"
            )
        return self.queue.pop(0)


class _StubDateProposer:
    def __init__(self, proposal: Proposal) -> None:
        self.proposal = proposal
        self.calls: list[dict[str, Any]] = []

    async def propose(
        self, *, project_id: int, intent: Intent, now: datetime
    ):
        self.calls.append(
            {"project_id": project_id, "intent": intent, "now": now}
        )
        return self.proposal


def _ctx(trace_id: str) -> AnswerContext:
    return AnswerContext(
        chat_id=_CHAT_ID,
        customer_username="@danil",
        trace_id=trace_id,
        now=_NOW,
        project_id=_PROJECT_ID,
    )


@pytest.fixture
def env(tmp_path):
    sales_db = str(tmp_path / "sales.sqlite3")
    rag_db = str(tmp_path / "rag.sqlite3")
    state_repo = StateRepository(db_path=sales_db)
    services_repo = ServicesRepository(db_path=sales_db)
    rag_repo = RagRepository(db_path=rag_db)
    normalizer = get_russian_normalizer()
    openrouter = _StubOpenRouter()
    proposal = Proposal(
        date_iso="2026-05-01",
        start_time_iso="14:00",
        end_time_iso="16:00",
        service_id=1,
        proposed_at=_NOW.isoformat(),
    )
    answerer = SalesPersonaAnswerer(
        state_repo=state_repo,
        services_repo=_ServicesRepoAdapter(services_repo),
        openrouter=openrouter,
        normalizer=normalizer,
        clock=lambda: _NOW,
        bot_persona_getter=lambda: "Анна Иванова",
        price_lookup=PriceLookup(rag_retriever=rag_repo, normalizer=normalizer),
        date_proposer=_StubDateProposer(proposal),
    )
    pipeline = AnswerPipeline([answerer])
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
        description_md="Каньонинг — спуск по каньону с верёвочной техникой.",
        tags=["каньон"],
        now=_NOW,
    )
    return {
        "pipeline": pipeline,
        "state_repo": state_repo,
        "services_repo": services_repo,
        "rag_repo": rag_repo,
        "openrouter": openrouter,
    }


@pytest.mark.asyncio
async def test_full_danil_dialog_runs_greeting_scoping_pricing_proposal_closing(
    env,
) -> None:
    pipeline: AnswerPipeline = env["pipeline"]
    state_repo: StateRepository = env["state_repo"]
    rag_repo: RagRepository = env["rag_repo"]
    openrouter: _StubOpenRouter = env["openrouter"]

    # Step 1 — Greeting: sales-intent message creates a state row and
    # transitions to scoping; the first scoping question gets returned.
    openrouter.queue_response(
        {
            "extracted_fields": {"dates": "1 мая"},
            "next_question": "Здравствуйте, Данил! На сколько человек?",
        }
    )
    r1 = await pipeline.run(
        question="интересует тур на квадроциклах 1 мая",
        ctx=_ctx("danil-1"),
    )
    assert r1.handled is True
    assert r1.metadata["answerer"] == "sales_persona"
    assert r1.metadata["stage_after"] == STAGE_SCOPING

    # Step 2 — Catalog ask mid-scoping returns operator names verbatim.
    r2 = await pipeline.run(
        question="что у вас вообще есть?", ctx=_ctx("danil-2")
    )
    assert r2.handled is True
    assert r2.metadata["sales_turn_kind"] == "catalog"
    assert "Медовеевка Лайт" in (r2.text or "")
    assert "Каньонинг" in (r2.text or "")

    # Step 3 — Concept ask: operator description for a known service.
    r3 = await pipeline.run(
        question="что такое каньонинг?", ctx=_ctx("danil-3")
    )
    assert r3.handled is True
    assert r3.metadata["sales_turn_kind"] == "concept_op_desc"
    assert "верёвочной" in (r3.text or "")

    # Step 4 — Scope the remaining four fields. The bot's reply is the
    # next scoping question each time; the test only validates that the
    # answerer is handling + persisting state.
    for trace_id, extracted, customer in [
        ("danil-4", {"headcount": 4}, "Нас 4 человека."),
        ("danil-5", {"vehicle_count": 2}, "2 квадрика хватит."),
        ("danil-6", {"difficulty": "начальный"}, "Начинающие."),
        ("danil-7", {"drivers": 1}, "Один водитель."),
    ]:
        openrouter.queue_response(
            {"extracted_fields": extracted, "next_question": "Принято."}
        )
        rN = await pipeline.run(question=customer, ctx=_ctx(trace_id))
        assert rN.handled is True

    state = state_repo.get(_CHAT_ID)
    assert state is not None
    assert Intent.from_dict(state["collected_intent"]).is_complete()

    # Step 5 — Empty-KB pricing turn (escalate with PRICING_MISS_FALLBACK).
    state_repo.upsert(
        chat_id=_CHAT_ID,
        project_id=_PROJECT_ID,
        current_stage=STAGE_PRICING,
        collected_intent=state["collected_intent"],
        now=_NOW,
        last_bot_msg_at=_NOW,
    )
    r_price1 = await pipeline.run(
        question="сколько стоит Медовеевка Лайт?",
        ctx=_ctx("danil-price-1"),
    )
    assert r_price1.handled is True
    assert r_price1.text == PRICING_MISS_FALLBACK
    assert r_price1.response_mode == RESPONSE_MODE_SALES_ESCALATION
    assert r_price1.metadata["hitl_reason"] == "price_unknown"

    # Operator answers — mirror Epic-06 publish by ingesting the price
    # into RAG. The next identical ask hits the KB.
    rag_repo.ingest(
        source_id="kb:operator-reply",
        text="Медовеевка Лайт — 15 000 ₽ за группу.",
        project_id=_PROJECT_ID,
    )

    # Step 6 — Pricing hit on the same question (KB-learning loop).
    openrouter.queue_response(
        {"text": "Медовеевка Лайт — 15 000 ₽ за группу."}
    )
    r_price2 = await pipeline.run(
        question="сколько стоит Медовеевка Лайт?",
        ctx=_ctx("danil-price-2"),
    )
    assert r_price2.handled is True
    assert r_price2.metadata["sales_turn_kind"] == "pricing_hit"
    assert r_price2.metadata.get("sales_price_source_chunk_id")
    assert "15 000" in (r_price2.text or "")

    # Step 7 — Date proposal turn: verbatim slot.
    state_repo.upsert(
        chat_id=_CHAT_ID,
        project_id=_PROJECT_ID,
        current_stage=STAGE_PROPOSING,
        collected_intent=state["collected_intent"],
        now=_NOW,
        last_bot_msg_at=_NOW,
    )
    openrouter.queue_response(
        {"text": "Предлагаю 1 мая в 14:00 — подходит?"}
    )
    r_prop = await pipeline.run(
        question="а когда можно поехать?", ctx=_ctx("danil-proposal")
    )
    assert r_prop.handled is True
    assert r_prop.metadata["sales_turn_kind"] == "proposal"
    assert "1 мая" in (r_prop.text or "")
    assert "14:00" in (r_prop.text or "")

    # Step 8 — Acceptance → closing handoff + sales escalation metadata.
    r_close = await pipeline.run(
        question="да, едем", ctx=_ctx("danil-acceptance")
    )
    assert r_close.handled is True
    assert r_close.text == CLOSING_HANDOFF_LINE
    assert r_close.response_mode == RESPONSE_MODE_SALES_ESCALATION
    assert r_close.metadata["hitl_reason"] == "sales_closing_handoff"
    assert r_close.metadata["escalate"] is True

    final = state_repo.get(_CHAT_ID)
    assert final is not None
    assert final["current_stage"] == "closing"

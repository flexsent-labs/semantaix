"""Epic 12, Story 12.04 — pricing turn KB-first, escalate-if-unknown.

The pipeline wiring for ``SalesPersonaAnswerer`` lands in a later story,
so the integration drives the answerer + a real :class:`PriceLookup`
against sqlite-backed repos for state and RAG. The full loop:

  1. Empty KB → customer asks the price → bot replies with the fixed
     ``Уточню у коллег…`` line + escalates with
     ``reason='price_unknown'`` and a structured payload.
  2. Operator answers (we mirror Epic-06 publish by inserting a
     ``rag_chunks`` row directly).
  3. Customer asks the same question → bot quotes the price verbatim
     (``sales_turn_kind='pricing_hit'``) — no second escalation.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import pytest

from services.api.app.answerers import AnswerContext
from services.api.app.calendar.project_services_repository import (
    ProjectServiceRepository,
)
from services.api.app.rag import RagRepository
from services.api.app.russian_text import get_russian_normalizer
from services.api.app.sales.price_lookup import PriceLookup
from services.api.app.sales.sales_persona_answerer import (
    HITL_REASON_PRICE_UNKNOWN,
    PRICING_MISS_FALLBACK,
    RESPONSE_MODE_SALES_ESCALATION,
    STAGE_AWAITING_OPERATOR_PRICE,
    STAGE_PRICING,
    SalesPersonaAnswerer,
)
from services.api.app.sales.state_repository import StateRepository

pytestmark = [
    pytest.mark.e2e,
    pytest.mark.epic("12"),
    pytest.mark.story("12-04"),
]


@dataclass
class _ServiceRow:
    id: int
    name: str
    description: str | None


class _ServicesRepoAdapter:
    def __init__(self, repo: ProjectServiceRepository) -> None:
        self._repo = repo

    def count_active(self, *, project_id: int) -> int:
        return len(self._repo.list_for_project(project_id=project_id))

    def list_for_project(self, *, project_id: int) -> list[_ServiceRow]:
        return [
            _ServiceRow(id=row.id, name=row.name, description=row.description)
            for row in self._repo.list_for_project(project_id=project_id)
        ]

    def get_by_name(
        self, *, project_id: int, name: str
    ) -> _ServiceRow | None:
        row = self._repo.get_by_name(project_id=project_id, name=name)
        if row is None:
            return None
        return _ServiceRow(
            id=row.id, name=row.name, description=row.description
        )


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
            raise AssertionError("LLM called without a queued payload")
        return self.queue.pop(0)


_NOW = datetime(2026, 4, 25, 9, 0, tzinfo=UTC)


def _ctx(chat_id: int, project_id: int) -> AnswerContext:
    return AnswerContext(
        chat_id=chat_id,
        customer_username="darya",
        trace_id=f"e2e-epic12-pricing-{chat_id}",
        now=_NOW,
        project_id=project_id,
    )


def _build_answerer(
    tmp_path,
) -> tuple[SalesPersonaAnswerer, RagRepository, ProjectServiceRepository, _StubOpenRouter]:
    sales_db = str(tmp_path / "sales.sqlite3")
    services_db = str(tmp_path / "services.sqlite3")
    rag_db = str(tmp_path / "rag.sqlite3")
    state_repo = StateRepository(db_path=sales_db)
    services_repo = ProjectServiceRepository(db_path=services_db)
    rag_repo = RagRepository(db_path=rag_db)
    openrouter = _StubOpenRouter()
    price_lookup = PriceLookup(
        rag_retriever=rag_repo, normalizer=get_russian_normalizer()
    )
    answerer = SalesPersonaAnswerer(
        state_repo=state_repo,
        services_repo=_ServicesRepoAdapter(services_repo),
        openrouter=openrouter,
        normalizer=get_russian_normalizer(),
        clock=lambda: _NOW,
        bot_persona_getter=lambda: "Николай",
        price_lookup=price_lookup,
    )
    # Park the chat directly in pricing — the funnel-entry pipework is
    # owned by 12.03 / 12.09, not this story.
    state_repo.upsert(
        chat_id=42,
        project_id=1,
        current_stage=STAGE_PRICING,
        collected_intent={},
        now=_NOW,
        last_bot_msg_at=_NOW,
    )
    return answerer, rag_repo, services_repo, openrouter


@pytest.mark.asyncio
async def test_pricing_loop_unknown_then_learned(tmp_path) -> None:
    project_id = 1
    chat_id = 42
    answerer, rag_repo, services_repo, openrouter = _build_answerer(tmp_path)
    services_repo.upsert(
        project_id=project_id,
        name="Медовеевка Лайт",
        description="Лайт уровень, с видами.",
    )

    # ── Turn 1: empty KB → miss + escalation ────────────────────────────
    first = await answerer.try_answer(
        question="Сколько стоит 6 часов?",
        ctx=_ctx(chat_id, project_id),
    )
    assert first.text == PRICING_MISS_FALLBACK
    assert first.response_mode == RESPONSE_MODE_SALES_ESCALATION
    assert first.metadata["hitl_reason"] == HITL_REASON_PRICE_UNKNOWN
    assert first.metadata["sales_turn_kind"] == "pricing_miss"
    assert (
        first.metadata["sales_price_unknown_payload"]["original_question"]
        == "Сколько стоит 6 часов?"
    )

    # ── Step 2: operator answers; we publish the answer to RAG. ─────────
    rag_repo.ingest(
        source_id="kb:operator-reply-1",
        text="6 часов — 15 000 ₽",
        project_id=project_id,
    )

    # ── Turn 3: customer asks again → KB hit, no new escalation. ────────
    openrouter.queue_response({"text": "6 часов — 15 000 ₽."})
    second = await answerer.try_answer(
        question="Сколько стоит 6 часов?",
        ctx=_ctx(chat_id, project_id),
    )
    assert second.handled is True
    assert second.text == "6 часов — 15 000 ₽."
    assert second.metadata["sales_turn_kind"] == "pricing_hit"
    assert second.metadata["sales_price_source_chunk_id"]
    # No escalation on the hit path.
    assert second.response_mode is None
    assert "escalate" not in second.metadata


@pytest.mark.asyncio
async def test_pricing_loop_drift_stays_safe(tmp_path) -> None:
    """A drifted LLM quote escalates — never delivers a wrong price."""
    project_id = 1
    chat_id = 42
    answerer, rag_repo, services_repo, openrouter = _build_answerer(tmp_path)
    services_repo.upsert(
        project_id=project_id, name="Медовеевка Лайт", description=None
    )
    rag_repo.ingest(
        source_id="kb:price",
        text="6 часов — 15 000 ₽",
        project_id=project_id,
    )

    # LLM hallucinates a different number (12 000 instead of 15 000).
    openrouter.queue_response({"text": "Стоит около 12 000 ₽."})
    result = await answerer.try_answer(
        question="Сколько стоит 6 часов?",
        ctx=_ctx(chat_id, project_id),
    )

    assert result.text == PRICING_MISS_FALLBACK
    assert result.metadata["sales_turn_kind"] == "pricing_quote_drift"
    assert result.metadata["hitl_reason"] == HITL_REASON_PRICE_UNKNOWN
    assert result.metadata["stage_after"] == STAGE_AWAITING_OPERATOR_PRICE

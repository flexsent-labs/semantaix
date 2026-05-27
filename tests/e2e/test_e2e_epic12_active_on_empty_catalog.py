"""Epic 12, Story 12.09 — always-on activation on empty catalog (E2E).

Sales is always-on. A freshly-bootstrapped project with zero ``services``
rows must still:

  1. Greet + scope a sales-intent message (NOT silently fall through to
     RAG/HITL). The answer trace shows ``sales_persona`` handled the
     turn.
  2. When the customer then asks the catalog ("Что у вас есть?"), reply
     with the fixed Russian line "Услуг пока нет. Уточню у коллег и
     сразу сообщу." AND escalate (sales_escalation +
     ``hitl_reason='catalog_empty'``).

The activation gate must NOT call ``services_repo.count_active`` — the
catalog count is a routing detail for the catalog handler, not the gate.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import pytest

from services.api.app.answerers import AnswerContext, AnswerPipeline
from services.api.app.russian_text import get_russian_normalizer
from services.api.app.sales.sales_persona_answerer import (
    EMPTY_CATALOG_ESCALATION_LINE,
    HITL_REASON_EMPTY_CATALOG,
    RESPONSE_MODE_SALES_ESCALATION,
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


_NOW = datetime(2026, 5, 1, 13, 33, tzinfo=UTC)
_CHAT_ID = 33
_PROJECT_ID = 1


@dataclass
class _ServiceShim:
    name: str
    description: str | None


class _ServicesRepoSpy:
    """Wrap a real services repo; record every ``count_active`` call so
    the test can enforce the always-on invariant."""

    def __init__(self, repo: ServicesRepository) -> None:
        self._repo = repo
        self.count_active_calls = 0

    def count_active(self, *, project_id: int) -> int:
        self.count_active_calls += 1
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
    state_repo = StateRepository(db_path=sales_db)
    services_repo = ServicesRepository(db_path=sales_db)
    services_spy = _ServicesRepoSpy(services_repo)
    openrouter = _StubOpenRouter()
    answerer = SalesPersonaAnswerer(
        state_repo=state_repo,
        services_repo=services_spy,
        openrouter=openrouter,
        normalizer=get_russian_normalizer(),
        clock=lambda: _NOW,
        bot_persona_getter=lambda: "Анна Иванова",
    )
    pipeline = AnswerPipeline([answerer])
    return {
        "pipeline": pipeline,
        "state_repo": state_repo,
        "services_repo": services_repo,
        "services_spy": services_spy,
        "openrouter": openrouter,
    }


@pytest.mark.asyncio
async def test_zero_services_sales_intent_engages_greeting(env) -> None:
    pipeline: AnswerPipeline = env["pipeline"]
    openrouter: _StubOpenRouter = env["openrouter"]
    services_spy: _ServicesRepoSpy = env["services_spy"]
    state_repo: StateRepository = env["state_repo"]

    openrouter.queue_response(
        {
            "extracted_fields": {"dates": "1 мая"},
            "next_question": "Здравствуйте! На сколько человек?",
        }
    )

    result = await pipeline.run(
        question="интересует тур на квадроциклах 1 мая",
        ctx=_ctx("empty-cat-greet"),
    )

    assert result.handled is True
    assert result.metadata["answerer"] == "sales_persona"
    assert result.metadata["stage_after"] == STAGE_SCOPING
    # Always-on invariant: the gate must NOT inspect the catalog count.
    assert services_spy.count_active_calls == 0
    # State row exists so subsequent turns route through sales again.
    state = state_repo.get(_CHAT_ID)
    assert state is not None
    assert state["current_stage"] == STAGE_SCOPING


@pytest.mark.asyncio
async def test_zero_services_catalog_ask_returns_fixed_line_and_escalates(
    env,
) -> None:
    """The catalog turn for an empty project replies with the fixed line
    'Услуг пока нет. Уточню у коллег и сразу сообщу.' AND opens a sales
    escalation so the operator picks up.

    Pre-condition: chat is already mid-funnel (state row exists). The
    catalog aside intercept runs before the stage handler.
    """
    pipeline: AnswerPipeline = env["pipeline"]
    openrouter: _StubOpenRouter = env["openrouter"]
    state_repo: StateRepository = env["state_repo"]

    # Seed scoping state so the aside intercept fires for the catalog ask.
    state_repo.upsert(
        chat_id=_CHAT_ID,
        project_id=_PROJECT_ID,
        current_stage=STAGE_SCOPING,
        collected_intent={},
        now=_NOW,
        last_bot_msg_at=_NOW,
    )

    result = await pipeline.run(
        question="Что у вас есть?", ctx=_ctx("empty-cat-ask")
    )

    assert result.handled is True
    assert result.text == EMPTY_CATALOG_ESCALATION_LINE
    assert result.response_mode == RESPONSE_MODE_SALES_ESCALATION
    assert result.metadata["sales_turn_kind"] == "catalog_empty"
    assert result.metadata["hitl_reason"] == HITL_REASON_EMPTY_CATALOG
    assert result.metadata["escalate"] is True
    # No LLM in the empty-catalog branch — the line is a fixed Russian
    # operator-authored copy.
    assert openrouter.calls == []

"""Integration: SalesPersonaAnswerer + real StateRepository round-trip.

Two calls in sequence against the same `StateRepository` (tmp DB): the
first call greets and persists, the second call resumes from the
persisted state and asks the next scoping question.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from services.api.app.answerers import AnswerContext
from services.api.app.russian_text import get_russian_normalizer
from services.api.app.sales.intent import Intent
from services.api.app.sales.sales_persona_answerer import SalesPersonaAnswerer
from services.api.app.sales.state_repository import StateRepository


class _FakeServicesRepo:
    def count_active(self, *, project_id: int) -> int:  # pragma: no cover
        return 0


class _ScriptedOpenRouter:
    def __init__(self, payloads: list[dict[str, Any]]) -> None:
        self.payloads = list(payloads)
        self.calls: list[dict[str, Any]] = []

    async def complete_json(
        self, *, system: str, user: str, model: str | None = None
    ) -> dict[str, Any]:
        self.calls.append({"system": system, "user": user, "model": model})
        return self.payloads.pop(0)


_FIXED_NOW = datetime(2026, 5, 1, 13, 33, tzinfo=UTC)


def _clock() -> datetime:
    return _FIXED_NOW


def _ctx(chat_id: int = 7) -> AnswerContext:
    return AnswerContext(
        chat_id=chat_id,
        customer_username="darya",
        trace_id="trace-int",
        now=_FIXED_NOW,
        project_id=1,
    )


@pytest.mark.asyncio
async def test_round_trip_first_greets_second_resumes(tmp_path: Path) -> None:
    db = tmp_path / "sales.sqlite3"
    state_repo = StateRepository(db_path=str(db))
    openrouter = _ScriptedOpenRouter(
        [
            {
                "extracted_fields": {"dates": "1 мая"},
                "next_question": "Сколько человек?",
            },
            {
                "extracted_fields": {"headcount": 6},
                "next_question": "Сколько квадроциклов?",
            },
        ]
    )
    answerer = SalesPersonaAnswerer(
        state_repo=state_repo,
        services_repo=_FakeServicesRepo(),
        openrouter=openrouter,
        normalizer=get_russian_normalizer(),
        clock=_clock,
        bot_persona_getter=lambda: "Николай",
    )

    # Turn 1 — greeting.
    first = await answerer.try_answer(
        question="1 мая хочу тур на квадроциклах", ctx=_ctx()
    )
    assert first.handled is True
    state = state_repo.get(7)
    assert state is not None
    assert state["current_stage"] == "scoping"
    assert state["collected_intent"] == Intent(dates="1 мая").to_dict()

    # Turn 2 — resume from persisted state.
    second = await answerer.try_answer(question="нас 6", ctx=_ctx())
    assert second.handled is True
    state = state_repo.get(7)
    assert state is not None
    assert state["collected_intent"] == Intent(
        dates="1 мая", headcount=6
    ).to_dict()
    # Still in scoping — three more fields to collect.
    assert state["current_stage"] == "scoping"


@pytest.mark.asyncio
async def test_round_trip_state_persists_across_repository_instances(
    tmp_path: Path,
) -> None:
    db = tmp_path / "sales.sqlite3"
    repo_a = StateRepository(db_path=str(db))
    openrouter = _ScriptedOpenRouter(
        [{"extracted_fields": {}, "next_question": "Какие даты?"}]
    )
    answerer = SalesPersonaAnswerer(
        state_repo=repo_a,
        services_repo=_FakeServicesRepo(),
        openrouter=openrouter,
        normalizer=get_russian_normalizer(),
        clock=_clock,
        bot_persona_getter=lambda: "Николай",
    )
    await answerer.try_answer(question="Хочу тур", ctx=_ctx())

    # Open a fresh repo against the same DB path and confirm the row survives.
    repo_b = StateRepository(db_path=str(db))
    state = repo_b.get(7)
    assert state is not None
    assert state["current_stage"] == "scoping"

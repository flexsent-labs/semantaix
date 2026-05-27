"""Story 12.09 — always-on activation regression.

Sales is always-on. A project with zero ``services`` rows must still
engage the SalesPersonaAnswerer when the inbound text matches the
Russian sales-intent regex. The gate must NOT call
``services_repo.count_active``.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest

from services.api.app.answerers import AnswerContext, AnswerPipeline
from services.api.app.russian_text import get_russian_normalizer
from services.api.app.sales.sales_persona_answerer import SalesPersonaAnswerer


class _FakeStateRepo:
    def __init__(self) -> None:
        self.rows: dict[int, dict[str, Any]] = {}

    def get(self, chat_id: int):
        return self.rows.get(chat_id)

    def upsert(self, **kwargs: Any) -> None:
        chat_id = int(kwargs["chat_id"])
        self.rows[chat_id] = dict(kwargs)


class _FakeServicesRepo:
    def __init__(self) -> None:
        self.count_active_calls = 0
        self.list_for_project_calls = 0

    def count_active(self, *, project_id: int) -> int:
        self.count_active_calls += 1
        return 0

    def list_for_project(self, *, project_id: int):
        self.list_for_project_calls += 1
        return []

    def get_by_name(self, *, project_id: int, name: str):
        return None


class _FakeOpenRouter:
    def __init__(self, payload: dict[str, Any]) -> None:
        self.payload = payload
        self.calls: list[dict[str, Any]] = []

    async def complete_json(
        self, *, system: str, user: str, model: str | None = None
    ) -> dict[str, Any]:
        self.calls.append({"system": system, "user": user, "model": model})
        return self.payload


def _ctx() -> AnswerContext:
    return AnswerContext(
        chat_id=7,
        customer_username="@danil",
        trace_id="trace-empty-catalog",
        now=datetime(2026, 5, 1, 13, 33, tzinfo=UTC),
        project_id=1,
    )


@pytest.mark.asyncio
async def test_empty_services_still_engages_sales_on_sales_intent() -> None:
    state_repo = _FakeStateRepo()
    services_repo = _FakeServicesRepo()
    openrouter = _FakeOpenRouter(
        {
            "extracted_fields": {},
            "next_question": "Здравствуйте! На какие даты?",
        }
    )
    sales = SalesPersonaAnswerer(
        state_repo=state_repo,
        services_repo=services_repo,
        openrouter=openrouter,
        normalizer=get_russian_normalizer(),
        clock=lambda: datetime(2026, 5, 1, 13, 33, tzinfo=UTC),
        bot_persona_getter=lambda: "Анна Иванова",
    )

    pipeline = AnswerPipeline([sales])
    result = await pipeline.run(
        question="интересует тур на квадроциклах 1 мая", ctx=_ctx()
    )

    assert result.handled is True
    assert result.metadata.get("answerer") == "sales_persona"
    # The always-on invariant: gate must NOT consult the catalog count.
    assert services_repo.count_active_calls == 0

"""Story 12.09 — silent-on-non-intent regression.

Without an existing conversation state and without a sales-intent
match, the sales answerer must skip silently: no LLM call, no DB
write, no services-catalog lookup. This bounds dormant cost: every
non-sales inbound message costs one cheap regex match.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest

from services.api.app.answerers import AnswerContext, AnswerPipeline
from services.api.app.russian_text import get_russian_normalizer
from services.api.app.sales.sales_persona_answerer import SalesPersonaAnswerer


class _RecordingStateRepo:
    def __init__(self) -> None:
        self.get_calls: list[int] = []
        self.upsert_calls: list[dict[str, Any]] = []

    def get(self, chat_id: int):
        self.get_calls.append(int(chat_id))
        return None

    def upsert(self, **kwargs: Any) -> None:  # pragma: no cover - must not run
        self.upsert_calls.append(dict(kwargs))


class _RecordingServicesRepo:
    def __init__(self) -> None:
        self.count_active_calls = 0
        self.list_for_project_calls = 0

    def count_active(self, *, project_id: int) -> int:  # pragma: no cover
        self.count_active_calls += 1
        return 0

    def list_for_project(self, *, project_id: int):  # pragma: no cover
        self.list_for_project_calls += 1
        return []

    def get_by_name(self, *, project_id: int, name: str):  # pragma: no cover
        return None


class _MustNotCallOpenRouter:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def complete_json(
        self, *, system: str, user: str, model: str | None = None
    ) -> dict[str, Any]:  # pragma: no cover - must not run
        self.calls.append({"system": system, "user": user, "model": model})
        raise AssertionError("LLM must not be called on a non-sales message")


def _ctx() -> AnswerContext:
    return AnswerContext(
        chat_id=42,
        customer_username="@stranger",
        trace_id="trace-non-intent",
        now=datetime(2026, 5, 1, 13, 33, tzinfo=UTC),
        project_id=1,
    )


@pytest.mark.asyncio
async def test_non_sales_message_skips_silently() -> None:
    state_repo = _RecordingStateRepo()
    services_repo = _RecordingServicesRepo()
    openrouter = _MustNotCallOpenRouter()
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
        question="Какая сегодня погода в Москве?", ctx=_ctx()
    )

    assert result.handled is False
    # State row read is allowed (it's the cheap gate) but no LLM call,
    # no upsert, and no catalog lookup may happen.
    assert openrouter.calls == []
    assert state_repo.upsert_calls == []
    assert services_repo.count_active_calls == 0
    assert services_repo.list_for_project_calls == 0

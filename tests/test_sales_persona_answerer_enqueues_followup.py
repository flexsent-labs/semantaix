"""SalesPersonaAnswerer enqueues a +1d follow-up after every handled turn."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from services.api.app.answerers import AnswerContext
from services.api.app.russian_text import get_russian_normalizer
from services.api.app.sales.followup_queue_repository import (
    STATUS_CANCELLED_REPLACED,
    STATUS_SCHEDULED,
    FollowupQueueRepository,
)
from services.api.app.sales.sales_persona_answerer import (
    FOLLOWUP_DELAY,
    SalesPersonaAnswerer,
)
from services.api.app.sales.state_repository import StateRepository


class _FakeServicesRepo:
    def count_active(self, *, project_id: int) -> int:  # pragma: no cover
        return 0


class _ScriptedOpenRouter:
    def __init__(self, payloads: list[dict[str, Any]]) -> None:
        self.payloads = list(payloads)

    async def complete_json(
        self, *, system: str, user: str, model: str | None = None
    ) -> dict[str, Any]:
        return self.payloads.pop(0)


_NOW = datetime(2026, 5, 26, 13, 0, tzinfo=UTC)


def _ctx(chat_id: int = 7) -> AnswerContext:
    return AnswerContext(
        chat_id=chat_id,
        customer_username="darya",
        trace_id="trace-fu",
        now=_NOW,
        project_id=11,
    )


def _build(tmp_path: Path, payloads: list[dict[str, Any]]) -> tuple[
    SalesPersonaAnswerer, FollowupQueueRepository
]:
    state_repo = StateRepository(db_path=str(tmp_path / "sales_state.db"))
    followup_repo = FollowupQueueRepository(
        db_path=str(tmp_path / "sales_followup.db")
    )
    answerer = SalesPersonaAnswerer(
        state_repo=state_repo,
        services_repo=_FakeServicesRepo(),
        openrouter=_ScriptedOpenRouter(payloads),
        normalizer=get_russian_normalizer(),
        clock=lambda: _NOW,
        bot_persona_getter=lambda: "Николай",
        followup_repo=followup_repo,
    )
    return answerer, followup_repo


@pytest.mark.asyncio
async def test_handled_turn_enqueues_followup_at_plus_24h(
    tmp_path: Path,
) -> None:
    answerer, followup_repo = _build(
        tmp_path,
        [
            {
                "extracted_fields": {"dates": "1 мая"},
                "next_question": "Сколько человек?",
            }
        ],
    )

    result = await answerer.try_answer(question="хочу тур", ctx=_ctx())
    assert result.handled is True

    rows = followup_repo.list_for_chat(7)
    assert len(rows) == 1
    row = rows[0]
    assert row.status == STATUS_SCHEDULED
    assert row.project_id == 11
    assert row.fire_at == _NOW + FOLLOWUP_DELAY
    # The follow-up delay is exactly 24h per the story.
    assert FOLLOWUP_DELAY == timedelta(hours=24)


@pytest.mark.asyncio
async def test_second_handled_turn_replaces_prior_scheduled_row(
    tmp_path: Path,
) -> None:
    answerer, followup_repo = _build(
        tmp_path,
        [
            {
                "extracted_fields": {"dates": "1 мая"},
                "next_question": "Сколько человек?",
            },
            {
                "extracted_fields": {"headcount": 6},
                "next_question": "Сколько квадроциклов?",
            },
        ],
    )

    await answerer.try_answer(question="хочу тур", ctx=_ctx())
    await answerer.try_answer(question="нас шесть", ctx=_ctx())

    rows = followup_repo.list_for_chat(7)
    statuses = sorted(row.status for row in rows)
    assert statuses == [STATUS_CANCELLED_REPLACED, STATUS_SCHEDULED]
    scheduled = next(r for r in rows if r.status == STATUS_SCHEDULED)
    assert scheduled.fire_at == _NOW + FOLLOWUP_DELAY


@pytest.mark.asyncio
async def test_skip_does_not_enqueue_followup(tmp_path: Path) -> None:
    answerer, followup_repo = _build(tmp_path, [])
    result = await answerer.try_answer(
        question="какая погода?", ctx=_ctx()
    )
    assert result.handled is False
    assert followup_repo.list_for_chat(7) == []


class _ExplodingFollowupRepo:
    """Stub repo whose ``enqueue`` blows up; turn must still return handled."""

    def __init__(self) -> None:
        self.calls = 0

    def enqueue(self, **_kwargs: Any) -> int:
        self.calls += 1
        raise RuntimeError("disk full")


@pytest.mark.asyncio
async def test_followup_enqueue_failure_is_swallowed(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    state_repo = StateRepository(db_path=str(tmp_path / "sales_state.db"))
    exploding_repo = _ExplodingFollowupRepo()
    answerer = SalesPersonaAnswerer(
        state_repo=state_repo,
        services_repo=_FakeServicesRepo(),
        openrouter=_ScriptedOpenRouter(
            [
                {
                    "extracted_fields": {"dates": "1 мая"},
                    "next_question": "Сколько человек?",
                }
            ]
        ),
        normalizer=get_russian_normalizer(),
        clock=lambda: _NOW,
        bot_persona_getter=lambda: "Николай",
        followup_repo=exploding_repo,
    )

    with caplog.at_level("WARNING"):
        result = await answerer.try_answer(
            question="хочу тур", ctx=_ctx()
        )

    assert result.handled is True
    assert exploding_repo.calls == 1
    assert any(
        "sales_followup_enqueue_failed" in record.message
        for record in caplog.records
    )

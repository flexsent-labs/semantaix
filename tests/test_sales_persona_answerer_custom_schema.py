"""Story 12.15 — a per-service schema drives the scoping anketa end-to-end.

With the consultation schema wired, the bot collects ``topic`` / ``contact``
(custom fields living in ``Intent.extra``) and never asks about headcount or
vehicles — the "1 person" domain.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest

from services.api.app.answerers import AnswerContext
from services.api.app.russian_text import get_russian_normalizer
from services.api.app.sales.intent import Intent
from services.api.app.sales.sales_persona_answerer import (
    SCOPING_COMPLETE_HANDOFF_LINE,
    SalesPersonaAnswerer,
)
from services.api.app.sales.scoping_schema import CONSULTATION_SCHEMA

_NOW = datetime(2026, 5, 30, 9, 0, tzinfo=UTC)
_CHAT_ID = 7
_PROJECT_ID = 1


class _FakeStateRepo:
    def __init__(self, *, initial: dict[str, Any]) -> None:
        self._state = initial
        self.upserts: list[dict[str, Any]] = []

    def get(self, chat_id: int) -> dict[str, Any] | None:
        return self._state

    def upsert(self, **kwargs: Any) -> None:
        self.upserts.append(kwargs)


class _NoOpServicesRepo:
    def count_active(self, *, project_id: int) -> int:  # pragma: no cover
        return 0

    def list_for_project(self, *, project_id: int) -> list[Any]:  # pragma: no cover
        return []

    def get_by_name(self, *, project_id, name):  # pragma: no cover
        return None


class _RecordingOpenRouter:
    def __init__(self, *responses: dict[str, Any]) -> None:
        self._responses = list(responses)
        self.systems: list[str] = []

    async def complete_json(self, *, system, user, model=None) -> dict[str, Any]:
        self.systems.append(system)
        return self._responses.pop(0)


def _ctx() -> AnswerContext:
    return AnswerContext(
        chat_id=_CHAT_ID, customer_username="@a", trace_id="t",
        now=_NOW, project_id=_PROJECT_ID,
    )


def _state(intent: Intent) -> dict[str, Any]:
    return {
        "chat_id": _CHAT_ID, "project_id": _PROJECT_ID,
        "current_stage": "scoping",
        "collected_intent": intent.to_dict(), "last_proposal": None,
    }


def _build(intent: Intent, openrouter: Any) -> tuple[SalesPersonaAnswerer, _FakeStateRepo]:
    repo = _FakeStateRepo(initial=_state(intent))
    answerer = SalesPersonaAnswerer(
        state_repo=repo,
        services_repo=_NoOpServicesRepo(),
        openrouter=openrouter,
        normalizer=get_russian_normalizer(),
        clock=lambda: _NOW,
        bot_persona_getter=lambda: "Анна",
        calendar_settings_repo=None,
        calendar_token_provider=None,
        calendar_freebusy_client=None,
        operator_chat_resolver=lambda op: 42,
        scoping_schema_getter=lambda ctx: CONSULTATION_SCHEMA,
    )
    return answerer, repo


@pytest.mark.asyncio
async def test_consultation_collects_custom_field_and_skips_headcount() -> None:
    intent = Intent.from_dict({"dates": "завтра в 15:00"})  # date already known
    openrouter = _RecordingOpenRouter(
        {"extracted_fields": {"topic": "ипотека"}, "next_question": "Оставьте телефон?"}
    )
    answerer, repo = _build(intent, openrouter)

    result = await answerer.try_answer(
        question="хочу обсудить ипотеку", ctx=_ctx()
    )

    # The custom field round-trips into state.
    assert repo.upserts[-1]["collected_intent"]["topic"] == "ипотека"
    # The prompt offered the consultation anketa, never the transfer fields.
    system = openrouter.systems[0]
    assert "topic" in system and "contact" in system
    assert "headcount" not in system and "vehicle_count" not in system
    # contact still missing → stays in scoping.
    assert result.metadata["stage_after"] == "scoping"


@pytest.mark.asyncio
async def test_consultation_completes_when_all_custom_fields_filled() -> None:
    intent = Intent.from_dict({"dates": "завтра в 15:00", "topic": "ипотека"})
    openrouter = _RecordingOpenRouter(
        {"extracted_fields": {"contact": "+79001234567"}, "next_question": "?"}
    )
    answerer, repo = _build(intent, openrouter)

    result = await answerer.try_answer(question="+79001234567", ctx=_ctx())

    assert result.text == SCOPING_COMPLETE_HANDOFF_LINE
    # The operator booking summary carries the custom fields.
    summary = result.metadata["escalation_context"]
    assert "topic=ипотека" in summary
    assert "contact=+79001234567" in summary

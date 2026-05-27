"""Catalog-ask aside tests for SalesPersonaAnswerer (Story 12.06 + 12.09).

A mid-funnel "что у вас есть?" / "какие туры?" must list the active service
names (operator-authored, verbatim) and preserve the customer's funnel state
across the aside. Story 12.09 extends the empty-catalog branch: instead of
silently skipping, the bot speaks ``EMPTY_CATALOG_ESCALATION_LINE`` and
escalates with ``hitl_reason='catalog_empty'`` — the always-on activation
invariant means an empty-catalog project still gives an honest reply.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import pytest

from services.api.app.answerers import AnswerContext
from services.api.app.russian_text import get_russian_normalizer
from services.api.app.sales.intent import Intent
from services.api.app.sales.sales_persona_answerer import (
    EMPTY_CATALOG_ESCALATION_LINE,
    HITL_REASON_EMPTY_CATALOG,
    RESPONSE_MODE_SALES_ESCALATION,
    SalesPersonaAnswerer,
)


class _FakeStateRepo:
    def __init__(self) -> None:
        self.rows: dict[int, dict[str, Any]] = {}
        self.upsert_calls: list[dict[str, Any]] = []

    def get(self, chat_id: int):
        return self.rows.get(chat_id)

    def upsert(self, **kwargs: Any) -> None:
        self.upsert_calls.append(kwargs)
        chat_id = int(kwargs["chat_id"])
        self.rows[chat_id] = dict(kwargs)


@dataclass(frozen=True)
class _FakeService:
    name: str
    description: str | None = None


class _FakeServicesRepo:
    def __init__(self, services: list[_FakeService] | None = None) -> None:
        self._services = list(services) if services is not None else []

    def count_active(self, *, project_id: int) -> int:
        return len(self._services)

    def list_for_project(self, *, project_id: int) -> list[_FakeService]:
        return list(self._services)

    def get_by_name(
        self, *, project_id: int, name: str
    ) -> _FakeService | None:
        target = name.strip().casefold()
        for service in self._services:
            if service.name.strip().casefold() == target:
                return service
        return None


class _FakeOpenRouter:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self.queue: list[dict[str, Any]] = []

    def queue_response(self, payload: dict[str, Any]) -> None:
        self.queue.append(payload)

    async def complete_json(
        self, *, system: str, user: str, model: str | None = None
    ) -> dict[str, Any]:
        self.calls.append({"system": system, "user": user, "model": model})
        if not self.queue:
            raise AssertionError("LLM called without a queued payload")
        return self.queue.pop(0)


_FIXED_NOW = datetime(2026, 5, 1, 13, 33, tzinfo=UTC)


def _clock() -> datetime:
    return _FIXED_NOW


def _ctx(project_id: int = 1) -> AnswerContext:
    return AnswerContext(
        chat_id=7,
        customer_username="darya",
        trace_id="trace-catalog",
        now=_FIXED_NOW,
        project_id=project_id,
    )


def _seed_scoping_state(
    state_repo: _FakeStateRepo,
    *,
    chat_id: int = 7,
    project_id: int = 1,
    intent: Intent | None = None,
) -> None:
    state_repo.rows[chat_id] = {
        "chat_id": chat_id,
        "project_id": project_id,
        "current_stage": "scoping",
        "collected_intent": (intent or Intent(dates="1 мая")).to_dict(),
        "last_proposal": None,
        "last_customer_msg_at": None,
        "last_bot_msg_at": None,
    }


def _build(
    services: list[_FakeService] | None = None,
) -> tuple[SalesPersonaAnswerer, _FakeStateRepo, _FakeOpenRouter, _FakeServicesRepo]:
    state_repo = _FakeStateRepo()
    openrouter = _FakeOpenRouter()
    services_repo = _FakeServicesRepo(services=services)
    answerer = SalesPersonaAnswerer(
        state_repo=state_repo,
        services_repo=services_repo,
        openrouter=openrouter,
        normalizer=get_russian_normalizer(),
        clock=_clock,
        bot_persona_getter=lambda: "Николай",
    )
    return answerer, state_repo, openrouter, services_repo


@pytest.mark.asyncio
async def test_catalog_ask_lists_active_service_names() -> None:
    services = [
        _FakeService(name="Медовеевка Лайт"),
        _FakeService(name="Ивановский водопад"),
        _FakeService(name="Каньонинг"),
    ]
    answerer, state_repo, openrouter, _ = _build(services=services)
    _seed_scoping_state(state_repo)

    result = await answerer.try_answer(
        question="Что у вас есть?", ctx=_ctx()
    )

    assert result.handled is True
    assert result.text is not None
    for service in services:
        assert service.name in result.text, (
            f"missing service name {service.name} in reply: {result.text!r}"
        )
    # The catalog reply is operator-authored data, not LLM-generated.
    assert openrouter.calls == [], "LLM must not be called for catalog asks"
    # Metadata records the turn kind for downstream auditing.
    assert result.metadata.get("sales_turn_kind") == "catalog"


@pytest.mark.asyncio
async def test_catalog_ask_preserves_funnel_state() -> None:
    services = [_FakeService(name="Каньонинг")]
    answerer, state_repo, _, _ = _build(services=services)
    seeded_intent = Intent(dates="1 мая", headcount=6)
    _seed_scoping_state(state_repo, intent=seeded_intent)

    await answerer.try_answer(question="Что у вас есть?", ctx=_ctx())

    # The catalog aside must NOT change `current_stage` or `collected_intent`.
    state = state_repo.rows[7]
    assert state["current_stage"] == "scoping"
    assert state["collected_intent"] == seeded_intent.to_dict()


@pytest.mark.asyncio
async def test_catalog_ask_empty_services_escalates_with_fixed_line() -> None:
    """Story 12.09: empty catalog → fixed Russian line + sales escalation.

    The bot must give an honest "пока нет" answer (never the default ack)
    and signal a HITL ticket via the metadata so the operator can fill the
    catalog before the next customer.
    """
    answerer, state_repo, openrouter, _ = _build(services=[])
    _seed_scoping_state(state_repo)

    result = await answerer.try_answer(
        question="Что у вас есть?", ctx=_ctx()
    )

    assert result.handled is True
    assert result.text == EMPTY_CATALOG_ESCALATION_LINE
    assert result.response_mode == RESPONSE_MODE_SALES_ESCALATION
    assert result.metadata.get("sales_turn_kind") == "catalog_empty"
    assert result.metadata.get("hitl_reason") == HITL_REASON_EMPTY_CATALOG
    assert result.metadata.get("escalate") is True
    # No LLM call — the line is a fixed, operator-authored Russian copy.
    assert openrouter.calls == []


@pytest.mark.asyncio
async def test_catalog_ask_does_not_persist_when_state_unchanged() -> None:
    services = [_FakeService(name="Каньонинг")]
    answerer, state_repo, _, _ = _build(services=services)
    _seed_scoping_state(state_repo)
    upserts_before = len(state_repo.upsert_calls)

    await answerer.try_answer(question="Что у вас есть?", ctx=_ctx())

    # No upsert needed — the state row was untouched.
    assert len(state_repo.upsert_calls) == upserts_before


@pytest.mark.asyncio
async def test_catalog_ask_during_pitching_still_handled() -> None:
    """A pitching/pricing customer can still ask "что у вас есть?" as an aside."""
    services = [_FakeService(name="Каньонинг")]
    answerer, state_repo, _, _ = _build(services=services)
    state_repo.rows[7] = {
        "chat_id": 7,
        "project_id": 1,
        "current_stage": "pitching",
        "collected_intent": Intent(
            dates="1 мая",
            headcount=6,
            vehicle_count=3,
            difficulty="средний",
            drivers="мужчины",
        ).to_dict(),
        "last_proposal": None,
        "last_customer_msg_at": None,
        "last_bot_msg_at": None,
    }

    result = await answerer.try_answer(
        question="А что у вас вообще есть?", ctx=_ctx()
    )

    assert result.handled is True
    assert "Каньонинг" in (result.text or "")
    # Pitching state is preserved across the aside.
    assert state_repo.rows[7]["current_stage"] == "pitching"

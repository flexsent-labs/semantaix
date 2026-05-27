"""Activation-gate tests for SalesPersonaAnswerer (Story 12.03).

The gate is the cheapest path: existing non-dormant state → enter; otherwise
run the intent regex on lemmatized text. Sales is **always-on** — an empty
`services` catalog never gates the answerer (the bot can still scope, look up
prices via RAG, propose dates).
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

import pytest

from services.api.app.answerers import AnswerContext
from services.api.app.russian_text import get_russian_normalizer
from services.api.app.sales.intent import Intent
from services.api.app.sales.sales_persona_answerer import SalesPersonaAnswerer


class _FakeStateRepo:
    def __init__(self) -> None:
        self.rows: dict[int, dict[str, Any]] = {}
        self.upsert_calls: list[dict[str, Any]] = []

    def get(self, chat_id: int):
        return self.rows.get(chat_id)

    def upsert(self, **kwargs: Any) -> None:
        self.upsert_calls.append(kwargs)
        # Mirror what a real repo persists, in dict form for inspection.
        chat_id = int(kwargs["chat_id"])
        self.rows[chat_id] = dict(kwargs)


class _FakeServicesRepo:
    def __init__(self, *, count: int = 0) -> None:
        self._count = count
        self.calls: int = 0

    def count_active(self, *, project_id: int) -> int:  # pragma: no cover
        # The gate must NOT call this — sales is always-on. We track calls so
        # the test can assert it stays at 0.
        self.calls += 1
        return self._count


class _FakeOpenRouter:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self.next_payload: dict[str, Any] | None = None

    async def complete_json(
        self, *, system: str, user: str, model: str | None = None
    ) -> dict[str, Any]:
        self.calls.append({"system": system, "user": user, "model": model})
        if self.next_payload is None:
            raise AssertionError("LLM called without a queued payload")
        return self.next_payload


def _fixed_clock() -> datetime:
    return datetime(2026, 5, 1, 13, 33, tzinfo=UTC)


def _ctx(*, chat_id: int = 7, project_id: int = 1) -> AnswerContext:
    return AnswerContext(
        chat_id=chat_id,
        customer_username="darya",
        trace_id="trace-001",
        now=_fixed_clock(),
        language="ru",
        country_code="RU",
        timezone="Europe/Moscow",
        location="Moscow",
        grounding_threshold=0.6,
        project_id=project_id,
    )


def _persona_getter() -> str:
    return "Николай"


def _build_answerer(
    *,
    state_repo: _FakeStateRepo | None = None,
    services_repo: _FakeServicesRepo | None = None,
    openrouter: _FakeOpenRouter | None = None,
) -> SalesPersonaAnswerer:
    return SalesPersonaAnswerer(
        state_repo=state_repo or _FakeStateRepo(),
        services_repo=services_repo or _FakeServicesRepo(count=0),
        openrouter=openrouter or _FakeOpenRouter(),
        normalizer=get_russian_normalizer(),
        clock=_fixed_clock,
        bot_persona_getter=_persona_getter,
    )


@pytest.mark.asyncio
async def test_skip_when_no_state_and_no_sales_intent() -> None:
    answerer = _build_answerer()
    result = await answerer.try_answer(
        question="Какая сегодня погода в Москве?", ctx=_ctx()
    )
    assert result.handled is False
    assert result.metadata.get("skip_reason") == "not_sales_intent"


@pytest.mark.asyncio
async def test_gate_enters_when_no_state_but_sales_intent_present() -> None:
    state_repo = _FakeStateRepo()
    services_repo = _FakeServicesRepo(count=0)  # empty catalog — must not gate
    openrouter = _FakeOpenRouter()
    openrouter.next_payload = {
        "extracted_fields": {},
        "next_question": "Здравствуйте! Какие даты вас интересуют?",
    }
    answerer = _build_answerer(
        state_repo=state_repo,
        services_repo=services_repo,
        openrouter=openrouter,
    )

    result = await answerer.try_answer(
        question="Здравствуйте, хочу тур на квадроциклах", ctx=_ctx()
    )

    assert result.handled is True
    # The gate must NOT consult the services catalog — sales is always-on.
    assert services_repo.calls == 0


@pytest.mark.asyncio
async def test_existing_non_dormant_state_resumes_regardless_of_intent() -> None:
    state_repo = _FakeStateRepo()
    state_repo.rows[7] = {
        "chat_id": 7,
        "project_id": 1,
        "current_stage": "scoping",
        "collected_intent": Intent(dates="1 мая").to_dict(),
        "last_proposal": None,
        "last_customer_msg_at": None,
        "last_bot_msg_at": None,
    }
    openrouter = _FakeOpenRouter()
    openrouter.next_payload = {
        "extracted_fields": {"headcount": 6},
        "next_question": "Сколько квадроциклов нужно?",
    }
    answerer = _build_answerer(state_repo=state_repo, openrouter=openrouter)

    # Even a "non-sales-looking" message resumes the existing state.
    result = await answerer.try_answer(question="нас будет 6", ctx=_ctx())

    assert result.handled is True


@pytest.mark.asyncio
async def test_dormant_state_skips_when_no_sales_intent() -> None:
    state_repo = _FakeStateRepo()
    state_repo.rows[7] = {
        "chat_id": 7,
        "project_id": 1,
        "current_stage": "dormant",
        "collected_intent": Intent().to_dict(),
        "last_proposal": None,
        "last_customer_msg_at": None,
        "last_bot_msg_at": None,
    }
    answerer = _build_answerer(state_repo=state_repo)

    result = await answerer.try_answer(
        question="Какая сегодня погода?", ctx=_ctx()
    )
    assert result.handled is False
    assert result.metadata.get("skip_reason") == "not_sales_intent"


@pytest.mark.asyncio
async def test_dormant_state_still_re_enters_on_sales_intent() -> None:
    state_repo = _FakeStateRepo()
    state_repo.rows[7] = {
        "chat_id": 7,
        "project_id": 1,
        "current_stage": "dormant",
        "collected_intent": Intent().to_dict(),
        "last_proposal": None,
        "last_customer_msg_at": None,
        "last_bot_msg_at": None,
    }
    openrouter = _FakeOpenRouter()
    openrouter.next_payload = {
        "extracted_fields": {},
        "next_question": "Здравствуйте! На какие даты планируете?",
    }
    answerer = _build_answerer(state_repo=state_repo, openrouter=openrouter)

    result = await answerer.try_answer(
        question="Хочу прокат квадроциклов", ctx=_ctx()
    )
    assert result.handled is True


@pytest.mark.asyncio
async def test_empty_catalog_is_a_valid_state_for_greeting() -> None:
    """Sales is enabled for every project; an empty `services` catalog must
    NOT gate the answerer."""
    state_repo = _FakeStateRepo()
    services_repo = _FakeServicesRepo(count=0)
    openrouter = _FakeOpenRouter()
    openrouter.next_payload = {
        "extracted_fields": {},
        "next_question": "Какие даты вас интересуют?",
    }
    answerer = _build_answerer(
        state_repo=state_repo,
        services_repo=services_repo,
        openrouter=openrouter,
    )

    result = await answerer.try_answer(
        question="Хочу записаться на тур", ctx=_ctx()
    )

    assert result.handled is True
    assert services_repo.calls == 0


@pytest.mark.asyncio
async def test_stage_pitching_skips_with_not_implemented_reason() -> None:
    state_repo = _FakeStateRepo()
    state_repo.rows[7] = {
        "chat_id": 7,
        "project_id": 1,
        "current_stage": "pitching",
        "collected_intent": Intent(
            dates="1 мая",
            headcount=6,
            vehicle_count=3,
            difficulty="средний",
            drivers="мужчины 30+",
        ).to_dict(),
        "last_proposal": None,
        "last_customer_msg_at": None,
        "last_bot_msg_at": None,
    }
    answerer = _build_answerer(state_repo=state_repo)

    result = await answerer.try_answer(question="ну что там?", ctx=_ctx())
    assert result.handled is False
    assert result.metadata.get("skip_reason") == "stage_not_implemented_yet"


@pytest.mark.asyncio
async def test_chat_id_none_skips() -> None:
    answerer = _build_answerer()
    ctx = AnswerContext(
        chat_id=None,
        customer_username=None,
        trace_id="t",
        now=_fixed_clock(),
    )
    result = await answerer.try_answer(question="Хочу тур", ctx=ctx)
    assert result.handled is False
    assert result.metadata.get("skip_reason") == "no_chat_id"


def test_answerer_exposes_protocol_name() -> None:
    assert SalesPersonaAnswerer.name == "sales_persona"


def test_intent_json_round_trip_through_state_payload() -> None:
    # State row payloads are JSON; the answerer stores Intent via to_dict.
    intent = Intent(dates="1 мая", headcount=6)
    encoded = json.dumps(intent.to_dict(), ensure_ascii=False, sort_keys=True)
    decoded = Intent.from_dict(json.loads(encoded))
    assert decoded == intent


@pytest.mark.asyncio
async def test_state_with_stage_new_resumes_greeting() -> None:
    """A row that was upserted before the bot replied (rare edge case) still
    routes through the greeting branch when re-entered."""
    state_repo = _FakeStateRepo()
    state_repo.rows[7] = {
        "chat_id": 7,
        "project_id": 1,
        "current_stage": "new",
        "collected_intent": Intent().to_dict(),
        "last_proposal": None,
        "last_customer_msg_at": None,
        "last_bot_msg_at": None,
    }
    openrouter = _FakeOpenRouter()
    openrouter.next_payload = {
        "extracted_fields": {},
        "next_question": "Здравствуйте! Какие даты?",
    }
    answerer = _build_answerer(state_repo=state_repo, openrouter=openrouter)
    result = await answerer.try_answer(question="да", ctx=_ctx())
    assert result.handled is True
    assert result.metadata.get("stage_before") == "new"
    assert result.metadata.get("stage_after") == "scoping"


@pytest.mark.asyncio
async def test_unknown_stage_falls_through_as_not_implemented() -> None:
    """Defensive: a malformed/unknown stage value should fall through cleanly
    so the message can still reach the downstream answerers."""
    state_repo = _FakeStateRepo()
    state_repo.rows[7] = {
        "chat_id": 7,
        "project_id": 1,
        "current_stage": "completely-unknown-stage",
        "collected_intent": Intent().to_dict(),
        "last_proposal": None,
        "last_customer_msg_at": None,
        "last_bot_msg_at": None,
    }
    answerer = _build_answerer(state_repo=state_repo)
    result = await answerer.try_answer(question="anything", ctx=_ctx())
    assert result.handled is False
    assert result.metadata.get("skip_reason") == "stage_not_implemented_yet"

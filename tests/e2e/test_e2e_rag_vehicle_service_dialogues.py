"""RAG-backed vehicle conversations from discovery through operator handoff.

The fixture below is a small, verbatim snapshot of the approved Kozlotur
brochure chunks currently indexed as ``knowledge_candidate:5`` and
``knowledge_candidate:6``.  The test deliberately uses the real
``RagRepository`` and ``PriceLookup`` instead of a fake service catalog: it
first proves that the offerings are retrievable, then drives the customer
conversation through the HTTP endpoint.

The booking cases also reproduce the production failure reported on 5 August:
the customer answers a numeric question with only ``2`` or ``3``.  Those
replies must stay in the sales funnel even when the LLM transport is down.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient

import services.api.app.main as api_main
from services.api.app.answerers import AnswerPipeline
from services.api.app.answerers.grounded_rag import GroundedRagAnswerer
from services.api.app.calendar.project_services_repository import (
    ProjectServiceRepository,
)
from services.api.app.main import app as api_app
from services.api.app.openrouter_client import GroundingVerdict, LlmUsageCapture
from services.api.app.project_prompts import ProjectPromptRepository
from services.api.app.rag import RagRepository
from services.api.app.russian_text import get_russian_normalizer
from services.api.app.sales.intent import Intent
from services.api.app.sales.price_lookup import PriceLookup
from services.api.app.sales.sales_persona_answerer import SalesPersonaAnswerer
from services.api.app.sales.services_repository import ServicesRepository
from services.api.app.sales.state_repository import StateRepository

pytestmark = [pytest.mark.e2e, pytest.mark.epic("12")]

_PROJECT_ID = 1
_OPERATOR_CHAT_ID = 7701
_NOW = datetime(2026, 8, 6, 10, 0, tzinfo=UTC)

# These lines are copied from the approved RAG chunks in the local database:
# knowledge_candidate:5 (Презентация 26.pdf) and knowledge_candidate:6
# (козлотур буклет А4 итог.pdf).  Keep this snapshot compact so the test is
# deterministic in CI while still exercising the production chunking and
# lemma-overlap retrieval code.
_APPROVED_RAG_VEHICLE_TEXT = """
Квадроциклы, Багги и Эндуро.
Багги (BRP & Yamaha) Эндуро (Kayo & GR 8)
Квадроциклы CFMOTO 500:
• идеальны для новичков • с широкими колёсами для устойчивости на склонах
• усиленная подвеска • автоматическая коробка
Эндуро (мото) - Аренда От 4 000₽ / час (Минимальное время: от 2 часов)
Багги: 20 000 Р до 75 000 Р.
Квадроцикл: 13 000 ₽ за квадроцикл.
Квадроцикл: 15 000 ₽ за квадроцикл.
Пикник в горах: от 10 000₽.
Баня и купель в горах: от 10 000₽.
Маршрут по горным просторам на квадроциклах/эндуро/багги согласно вашим личным предпочтениям.
Обучение езде на квадроцикле, мотоцикле Эндуро, багги.
""".strip()


@dataclass(frozen=True)
class _VehicleCase:
    name: str
    typo_selection: str
    people: str
    vehicles: str
    difficulty: str
    drivers: str
    price_token: str


_VEHICLES = (
    _VehicleCase(
        name="квадроциклы",
        typo_selection="Увадрациклы",
        people="2",
        vehicles="2",
        difficulty="Средний маршрут, мы катаемся иногда, но без экстрима.",
        drivers="2",
        price_token="13 000 ₽",
    ),
    _VehicleCase(
        name="багги",
        typo_selection="багги",
        people="3",
        vehicles="2",
        difficulty="Давайте лёгкий маршрут, мы скорее начинающие.",
        drivers="2",
        price_token="20 000 Р",
    ),
    _VehicleCase(
        name="мотоциклы",
        typo_selection="Мотоциклы",
        people="3",
        vehicles="3",
        difficulty="Средний маршрут, опыт небольшой, без экстрима.",
        drivers="3",
        price_token="4 000₽",
    ),
)


class _SalesOpenRouter:
    """Script only the non-deterministic funnel extraction calls."""

    def __init__(self, *, difficulty: str) -> None:
        self._difficulty = difficulty
        self.calls: list[dict[str, Any]] = []

    async def complete_json(
        self,
        *,
        system: str,
        user: str,
        model: str | None = None,
        **_kwargs: Any,
    ) -> dict[str, Any]:
        self.calls.append({"system": system, "user": user, "model": model})
        if "стоимость" in system.casefold() or "цена" in system.casefold():
            # The exact token is asserted by the caller against the RAG result;
            # the booking tests do not ask a price, so this branch is defensive.
            return {"text": "Уточню стоимость по каталогу."}
        return {
            "extracted_fields": {"difficulty": self._difficulty},
            "next_question": "Сколько водителей будет?",
        }


class _GroundedRagOpenRouter:
    """Keep grounding deterministic while retaining real retrieval inputs."""

    name = "rag-test-llm"

    def __init__(self) -> None:
        self.grounding_calls: list[dict[str, Any]] = []
        self.verification_calls: list[dict[str, Any]] = []

    async def answer_grounded(self, **kwargs: Any):
        self.grounding_calls.append(kwargs)
        question = str(kwargs["question"]).casefold()
        if "услуг" in question or "вариант" in question:
            answer = "У нас есть квадроциклы, багги и эндуро (мотоциклы)."
        else:
            answer = str(kwargs["snippets"][0].chunk_text)
        return answer, LlmUsageCapture(
            model_name=self.name,
            prompt_tokens=20,
            completion_tokens=10,
            cost_usd=0.0,
            created_at="2026-08-06T10:00:00Z",
        )

    async def verify_grounding(self, **kwargs: Any):
        self.verification_calls.append(kwargs)
        return GroundingVerdict(label="GROUNDED", reason="test answer uses RAG"), LlmUsageCapture(
            model_name=self.name,
            prompt_tokens=10,
            completion_tokens=5,
            cost_usd=0.0,
            created_at="2026-08-06T10:00:00Z",
        )


class _EmptyCatalogDigest:
    async def get_digest(self, *, project_id: int | None) -> str:
        del project_id
        return ""


def _seed_rag(rag: RagRepository) -> None:
    inserted = rag.ingest(
        source_id="knowledge_candidate:5",
        text=_APPROVED_RAG_VEHICLE_TEXT,
        project_id=_PROJECT_ID,
    )
    assert inserted > 0


def _build_pipeline(
    tmp_path, *, difficulty: str
) -> tuple[
    AnswerPipeline,
    StateRepository,
    RagRepository,
    _SalesOpenRouter,
    _GroundedRagOpenRouter,
]:
    state_repo = StateRepository(db_path=str(tmp_path / "sales.sqlite3"))
    services_repo = ServicesRepository(db_path=str(tmp_path / "services.sqlite3"))
    rag_repo = RagRepository(db_path=str(tmp_path / "rag.sqlite3"))
    _seed_rag(rag_repo)

    normalizer = get_russian_normalizer()
    sales_llm = _SalesOpenRouter(difficulty=difficulty)
    sales = SalesPersonaAnswerer(
        state_repo=state_repo,
        services_repo=services_repo,
        openrouter=sales_llm,
        normalizer=normalizer,
        clock=lambda: _NOW,
        bot_persona_getter=lambda: "Анна",
        rag_retriever=rag_repo,
        price_lookup=PriceLookup(
            rag_retriever=rag_repo,
            normalizer=normalizer,
        ),
    )
    grounded_llm = _GroundedRagOpenRouter()
    grounded = GroundedRagAnswerer(
        rag_repository=rag_repo,
        openrouter_client=grounded_llm,
        persona_reader=lambda: ("Анна", ""),
        project_prompt_repository=ProjectPromptRepository(
            str(tmp_path / "prompts.sqlite3")
        ),
        catalog_digest_service=_EmptyCatalogDigest(),
        project_services_reader=ProjectServiceRepository(
            db_path=str(tmp_path / "calendar.sqlite3")
        ),
    )
    return AnswerPipeline([sales, grounded]), state_repo, rag_repo, sales_llm, grounded_llm


def _wire_http(tmp_path, monkeypatch, pipeline: AnswerPipeline) -> AsyncMock:
    monkeypatch.setattr(api_main, "answer_pipeline", pipeline)
    monkeypatch.setattr(
        api_main,
        "_resolve_project_for_inbound",
        lambda **_kwargs: _PROJECT_ID,
    )
    monkeypatch.setattr(
        api_main, "_pick_assignee_for_chat", lambda _chat_id: "@flexsentlabs"
    )
    monkeypatch.setattr(
        api_main,
        "_effective_hitl_operator_chat_id",
        lambda: str(_OPERATOR_CHAT_ID),
    )
    monkeypatch.setattr(api_main, "_should_send_interim", lambda **_kwargs: False)
    monkeypatch.setattr(
        api_main, "_enqueue_outbound_customer_message", lambda **_kwargs: None
    )
    monkeypatch.setattr(api_main, "_enqueue_hitl_event", lambda **_kwargs: None)
    monkeypatch.setattr(api_main, "maybe_cancel", lambda **_kwargs: None)
    monkeypatch.setattr(
        api_main.hitl_ticket_repository,
        "db_path",
        str(tmp_path / "hitl.sqlite3"),
    )
    monkeypatch.setattr(
        api_main.incident_repository,
        "db_path",
        str(tmp_path / "incidents.sqlite3"),
    )
    monkeypatch.setattr(
        api_main.answer_trace_repository,
        "db_path",
        str(tmp_path / "answer-traces.sqlite3"),
    )
    sender = AsyncMock(return_value=1)
    monkeypatch.setattr(api_main.telegram_bot_sender, "send_message", sender)
    return sender


@pytest.mark.asyncio
@pytest.mark.parametrize("vehicle", _VEHICLES, ids=lambda item: item.name)
async def test_current_rag_contains_each_vehicle_and_its_price(
    tmp_path, vehicle: _VehicleCase
) -> None:
    """The service questions are based on retrievable approved knowledge."""
    _pipeline, _state, rag, _sales_llm, _grounded_llm = _build_pipeline(
        tmp_path, difficulty="средний"
    )

    hits = rag.retrieve(
        query=f"{vehicle.name} маршрут",
        limit=8,
        project_id=_PROJECT_ID,
    )
    assert hits, f"RAG has no hit for {vehicle.name}"
    assert any(vehicle.name.rstrip("ы") in hit.chunk_text.casefold() for hit in hits)

    price = PriceLookup(
        rag_retriever=rag,
        normalizer=get_russian_normalizer(),
    )
    result = await price.lookup(
        project_id=_PROJECT_ID,
        intent=Intent(),
        question=f"Сколько стоит {vehicle.name}?",
    )
    assert result.__class__.__name__ == "PriceFound", result
    assert vehicle.price_token in result.snippet  # type: ignore[union-attr]


@pytest.mark.parametrize("vehicle", _VEHICLES, ids=lambda item: item.name)
def test_rag_vehicle_dialogue_reaches_operator_after_numeric_replies(
    tmp_path, monkeypatch, vehicle: _VehicleCase
) -> None:
    """Each RAG-backed offering survives a natural full booking dialogue."""
    pipeline, state_repo, rag, sales_llm, grounded_llm = _build_pipeline(
        tmp_path, difficulty="средний"
    )
    sender = _wire_http(tmp_path, monkeypatch, pipeline)
    client = TestClient(api_app)
    chat_id = 12100 + _VEHICLES.index(vehicle)

    # The catalog answer is sourced through the real RAG answerer; it does not
    # create a booking state, so selecting one of the offered vehicles starts
    # the normal funnel.
    opening = [
        "Привет",
        "Какие услуги и варианты поездок есть?",
        vehicle.typo_selection,
        "завтра",
        vehicle.people,
        vehicle.vehicles,
        vehicle.difficulty,
        vehicle.drivers,
    ]
    responses: list[dict[str, Any]] = []
    for turn, text in enumerate(opening, start=1):
        response = client.post(
            "/conversations/inbound",
            json={
                "text": text,
                "chat_id": chat_id,
                "customer_username": "@artur",
                "trace_id": f"rag-vehicle-{chat_id}-{turn}",
            },
        )
        assert response.status_code == 200, response.text
        body = response.json()
        responses.append(body)
        assert body.get("answer_text") not in {"С этим не помогу.", "Это не ко мне."}

    catalog = responses[1]
    assert catalog["response_mode"] in {"grounded_rag", "grounded_rag_fallback"}
    catalog_text = (catalog["answer_text"] or "").casefold()
    assert all(
        marker in catalog_text for marker in ("квадроциклы", "багги", "эндуро")
    )
    assert grounded_llm.grounding_calls
    assert any(
        any(
            marker in chunk.chunk_text.casefold()
            for marker in ("квадроцикл", "багги", "эндуро")
        )
        for chunk in grounded_llm.grounding_calls[0]["snippets"]
    )

    # This is the regression assertion for both observed production chats:
    # the short numeric answer is handled by the sales answerer and never
    # reaches ScopeGuard when OpenRouter is unavailable.
    assert responses[4]["answer_text"]
    assert responses[-1]["escalated"] is True
    assert responses[-1]["response_mode"] == "sales_escalation"
    assert responses[-1]["hitl_ticket_id"] is not None
    assert responses[-1]["hitl_operator_username"] == "@flexsentlabs"

    state = state_repo.get(chat_id)
    assert state is not None
    intent = state["collected_intent"]
    assert intent["headcount"] == int(vehicle.people)
    assert intent["vehicle_count"] == int(vehicle.vehicles)
    assert intent["difficulty"] == "средний"
    assert intent["drivers"] == int(vehicle.drivers)
    assert sales_llm.calls, "only the free-form difficulty needs the scripted LLM"

    operator_texts = [
        call.kwargs["text"]
        for call in sender.await_args_list
        if call.kwargs["chat_id"] == _OPERATOR_CHAT_ID
    ]
    assert any(
        f"HITL ticket #{responses[-1]['hitl_ticket_id']}" in text
        and "headcount" in text
        and "vehicle_count" in text
        for text in operator_texts
    )

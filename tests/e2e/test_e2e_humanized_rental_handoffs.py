"""Humanized customer dialogues for vehicle rentals.

These scenarios intentionally use the way a real Telegram customer writes:
polite filler, uncertainty, clarifications and a service typo.  Every turn is
sent through the inbound HTTP endpoint and the final turn must create and
notify an operator HITL ticket.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient

import services.api.app.main as api_main
from services.api.app.answerers import AnswerPipeline
from services.api.app.main import app as api_app
from services.api.app.russian_text import get_russian_normalizer
from services.api.app.sales.sales_persona_answerer import SalesPersonaAnswerer
from services.api.app.sales.services_repository import ServicesRepository
from services.api.app.sales.state_repository import StateRepository

pytestmark = [pytest.mark.e2e, pytest.mark.epic("12")]

_PROJECT_ID = 1210
_OPERATOR_CHAT_ID = 7700


class _StubOpenRouter:
    def __init__(self, responses: list[dict[str, Any]]) -> None:
        self._responses = responses

    async def complete_json(
        self, *, system: str, user: str, model: str | None = None, **_kwargs: Any
    ) -> dict[str, Any]:
        del system, user, model
        if not self._responses:
            raise AssertionError("unexpected LLM call after the scripted dialogue")
        return self._responses.pop(0)


@dataclass(frozen=True)
class _RentalScenario:
    name: str
    chat_id: int
    opening: str
    headcount: str
    vehicles: str
    route: str
    drivers: str


_SCENARIOS = (
    _RentalScenario(
        name="quad bikes",
        chat_id=12101,
        opening=(
            "Здравствуйте! Мы с друзьями хотим завтра покататься на "
            "квадрацыклах. Подскажите, пожалуйста, как лучше всё организовать?"
        ),
        headcount="Нас будет 2 человека, если что, включая меня.",
        vehicles=(
            "Думаю, нам понадобится 2 квадрика — хочется, чтобы всем было удобно."
        ),
        route="Мы не профи, но и совсем простой маршрут не хочется — давайте средний.",
        drivers="Водитель нужен один, я сам сяду за руль.",
    ),
    _RentalScenario(
        name="buggies",
        chat_id=12102,
        opening=(
            "Добрый день! Хотели бы завтра выбраться на багги, но сначала "
            "хочется понять, какие варианты поездок у вас есть."
        ),
        headcount="Будет 3 человека, нас немного, но хочется без спешки.",
        vehicles="Наверное, возьмём 2 багги, чтобы не тесниться и нормально ехать.",
        route="По опыту мы скорее начинающие, поэтому маршрут лучше лёгкий.",
        drivers="За рулём будут двое, остальные пассажиры.",
    ),
    _RentalScenario(
        name="motorcycles",
        chat_id=12103,
        opening=(
            "Привет! Планируем завтра небольшую поездку на мотоциклах. "
            "Можно уточнить, как это обычно проходит и что нужно для записи?"
        ),
        headcount="Едем вдвоём, то есть всего 2 человека.",
        vehicles="Техники, думаю, нужно 2 — по одному мотоциклу на каждого.",
        route="Маршрут хотелось бы средний: катаемся иногда, но без экстрима.",
        drivers="Водителя будет два, каждый поедет сам.",
    ),
)


def _llm_script() -> list[dict[str, Any]]:
    return [
        {
            "extracted_fields": {"dates": "завтра"},
            "next_question": "Сколько человек поедет?",
        },
        {
            "extracted_fields": {"difficulty": "средний"},
            "next_question": "Сколько нужно водителей?",
        },
        {
            "extracted_fields": {"drivers": 1},
            "next_question": "Принято.",
        },
    ]


def _wire(tmp_path, monkeypatch, scenario: _RentalScenario) -> AsyncMock:
    state_repo = StateRepository(db_path=str(tmp_path / f"state-{scenario.chat_id}.sqlite3"))
    services_repo = ServicesRepository(db_path=str(tmp_path / "services.sqlite3"))
    for name in ("Квадроциклы", "Багги", "Мотоциклы"):
        if services_repo.get_by_name(project_id=_PROJECT_ID, name=name) is None:
            services_repo.add(
                project_id=_PROJECT_ID,
                name=name,
                now=api_main.datetime.now(api_main.UTC),
                description_md=f"Прокат: {name.lower()}.",
            )
    answerer = SalesPersonaAnswerer(
        state_repo=state_repo,
        services_repo=services_repo,
        openrouter=_StubOpenRouter(_llm_script()),
        normalizer=get_russian_normalizer(),
        clock=lambda: api_main.datetime.now(api_main.UTC),
        bot_persona_getter=lambda: "Анна",
    )
    monkeypatch.setattr(api_main, "answer_pipeline", AnswerPipeline([answerer]))
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
    monkeypatch.setattr(api_main, "_enqueue_outbound_customer_message", lambda **_kwargs: None)
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
    send_mock = AsyncMock(return_value=1)
    monkeypatch.setattr(api_main.telegram_bot_sender, "send_message", send_mock)
    return send_mock


@pytest.mark.parametrize("scenario", _SCENARIOS, ids=lambda item: item.name)
def test_humanized_rental_dialogue_reaches_operator_ticket(
    tmp_path, monkeypatch, scenario: _RentalScenario
) -> None:
    send_mock = _wire(tmp_path, monkeypatch, scenario)
    client = TestClient(api_app)

    conversation = [
        scenario.opening,
        scenario.headcount,
        scenario.vehicles,
        scenario.route,
        scenario.drivers,
    ]
    responses = []
    for turn, text in enumerate(conversation, start=1):
        response = client.post(
            "/conversations/inbound",
            json={
                "text": text,
                "chat_id": scenario.chat_id,
                "customer_username": "@artur",
                "trace_id": f"humanized-{scenario.chat_id}-{turn}",
            },
        )
        assert response.status_code == 200
        body = response.json()
        responses.append(body)
        assert body.get("answer_text") != "С этим не помогу."

    final = responses[-1]
    assert final["escalated"] is True
    assert final["response_mode"] == "sales_escalation"
    assert final["hitl_ticket_id"] is not None
    assert final["hitl_operator_username"] == "@flexsentlabs"

    operator_messages = [
        call.kwargs["text"]
        for call in send_mock.await_args_list
        if call.kwargs["chat_id"] == _OPERATOR_CHAT_ID
    ]
    assert any(
        f"HITL ticket #{final['hitl_ticket_id']}" in text
        and "@artur" in text
        and "headcount" in text
        for text in operator_messages
    )

"""Adversarial rental dialogues through the real inbound HTTP pipeline.

These cases are deliberately awkward rather than polished happy paths: Russian
number words, a declined field, contradictory vehicle counts, service asides,
mixed offerings, impossible/past dates and non-catalog activities.  The
catalog and grounding path are the same RAG-backed fixtures used by the
vehicle E2E suite; only the LLM transport is deterministic test control.
"""

from __future__ import annotations

from typing import Any

import pytest
from fastapi.testclient import TestClient

from services.api.app.main import app as api_app
from services.api.app.sales.intent import Intent
from tests.e2e.test_e2e_rag_vehicle_service_dialogues import (
    _NOW,
    _PROJECT_ID,
    _build_pipeline,
    _wire_http,
)

pytestmark = [pytest.mark.e2e, pytest.mark.epic("12")]


class _PriceAwareSalesLlm:
    async def complete_json(
        self, *, system: str, user: str, model: str | None = None, **_kwargs: Any
    ) -> dict[str, Any]:
        del user, model
        if "стоимость" in system.casefold() or "цена" in system.casefold():
            return {"text": "Багги стоят 20 000 Р по каталогу."}
        return {
            "extracted_fields": {"difficulty": "средний"},
            "next_question": "Сколько водителей будет?",
        }


def _post(client: TestClient, *, chat_id: int, turn: int, text: str) -> dict[str, Any]:
    response = client.post(
        "/conversations/inbound",
        json={
            "text": text,
            "chat_id": chat_id,
            "customer_username": "@adversarial_customer",
            "trace_id": f"adversarial-{chat_id}-{turn}",
        },
    )
    assert response.status_code == 200, response.text
    return response.json()


def test_word_counts_declined_driver_and_final_handoff(tmp_path, monkeypatch) -> None:
    """Word-form counts must survive an LLM-independent numeric continuation."""
    pipeline, state_repo, _rag, _sales_llm, _grounded_llm = _build_pipeline(
        tmp_path, difficulty="средний"
    )
    sender = _wire_http(tmp_path, monkeypatch, pipeline)
    client = TestClient(api_app)
    chat_id = 13101

    conversation = [
        "Хочу покататься на багги",
        "завтра",
        "Нас будет двое, если это важно",
        "Одну багги, пожалуйста",
        "Без водителей, мы сами разберёмся",
    ]
    responses = []
    for turn, text in enumerate(conversation, start=1):
        responses.append(_post(client, chat_id=chat_id, turn=turn, text=text))

    assert all(
        response.get("answer_text") not in {"С этим не помогу.", "Это не ко мне."}
        for response in responses
    )
    assert responses[2]["answer_text"] == "Сколько багги нужно?"
    assert responses[3]["answer_text"] == "Сколько нужно водителей?"
    final = responses[-1]
    assert final["escalated"] is True
    assert final["response_mode"] == "sales_escalation"
    assert final["hitl_operator_username"] == "@flexsentlabs"

    state = state_repo.get(chat_id)
    assert state is not None
    assert state["collected_intent"]["headcount"] == 2
    assert state["collected_intent"]["vehicle_count"] == 1
    assert state["collected_intent"]["drivers"] == "не требуется"
    assert any(
        "drivers=не требуется" in call.kwargs["text"]
        for call in sender.await_args_list
        if call.kwargs["chat_id"] == 7701
    )


def test_last_vehicle_count_mismatch_is_clarified_before_handoff(
    tmp_path, monkeypatch
) -> None:
    """A bad count must not become a completed operator booking."""
    pipeline, state_repo, _rag, _sales_llm, _grounded_llm = _build_pipeline(
        tmp_path, difficulty="средний"
    )
    _wire_http(tmp_path, monkeypatch, pipeline)
    client = TestClient(api_app)
    chat_id = 13102
    state_repo.upsert(
        chat_id=chat_id,
        project_id=_PROJECT_ID,
        current_stage="scoping",
        collected_intent=Intent(
            dates="завтра",
            headcount=2,
            difficulty="средний",
            drivers=1,
        ).to_dict(),
        now=_NOW,
    )

    result = _post(client, chat_id=chat_id, turn=1, text="Пять багги")

    assert result["answer_text"] is not None
    assert "5 багги" in result["answer_text"]
    assert result["response_mode"] is None
    assert result.get("escalated") is not True
    assert result.get("hitl_ticket_id") is None


def test_price_aside_preserves_funnel_then_completes(tmp_path, monkeypatch) -> None:
    """A price question mid-booking must not reset or complete the funnel."""
    pipeline, state_repo, _rag, _sales_llm, _grounded_llm = _build_pipeline(
        tmp_path, difficulty="средний"
    )
    _wire_http(tmp_path, monkeypatch, pipeline)
    pipeline.answerers[0]._openrouter = _PriceAwareSalesLlm()  # type: ignore[attr-defined]
    client = TestClient(api_app)
    chat_id = 13103

    responses = [
        _post(client, chat_id=chat_id, turn=1, text="Хочу покататься на багги"),
        _post(client, chat_id=chat_id, turn=2, text="завтра"),
    ]
    price = _post(client, chat_id=chat_id, turn=3, text="А сколько это стоит?")
    state_after_price = state_repo.get(chat_id)
    responses.extend(
        [
            price,
            _post(client, chat_id=chat_id, turn=4, text="Нас двое"),
            _post(client, chat_id=chat_id, turn=5, text="Одна багги"),
            _post(client, chat_id=chat_id, turn=6, text="Один водитель"),
        ]
    )

    price = responses[2]
    assert price["answer_text"]
    assert price["response_mode"] in {None, "sales_pricing", "sales_escalation"}
    assert price["answer_text"] not in {"С этим не помогу.", "Это не ко мне."}
    assert state_after_price is not None
    assert state_after_price["current_stage"] == "pricing"
    assert responses[3]["answer_text"] == "Сколько багги нужно?"
    assert responses[4]["answer_text"] == "Сколько нужно водителей?"
    assert responses[-1]["escalated"] is True


def test_rag_catalog_concept_and_price_asides_preserve_selected_service(
    tmp_path, monkeypatch
) -> None:
    """Catalog, concept and price asides must not lose the active RAG funnel."""
    pipeline, state_repo, _rag, _sales_llm, _grounded_llm = _build_pipeline(
        tmp_path, difficulty="средний"
    )
    _wire_http(tmp_path, monkeypatch, pipeline)
    pipeline.answerers[0]._openrouter = _PriceAwareSalesLlm()  # type: ignore[attr-defined]
    client = TestClient(api_app)
    chat_id = 13106

    selected = _post(client, chat_id=chat_id, turn=1, text="Багги")
    before = state_repo.get(chat_id)
    catalog = _post(
        client,
        chat_id=chat_id,
        turn=2,
        text="А что у вас вообще есть из поездок?",
    )
    after_catalog = state_repo.get(chat_id)
    concept = _post(
        client,
        chat_id=chat_id,
        turn=3,
        text="И что такое эндуро, кстати?",
    )
    after_concept = state_repo.get(chat_id)
    price = _post(client, chat_id=chat_id, turn=4, text="А сколько стоит багги?")
    after_price = state_repo.get(chat_id)

    assert selected["answer_text"] == "Отлично. На какую дату планируете поездку?"
    assert before is not None
    assert before["collected_intent"]["service"] == "багги"
    assert catalog["response_mode"] in {"grounded_rag", "grounded_rag_fallback"}
    assert catalog["answer_text"]
    assert after_catalog is not None
    assert after_catalog["collected_intent"] == before["collected_intent"]
    # The sales answerer wraps the RAG chunk itself, so the API response mode is
    # intentionally unset (unlike the generic grounded-rag answerer).
    assert concept["response_mode"] is None
    assert concept["answer_text"]
    assert after_concept is not None
    assert after_concept["collected_intent"] == before["collected_intent"]
    assert "20 000" in price["answer_text"]
    assert price["response_mode"] in {None, "sales_pricing"}
    assert after_price is not None
    assert after_price["current_stage"] == "pricing"
    assert after_price["collected_intent"]["service"] == "багги"


def test_typo_temporal_reply_and_natural_count_words_reach_handoff(
    tmp_path, monkeypatch
) -> None:
    """A typo date and word-form counts stay in the same vehicle funnel."""
    pipeline, state_repo, _rag, _sales_llm, _grounded_llm = _build_pipeline(
        tmp_path, difficulty="средний"
    )
    sender = _wire_http(tmp_path, monkeypatch, pipeline)
    client = TestClient(api_app)
    chat_id = 13107

    responses = [
        _post(client, chat_id=chat_id, turn=1, text="Квадрациклы"),
        _post(client, chat_id=chat_id, turn=2, text="Завтро"),
        _post(client, chat_id=chat_id, turn=3, text="Нас будет трое взрослых"),
        _post(client, chat_id=chat_id, turn=4, text="Две машины"),
        _post(
            client,
            chat_id=chat_id,
            turn=5,
            text="Средний маршрут, без экстрима",
        ),
        _post(client, chat_id=chat_id, turn=6, text="Без водителя, я сам за рулём"),
    ]

    assert responses[1]["answer_text"] == "Сколько человек поедет?"
    assert responses[2]["answer_text"] == "Сколько квадроциклов нужно?"
    assert responses[3]["answer_text"] == "Сколько нужно водителей?"
    assert responses[-1]["escalated"] is True
    state = state_repo.get(chat_id)
    assert state is not None
    assert state["collected_intent"]["headcount"] == 3
    assert state["collected_intent"]["vehicle_count"] == 2
    assert state["collected_intent"]["drivers"] == "не требуется"
    assert any(
        "drivers=не требуется" in call.kwargs["text"]
        for call in sender.await_args_list
        if call.kwargs["chat_id"] == 7701
    )


@pytest.mark.parametrize(
    ("text", "service"),
    [("Мото", "мопед"), ("Эндурка", "эндуро")],
)
def test_colloquial_service_name_starts_the_correct_funnel(
    tmp_path, monkeypatch, text: str, service: str
) -> None:
    pipeline, state_repo, _rag, _sales_llm, _grounded_llm = _build_pipeline(
        tmp_path, difficulty="средний"
    )
    _wire_http(tmp_path, monkeypatch, pipeline)
    client = TestClient(api_app)

    result = _post(client, chat_id=13105, turn=1, text=text)

    assert result["answer_text"] == "Отлично. На какую дату планируете поездку?"
    state = state_repo.get(13105)
    assert state is not None
    assert state["collected_intent"]["service"] == service


@pytest.mark.parametrize(
    ("text", "turn_kind"),
    [
        ("Хочу багги и квадроциклы, чтобы сравнить", "mixed_service"),
        ("Можно записаться на багги 31 февраля?", "invalid_date"),
        ("Вчера хотел покататься на багги", "past_date"),
        ("А на вертолёте у вас полетать можно?", "out_of_scope_decline"),
    ],
)
def test_ambiguous_or_invalid_requests_do_not_create_booking(
    tmp_path, monkeypatch, text: str, turn_kind: str
) -> None:
    pipeline, _state_repo, _rag, _sales_llm, _grounded_llm = _build_pipeline(
        tmp_path, difficulty="средний"
    )
    _wire_http(tmp_path, monkeypatch, pipeline)
    client = TestClient(api_app)

    result = _post(client, chat_id=13104, turn=1, text=text)

    expected_markers = {
        "mixed_service": "по одной услуге",
        "invalid_date": "не существует",
        "past_date": "уже прошла",
        "out_of_scope_decline": "не помогу",
    }
    assert expected_markers[turn_kind] in result["answer_text"].casefold()
    assert result.get("escalated") is not True
    assert result.get("hitl_ticket_id") is None
    assert result["answer_text"] not in {"С этим не помогу.", "Это не ко мне."}

"""Scope guard answerer tests.

Covers:
- try_answer always returns handled=True with scope_decline mode
- Text is picked from the phrases_getter list
- Multiple calls can produce different phrases (randomness)
- scope_guard is the last answerer in the live pipeline
- Runtime config override respected via _effective_scope_decline_messages
- Integration: off-topic message delivers one of the phrases, no HITL ticket
"""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient

import services.api.app.main as main_mod
from services.api.app.answerers import AnswerContext
from services.api.app.answerers.scope_guard import (
    RESPONSE_MODE_SCOPE_DECLINE,
    ScopeGuardAnswerer,
)
from services.api.app.main import (
    _effective_scope_decline_messages,
    answer_pipeline,
    answer_trace_repository,
    hitl_ticket_repository,
    incident_repository,
    rag_repository,
    settings,
    telegram_bot_sender,
)
from services.api.app.main import app as api_app


def _make_ctx() -> AnswerContext:
    return AnswerContext(
        chat_id=1,
        customer_username="@u",
        trace_id="t",
        now=datetime.now(UTC),
    )


def _wire(tmp_path) -> None:
    hitl_ticket_repository.db_path = str(tmp_path / "hitl.sqlite3")
    incident_repository.db_path = str(tmp_path / "incidents.sqlite3")
    rag_repository.db_path = str(tmp_path / "rag.sqlite3")
    answer_trace_repository.db_path = str(tmp_path / "answer_traces.sqlite3")


# ---------------------------------------------------------------------------
# Unit: ScopeGuardAnswerer
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_try_answer_returns_handled_true():
    answerer = ScopeGuardAnswerer(phrases_getter=lambda: "Этим не занимаюсь.")
    result = await answerer.try_answer(question="Который час?", ctx=_make_ctx())
    assert result.handled is True
    assert result.response_mode == RESPONSE_MODE_SCOPE_DECLINE
    assert result.text == "Этим не занимаюсь."


@pytest.mark.asyncio
async def test_try_answer_picks_from_multiline_list():
    phrases = "Фраза А\nФраза Б\nФраза В"
    answerer = ScopeGuardAnswerer(phrases_getter=lambda: phrases)
    results = {
        (await answerer.try_answer(question="x", ctx=_make_ctx())).text
        for _ in range(30)
    }
    assert results <= {"Фраза А", "Фраза Б", "Фраза В"}
    assert len(results) > 1, "expected random variation across 30 calls"


@pytest.mark.asyncio
async def test_try_answer_strips_blank_lines():
    answerer = ScopeGuardAnswerer(phrases_getter=lambda: "\nФраза А\n\nФраза Б\n")
    for _ in range(10):
        result = await answerer.try_answer(question="x", ctx=_make_ctx())
        assert result.text in {"Фраза А", "Фраза Б"}


@pytest.mark.asyncio
async def test_try_answer_single_phrase_always_returns_it():
    answerer = ScopeGuardAnswerer(phrases_getter=lambda: "Единственная фраза.")
    for _ in range(5):
        result = await answerer.try_answer(question="x", ctx=_make_ctx())
        assert result.text == "Единственная фраза."


# ---------------------------------------------------------------------------
# Unit: in-scope asks DEFER (skip) so the inbound HITL escalation handles them
# (Story 12.39, D10). A price/booking ask only reaches the last-resort guard
# when every upstream answerer skipped (e.g. the persona LLM briefly failed) —
# declining a real customer with "Этим не занимаюсь." is the bug.
# ---------------------------------------------------------------------------


def _bookings_answerer(phrases: str = "Этим не занимаюсь.") -> ScopeGuardAnswerer:
    """A scope guard whose project DOES offer bookings (so in-scope asks defer)."""
    return ScopeGuardAnswerer(
        phrases_getter=lambda: phrases,
        project_does_bookings=lambda _pid: True,
    )


@pytest.mark.asyncio
async def test_in_scope_price_ask_defers_to_hitl():
    answerer = _bookings_answerer()
    result = await answerer.try_answer(
        question="Сколько стоит покраска?", ctx=_make_ctx()
    )
    assert result.handled is False
    assert result.text is None
    assert result.metadata.get("skip_reason") == "in_scope_defer_to_hitl"


@pytest.mark.asyncio
async def test_in_scope_booking_ask_defers_to_hitl():
    answerer = _bookings_answerer()
    result = await answerer.try_answer(
        question="Запишите на багги 3 июня в 13:00", ctx=_make_ctx()
    )
    assert result.handled is False
    assert result.metadata.get("skip_reason") == "in_scope_defer_to_hitl"


@pytest.mark.asyncio
async def test_in_scope_ask_declines_when_project_has_no_bookings():
    # Default (no bookings, e.g. a disabled noop project): even an in-scope ask
    # declines rather than escalating — don't turn off-topic into operator noise.
    answerer = ScopeGuardAnswerer(phrases_getter=lambda: "Этим не занимаюсь.")
    result = await answerer.try_answer(
        question="Запишите на багги 3 июня в 13:00", ctx=_make_ctx()
    )
    assert result.handled is True
    assert result.response_mode == RESPONSE_MODE_SCOPE_DECLINE


@pytest.mark.asyncio
async def test_factual_question_still_declines():
    # "Какое сегодня число?" is is_sales_intent-positive (false positive!) but is
    # NOT a price/booking ask -> the precise signal keeps it a scope decline even
    # on a bookings-enabled project.
    answerer = _bookings_answerer()
    result = await answerer.try_answer(
        question="Какое сегодня число?", ctx=_make_ctx()
    )
    assert result.handled is True
    assert result.response_mode == RESPONSE_MODE_SCOPE_DECLINE


@pytest.mark.asyncio
async def test_out_of_scope_booking_still_declines():
    # A booking-shaped ask for an out-of-scope service (lodging, Story 12.34)
    # declines rather than escalating — we don't book hotels.
    answerer = _bookings_answerer()
    result = await answerer.try_answer(
        question="Где забронировать отель?", ctx=_make_ctx()
    )
    assert result.handled is True
    assert result.response_mode == RESPONSE_MODE_SCOPE_DECLINE


# ---------------------------------------------------------------------------
# Unit: _effective_scope_decline_messages
# ---------------------------------------------------------------------------


def test_effective_scope_decline_messages_returns_settings_default(tmp_path, monkeypatch):
    hitl_ticket_repository.db_path = str(tmp_path / "hitl.sqlite3")
    monkeypatch.setattr(settings, "scope_decline_messages", "Тест.")
    assert _effective_scope_decline_messages() == "Тест."


def test_effective_scope_decline_messages_runtime_config_overrides(tmp_path, monkeypatch):
    hitl_ticket_repository.db_path = str(tmp_path / "hitl.sqlite3")
    hitl_ticket_repository.set_runtime_config(
        key="scope_decline_messages", value="Кастом А\nКастом Б", updated_by="@op"
    )
    monkeypatch.setattr(settings, "scope_decline_messages", "Default.")
    try:
        assert _effective_scope_decline_messages() == "Кастом А\nКастом Б"
    finally:
        hitl_ticket_repository.set_runtime_config(
            key="scope_decline_messages", value="", updated_by="@op"
        )


# ---------------------------------------------------------------------------
# Pipeline wiring
# ---------------------------------------------------------------------------


def test_scope_guard_is_last_in_pipeline():
    last = answer_pipeline.answerers[-1]
    assert last.name == "scope_guard"


def test_scope_guard_is_in_pipeline():
    names = [a.name for a in answer_pipeline.answerers]
    assert "scope_guard" in names


# ---------------------------------------------------------------------------
# Integration: off-topic message → decline phrase, no HITL ticket
# ---------------------------------------------------------------------------


def _mini_pipeline(phrases: str, *, project_does_bookings=lambda _pid: False):
    """Return an AnswerPipeline with ONLY the ScopeGuardAnswerer.

    Replacing the full pipeline with this avoids real LLM/RAG calls in
    integration tests that are specifically exercising the scope guard path.
    ``project_does_bookings`` defaults to a noop project (decline path).
    """
    from services.api.app.answerers import AnswerPipeline

    return AnswerPipeline(
        [
            ScopeGuardAnswerer(
                phrases_getter=lambda: phrases,
                project_does_bookings=project_does_bookings,
            )
        ]
    )


def test_offtopic_message_delivers_decline_and_no_ticket(tmp_path, monkeypatch):
    _wire(tmp_path)
    phrases = "Этим не занимаюсь.\nНе по адресу."
    monkeypatch.setattr(main_mod, "answer_pipeline", _mini_pipeline(phrases))
    monkeypatch.setattr(main_mod, "_should_send_interim", lambda text, chat_id: False)

    send_calls: list[tuple] = []

    async def _fake_send(*, chat_id: int, text: str) -> int:
        send_calls.append((chat_id, text))
        return 1

    monkeypatch.setattr(telegram_bot_sender, "send_message", _fake_send)

    client = TestClient(api_app)
    resp = client.post(
        "/conversations/inbound",
        json={"text": "Который час?", "chat_id": 5001, "trace_id": "sg-trace-1"},
    )

    assert resp.status_code == 200
    body = resp.json()
    assert body["delivered"] is True
    assert body["escalated"] is False
    assert body["response_mode"] == RESPONSE_MODE_SCOPE_DECLINE

    assert len(send_calls) == 1
    assert send_calls[0][0] == 5001
    assert send_calls[0][1] in {"Этим не занимаюсь.", "Не по адресу."}

    tickets = client.get("/hitl/tickets").json()["items"]
    assert tickets == []


def test_inscope_ask_at_scope_guard_escalates_to_hitl(tmp_path, monkeypatch):
    # When an in-scope booking ask reaches the last-resort guard (every upstream
    # answerer skipped), the guard defers -> the inbound endpoint escalates to a
    # human: ack sent + ticket created (NOT a "Этим не занимаюсь." decline).
    _wire(tmp_path)
    monkeypatch.setattr(
        main_mod,
        "answer_pipeline",
        _mini_pipeline("Этим не занимаюсь.", project_does_bookings=lambda _pid: True),
    )
    monkeypatch.setattr(main_mod, "_should_send_interim", lambda text, chat_id: False)

    send_calls: list[tuple] = []

    async def _fake_send(*, chat_id: int, text: str) -> int:
        send_calls.append((chat_id, text))
        return 1

    monkeypatch.setattr(telegram_bot_sender, "send_message", _fake_send)

    client = TestClient(api_app)
    resp = client.post(
        "/conversations/inbound",
        json={
            "text": "Запишите на багги 3 июня в 13:00",
            "chat_id": 5003,
            "trace_id": "sg-trace-3",
        },
    )

    assert resp.status_code == 200
    body = resp.json()
    assert body["escalated"] is True
    assert body["response_mode"] == "human_only"

    tickets = client.get("/hitl/tickets").json()["items"]
    assert len(tickets) == 1
    # The customer got an ack (not a scope decline).
    assert len(send_calls) == 1
    assert send_calls[0][1] != "Этим не занимаюсь."


def test_offtopic_message_trace_persisted(tmp_path, monkeypatch):
    _wire(tmp_path)
    monkeypatch.setattr(main_mod, "answer_pipeline", _mini_pipeline("Здесь не помогу."))
    monkeypatch.setattr(main_mod, "_should_send_interim", lambda text, chat_id: False)
    monkeypatch.setattr(telegram_bot_sender, "send_message", AsyncMock(return_value=1))

    client = TestClient(api_app)
    client.post(
        "/conversations/inbound",
        json={"text": "Расскажи анекдот", "chat_id": 5002, "trace_id": "sg-trace-2"},
    )

    trace = client.get("/answer-traces/sg-trace-2").json()
    assert trace["response_mode"] == RESPONSE_MODE_SCOPE_DECLINE
    assert trace["guardrail_outcome"] == "valid"

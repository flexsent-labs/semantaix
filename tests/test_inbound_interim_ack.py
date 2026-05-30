"""Interim ack + pipeline hardening tests.

Covers:
- Interim message is sent before the pipeline for sales-intent messages.
- Interim message is sent for ongoing-sales-state messages.
- No interim is sent for non-sales messages.
- chat_id=None skips interim gracefully.
- An unhandled pipeline exception produces a HITL ack (not silence),
  creates a ticket, persists a trace, and does not re-raise.
- _should_send_interim and _effective_inbound_interim_message unit tests.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

from fastapi.testclient import TestClient

from services.api.app.answerers import AnswerResult
from services.api.app.main import (
    _effective_inbound_interim_delay,
    _effective_inbound_interim_message,
    _should_send_interim,
    answer_pipeline,
    answer_trace_repository,
    hitl_ticket_repository,
    incident_repository,
    rag_repository,
    sales_state_repository,
    settings,
    telegram_bot_sender,
)
from services.api.app.main import app as api_app


def _wire(tmp_path) -> None:
    hitl_ticket_repository.db_path = str(tmp_path / "hitl.sqlite3")
    incident_repository.db_path = str(tmp_path / "incidents.sqlite3")
    rag_repository.db_path = str(tmp_path / "rag.sqlite3")
    answer_trace_repository.db_path = str(tmp_path / "answer_traces.sqlite3")
    sales_state_repository.db_path = str(tmp_path / "sales.sqlite3")


def _stub_pipeline(monkeypatch, result: AnswerResult) -> AsyncMock:
    mock = AsyncMock(return_value=result)
    monkeypatch.setattr(answer_pipeline, "run", mock)
    return mock


# ---------------------------------------------------------------------------
# Unit tests for _should_send_interim
# ---------------------------------------------------------------------------


def test_should_send_interim_true_when_sales_intent(monkeypatch):
    """Sales-intent text with no prior state triggers the interim."""
    monkeypatch.setattr(sales_state_repository, "get", lambda chat_id: None)
    result = _should_send_interim(
        text="хочу забронировать багги завтра в 14:00",
        chat_id=12345,
    )
    assert result is True


def test_should_send_interim_true_when_active_state(monkeypatch):
    """Active sales state triggers interim regardless of text content."""
    monkeypatch.setattr(sales_state_repository, "get", lambda chat_id: {"stage": "scoping"})
    result = _should_send_interim(text="ок, взрослый", chat_id=12345)
    assert result is True


def test_should_send_interim_false_for_non_sales_text(monkeypatch):
    """Non-sales question with no active state skips the interim."""
    monkeypatch.setattr(sales_state_repository, "get", lambda chat_id: None)
    result = _should_send_interim(text="какой у вас график работы?", chat_id=12345)
    assert result is False


def test_should_send_interim_false_when_chat_id_none_and_no_intent(tmp_path, monkeypatch):
    """chat_id=None skips the state check; non-intent text → False."""
    boom = MagicMock(side_effect=AssertionError("should not call get"))
    monkeypatch.setattr(sales_state_repository, "get", boom)
    result = _should_send_interim(text="какой у вас график работы?", chat_id=None)
    assert result is False


def test_should_send_interim_true_for_sales_intent_without_chat_id(tmp_path):
    """Even with chat_id=None, a sales-intent message triggers interim."""
    result = _should_send_interim(text="хочу забронировать тур", chat_id=None)
    assert result is True


# ---------------------------------------------------------------------------
# Unit test for _effective_inbound_interim_message
# ---------------------------------------------------------------------------


def test_effective_inbound_interim_message_returns_settings_default(tmp_path, monkeypatch):
    hitl_ticket_repository.db_path = str(tmp_path / "hitl.sqlite3")
    monkeypatch.setattr(settings, "inbound_interim_message", "Тестовое сообщение")
    assert _effective_inbound_interim_message() == "Тестовое сообщение"


def test_effective_inbound_interim_delay_runtime_override(tmp_path, monkeypatch):
    hitl_ticket_repository.db_path = str(tmp_path / "hitl.sqlite3")
    hitl_ticket_repository.set_runtime_config(
        key="inbound_interim_delay_seconds", value="1.5", updated_by="@t"
    )
    try:
        assert _effective_inbound_interim_delay() == 1.5
    finally:
        hitl_ticket_repository.set_runtime_config(
            key="inbound_interim_delay_seconds", value="", updated_by="@t"
        )


def test_effective_inbound_interim_delay_invalid_falls_back(tmp_path, monkeypatch):
    hitl_ticket_repository.db_path = str(tmp_path / "hitl.sqlite3")
    hitl_ticket_repository.set_runtime_config(
        key="inbound_interim_delay_seconds", value="не число", updated_by="@t"
    )
    monkeypatch.setattr(settings, "inbound_interim_delay_seconds", 3.0)
    try:
        assert _effective_inbound_interim_delay() == 3.0
    finally:
        hitl_ticket_repository.set_runtime_config(
            key="inbound_interim_delay_seconds", value="", updated_by="@t"
        )


def test_effective_inbound_interim_message_runtime_config_overrides(tmp_path, monkeypatch):
    hitl_ticket_repository.db_path = str(tmp_path / "hitl.sqlite3")
    hitl_ticket_repository.set_runtime_config(
        key="inbound_interim_message", value="Секундочку!", updated_by="@test"
    )
    monkeypatch.setattr(settings, "inbound_interim_message", "Default")
    try:
        assert _effective_inbound_interim_message() == "Секундочку!"
    finally:
        hitl_ticket_repository.set_runtime_config(
            key="inbound_interim_message", value="", updated_by="@test"
        )


# ---------------------------------------------------------------------------
# Integration: interim is sent for sales-intent messages
# ---------------------------------------------------------------------------


def test_interim_sent_when_pipeline_is_slow(tmp_path, monkeypatch):
    # Story 12.13 — eligible message whose pipeline exceeds the delay → the
    # interim ack is sent, then the real answer.
    _wire(tmp_path)
    send_calls: list[tuple] = []

    async def _fake_send(*, chat_id: int, text: str) -> int:
        send_calls.append((chat_id, text))
        return len(send_calls)

    monkeypatch.setattr(telegram_bot_sender, "send_message", _fake_send)
    monkeypatch.setattr(settings, "inbound_interim_message", "Проверяю…")
    monkeypatch.setattr(settings, "inbound_interim_delay_seconds", 0.02)
    import services.api.app.main as main_mod
    monkeypatch.setattr(main_mod, "_should_send_interim", lambda text, chat_id: True)

    async def _slow_run(**_kwargs):
        import asyncio

        await asyncio.sleep(0.15)
        return AnswerResult(
            handled=True, text="Слот свободен!", response_mode="sales_booking"
        )

    monkeypatch.setattr(answer_pipeline, "run", _slow_run)

    client = TestClient(api_app)
    resp = client.post(
        "/conversations/inbound",
        json={"text": "хочу забронировать багги завтра в 14:00", "chat_id": 777},
    )
    assert resp.status_code == 200
    assert resp.json()["delivered"] is True
    assert len(send_calls) == 2
    assert send_calls[0] == (777, "Проверяю…")
    assert send_calls[1][1] == "Слот свободен!"


def test_interim_not_sent_when_pipeline_is_fast(tmp_path, monkeypatch):
    # Story 12.13 — eligible but fast (returns within the delay) → no interim.
    _wire(tmp_path)
    send_calls: list[tuple] = []

    async def _fake_send(*, chat_id: int, text: str) -> int:
        send_calls.append((chat_id, text))
        return len(send_calls)

    monkeypatch.setattr(telegram_bot_sender, "send_message", _fake_send)
    monkeypatch.setattr(settings, "inbound_interim_delay_seconds", 5.0)
    import services.api.app.main as main_mod
    monkeypatch.setattr(main_mod, "_should_send_interim", lambda text, chat_id: True)
    _stub_pipeline(
        monkeypatch,
        AnswerResult(handled=True, text="Готово!", response_mode="sales_booking"),
    )

    client = TestClient(api_app)
    resp = client.post(
        "/conversations/inbound",
        json={"text": "хочу забронировать багги", "chat_id": 777},
    )
    assert resp.status_code == 200
    assert len(send_calls) == 1  # only the real answer — no "минуточку"
    assert send_calls[0][1] == "Готово!"


def test_interim_not_sent_for_non_sales_message(tmp_path, monkeypatch):
    _wire(tmp_path)
    send_calls: list[tuple] = []

    async def _fake_send(*, chat_id: int, text: str) -> int:
        send_calls.append((chat_id, text))
        return 1

    monkeypatch.setattr(telegram_bot_sender, "send_message", _fake_send)
    import services.api.app.main as main_mod
    monkeypatch.setattr(main_mod, "_should_send_interim", lambda text, chat_id: False)
    _stub_pipeline(
        monkeypatch,
        AnswerResult(handled=True, text="Мы работаем с 9 до 18.", response_mode="grounded_rag"),
    )

    client = TestClient(api_app)
    resp = client.post(
        "/conversations/inbound",
        json={"text": "какой у вас график работы?", "chat_id": 888},
    )
    assert resp.status_code == 200
    # Only one send: the final answer, no interim
    assert len(send_calls) == 1
    assert send_calls[0][1] == "Мы работаем с 9 до 18."


def test_interim_skipped_when_chat_id_none(tmp_path, monkeypatch):
    _wire(tmp_path)
    send_mock = AsyncMock(return_value=1)
    monkeypatch.setattr(telegram_bot_sender, "send_message", send_mock)
    # Even if intent detected, chat_id=None suppresses the send
    import services.api.app.main as main_mod
    monkeypatch.setattr(main_mod, "_should_send_interim", lambda text, chat_id: True)
    _stub_pipeline(
        monkeypatch,
        AnswerResult(handled=True, text="Отлично!", response_mode="sales_booking"),
    )

    client = TestClient(api_app)
    resp = client.post(
        "/conversations/inbound",
        json={"text": "хочу забронировать багги", "chat_id": None},
    )
    assert resp.status_code == 200
    # No Telegram send because chat_id is None (guard in conversations_inbound)
    send_mock.assert_not_awaited()


# ---------------------------------------------------------------------------
# Integration: pipeline exception → HITL ack, no silence
# ---------------------------------------------------------------------------


def test_pipeline_exception_produces_hitl_ack_not_silence(tmp_path, monkeypatch):
    _wire(tmp_path)
    send_calls: list[tuple] = []

    async def _fake_send(*, chat_id: int, text: str) -> int:
        send_calls.append((chat_id, text))
        return len(send_calls)

    monkeypatch.setattr(telegram_bot_sender, "send_message", _fake_send)
    monkeypatch.setattr(settings, "inbound_ack_message", "Одну секунду, уточню.")
    import services.api.app.main as main_mod
    monkeypatch.setattr(main_mod, "_should_send_interim", lambda text, chat_id: False)

    async def _broken_run(**_kwargs):
        raise RuntimeError("LLM transport failed")

    monkeypatch.setattr(answer_pipeline, "run", _broken_run)

    client = TestClient(api_app)
    resp = client.post(
        "/conversations/inbound",
        json={"text": "какой у вас адрес?", "chat_id": 999, "trace_id": "err-trace-1"},
    )

    assert resp.status_code == 200
    body = resp.json()
    assert body["escalated"] is True
    assert body["delivered"] is False
    assert body["response_mode"] == "human_only"
    assert body["hitl_ticket_id"] is not None

    # Customer received the ack, not silence
    assert any(t == "Одну секунду, уточню." for _, t in send_calls)

    # Trace was persisted
    trace = client.get(f"/answer-traces/{body['trace_id']}").json()
    assert trace["guardrail_outcome"] == "pipeline_error"


def test_pipeline_exception_creates_hitl_ticket(tmp_path, monkeypatch):
    _wire(tmp_path)
    monkeypatch.setattr(telegram_bot_sender, "send_message", AsyncMock(return_value=1))
    import services.api.app.main as main_mod
    monkeypatch.setattr(main_mod, "_should_send_interim", lambda text, chat_id: False)

    async def _broken_run(**_kwargs):
        raise ValueError("date parse failed")

    monkeypatch.setattr(answer_pipeline, "run", _broken_run)

    client = TestClient(api_app)
    resp = client.post(
        "/conversations/inbound",
        json={"text": "хочу тур", "chat_id": 1001, "trace_id": "err-trace-2"},
    )
    assert resp.status_code == 200
    body = resp.json()
    ticket_id = body["hitl_ticket_id"]
    assert ticket_id is not None
    ticket = hitl_ticket_repository.get(ticket_id)
    assert ticket is not None
    assert ticket.reason == "pipeline_error"

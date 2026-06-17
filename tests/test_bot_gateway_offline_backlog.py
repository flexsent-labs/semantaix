from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from unittest.mock import AsyncMock, Mock

import pytest
from fastapi import BackgroundTasks
from fastapi.testclient import TestClient

from platform_common.settings import get_settings
from services.bot_gateway.app import main as bot_main
from services.bot_gateway.app.main import api_client
from services.bot_gateway.app.main import app as bot_app
from services.bot_gateway.app.offline_backlog_buffer import OfflineBacklogBuffer


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    monkeypatch.setenv("PERSISTENCE_DB_PATH", str(tmp_path / "persistence.sqlite3"))
    get_settings.cache_clear()
    # The module singleton binds the default DB path at import; swap in a
    # fresh, isolated buffer for each test.
    monkeypatch.setattr(
        bot_main,
        "offline_backlog_buffer",
        OfflineBacklogBuffer(str(tmp_path / "backlog.sqlite3")),
    )
    # Fire the debounce immediately so the flush drains on the first poll.
    monkeypatch.setattr(bot_main.settings, "offline_backlog_debounce_seconds", 0.0)
    yield
    get_settings.cache_clear()


def _stale_payload(
    *,
    update_id: int,
    message_id: int,
    text: str,
    chat_id: int = 5550,
    username: str = "customer",
) -> dict:
    sent = int(datetime.now(UTC).timestamp()) - 3600
    return {
        "update_id": update_id,
        "message": {
            "message_id": message_id,
            "from": {"id": chat_id, "username": username},
            "chat": {"id": chat_id, "type": "private"},
            "text": text,
            "date": sent,
        },
    }


def _live_payload(
    *,
    update_id: int,
    message_id: int,
    text: str,
    chat_id: int = 5551,
    username: str = "admin",
) -> dict:
    sent = int(datetime.now(UTC).timestamp())
    return {
        "update_id": update_id,
        "message": {
            "message_id": message_id,
            "from": {"id": chat_id, "username": username},
            "chat": {"id": chat_id, "type": "private"},
            "text": text,
            "date": sent,
        },
    }


# --- webhook branch ---------------------------------------------------------


def test_stale_customer_message_is_buffered_then_flushed(monkeypatch):
    forward = AsyncMock(return_value={})
    monkeypatch.setattr(api_client, "forward_inbound", forward)

    client = TestClient(bot_app)
    response = client.post(
        "/telegram/webhook",
        json=_stale_payload(
            update_id=8001,
            message_id=1,
            text="Подскажите, когда будет готов мой заказ?",
        ),
    )
    assert response.status_code == 200
    assert response.json()["status"] == "backlog_buffered"

    # debounce=0 -> the scheduled flush drains + forwards within the request.
    forward.assert_awaited_once()
    kwargs = forward.await_args.kwargs
    assert kwargs["text"] == "Подскажите, когда будет готов мой заказ?"
    assert kwargs["chat_id"] == 5550
    assert kwargs["customer_username"] == "@customer"
    assert kwargs["trace_id"] == "tg-update-8001"


def test_fresh_customer_message_uses_live_path(monkeypatch):
    forward = AsyncMock(return_value={})
    monkeypatch.setattr(api_client, "forward_inbound", forward)

    client = TestClient(bot_app)
    payload = _stale_payload(
        update_id=8002, message_id=2, text="Здравствуйте, есть ли места?"
    )
    payload["message"]["date"] = int(datetime.now(UTC).timestamp())
    response = client.post("/telegram/webhook", json=payload)

    assert response.json()["status"] == "accepted"
    forward.assert_awaited_once()
    assert forward.await_args.kwargs["text"] == "Здравствуйте, есть ли места?"
    assert forward.await_args.kwargs["trace_id"] == "tg-update-8002"


def test_second_distinct_message_same_chat_does_not_reschedule(monkeypatch):
    forward = AsyncMock(return_value={})
    monkeypatch.setattr(api_client, "forward_inbound", forward)
    # Pre-seed a row so the webhook's add() is NOT the first for this chat.
    bot_main.offline_backlog_buffer.add(
        chat_id=5550,
        update_id=1,
        source_message_id=99,
        customer_username="@customer",
        text="ранее",
        message_date=None,
    )

    client = TestClient(bot_app)
    response = client.post(
        "/telegram/webhook",
        json=_stale_payload(update_id=8003, message_id=3, text="новое сообщение тут"),
    )
    assert response.json()["status"] == "backlog_buffered"
    # No flush scheduled (not first row) -> nothing forwarded this request.
    forward.assert_not_awaited()
    assert len(bot_main.offline_backlog_buffer.drain(chat_id=5550)) == 2


# --- flush coroutine --------------------------------------------------------


def _seed(chat_id: int, *messages: tuple[int, str]) -> None:
    for update_id, text in messages:
        bot_main.offline_backlog_buffer.add(
            chat_id=chat_id,
            update_id=update_id,
            source_message_id=update_id,
            customer_username="@c",
            text=text,
            message_date=None,
        )


@pytest.mark.asyncio
async def test_flush_collapses_to_latest_self_contained(monkeypatch):
    forward = AsyncMock(return_value={})
    monkeypatch.setattr(api_client, "forward_inbound", forward)
    _seed(
        42,
        (1, "Здравствуйте, есть стрижка?"),
        (2, "Хочу записаться на завтра пожалуйста"),
    )

    await bot_main._flush_offline_backlog_after_debounce(chat_id=42)

    forward.assert_awaited_once()
    kwargs = forward.await_args.kwargs
    assert kwargs["text"] == "Хочу записаться на завтра пожалуйста"
    assert kwargs["trace_id"] == "tg-update-2"


@pytest.mark.asyncio
async def test_flush_thin_latest_includes_preceding_context(monkeypatch):
    forward = AsyncMock(return_value={})
    monkeypatch.setattr(api_client, "forward_inbound", forward)
    _seed(
        43,
        (1, "Здравствуйте, у вас есть мужская стрижка?"),
        (2, "да"),
    )

    await bot_main._flush_offline_backlog_after_debounce(chat_id=43)

    text = forward.await_args.kwargs["text"]
    assert "Предыдущие сообщения (контекст):" in text
    assert "- Здравствуйте, у вас есть мужская стрижка?" in text
    assert text.endswith("Вопрос клиента: да")


@pytest.mark.asyncio
async def test_flush_thin_latest_respects_max_context(monkeypatch):
    monkeypatch.setattr(bot_main.settings, "offline_backlog_max_context_messages", 2)
    forward = AsyncMock(return_value={})
    monkeypatch.setattr(api_client, "forward_inbound", forward)
    _seed(
        44,
        (1, "первое сообщение клиента"),
        (2, "второе сообщение клиента"),
        (3, "третье сообщение клиента"),
        (4, "да"),
    )

    await bot_main._flush_offline_backlog_after_debounce(chat_id=44)

    text = forward.await_args.kwargs["text"]
    assert "первое сообщение клиента" not in text
    assert "второе сообщение клиента" in text
    assert "третье сообщение клиента" in text


@pytest.mark.asyncio
async def test_flush_thin_latest_with_zero_max_context_has_no_context(monkeypatch):
    monkeypatch.setattr(bot_main.settings, "offline_backlog_max_context_messages", 0)
    forward = AsyncMock(return_value={})
    monkeypatch.setattr(api_client, "forward_inbound", forward)
    _seed(45, (1, "первое сообщение клиента"), (2, "да"))

    await bot_main._flush_offline_backlog_after_debounce(chat_id=45)

    assert forward.await_args.kwargs["text"] == "да"


@pytest.mark.asyncio
async def test_flush_empty_buffer_is_noop(monkeypatch):
    forward = AsyncMock(return_value={})
    monkeypatch.setattr(api_client, "forward_inbound", forward)

    await bot_main._flush_offline_backlog_after_debounce(chat_id=999)

    forward.assert_not_awaited()


@pytest.mark.asyncio
async def test_flush_drain_empty_after_settling_is_noop(monkeypatch):
    forward = AsyncMock(return_value={})
    monkeypatch.setattr(api_client, "forward_inbound", forward)
    _seed(46, (1, "одно сообщение здесь"))
    # Another flusher won the drain: latest_received_at sees the row, drain
    # returns nothing.
    monkeypatch.setattr(
        bot_main.offline_backlog_buffer, "drain", lambda *, chat_id: []
    )

    await bot_main._flush_offline_backlog_after_debounce(chat_id=46)

    forward.assert_not_awaited()


@pytest.mark.asyncio
async def test_flush_settling_cap_is_hit(monkeypatch):
    monkeypatch.setattr(bot_main.settings, "offline_backlog_debounce_seconds", 9999.0)
    monkeypatch.setattr(bot_main.settings, "offline_backlog_settling_cap_seconds", 0.0)
    forward = AsyncMock(return_value={})
    monkeypatch.setattr(api_client, "forward_inbound", forward)
    _seed(47, (1, "сообщение клиента которое не утихает"))

    await bot_main._flush_offline_backlog_after_debounce(chat_id=47)

    # Cap forces the drain even though the chat was never quiet.
    forward.assert_awaited_once()


@pytest.mark.asyncio
async def test_startup_recovery_flushes_pending_chats(monkeypatch):
    monkeypatch.setattr(bot_main.settings, "offline_backlog_debounce_seconds", 0.0)
    forward = AsyncMock(return_value={})
    monkeypatch.setattr(api_client, "forward_inbound", forward)
    _seed(70, (1, "сообщение клиента для восстановления"))
    _seed(71, (2, "другое сообщение клиента здесь"))

    await bot_main._recover_offline_backlog_on_startup()
    # The handler fires-and-forgets via create_task; await the tracked tasks.
    await asyncio.gather(*list(bot_main._offline_recovery_tasks))

    assert forward.await_count == 2
    assert bot_main.offline_backlog_buffer.pending_chat_ids() == []


@pytest.mark.asyncio
async def test_startup_recovery_noop_when_buffer_empty(monkeypatch):
    forward = AsyncMock(return_value={})
    monkeypatch.setattr(api_client, "forward_inbound", forward)

    await bot_main._recover_offline_backlog_on_startup()

    forward.assert_not_awaited()


def test_stale_platform_admin_is_not_forwarded(monkeypatch):
    monkeypatch.setattr(bot_main.settings, "admin_telegram_username", "@admin")
    monkeypatch.setattr(bot_main.settings, "hitl_config_admin_username", "@admin")
    monkeypatch.setattr(bot_main.settings, "telegram_alert_chat_id", "5550")

    async def _not_operator(**_kwargs):
        return None

    monkeypatch.setattr(bot_main, "resolve_operator_for_sender", _not_operator)
    forward = AsyncMock(return_value={})
    monkeypatch.setattr(api_client, "forward_inbound", forward)

    client = TestClient(bot_app)
    response = client.post(
        "/telegram/webhook",
        json=_stale_payload(
            update_id=8010,
            message_id=10,
            text="привет",
            chat_id=5550,
            username="admin",
        ),
    )

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ignored"
    assert body["reason"] == "platform_admin_not_customer"
    forward.assert_not_awaited()


def test_live_platform_admin_username_skips_customer_pipeline(monkeypatch):
    monkeypatch.setattr(bot_main.settings, "admin_telegram_username", "@admin")
    monkeypatch.setattr(bot_main.settings, "hitl_config_admin_username", "@admin")

    async def _not_operator(**_kwargs):
        return None

    monkeypatch.setattr(bot_main, "resolve_operator_for_sender", _not_operator)
    monkeypatch.setattr(bot_main, "persist_normalized_message", lambda **_: True)
    forward = AsyncMock(return_value={})
    monkeypatch.setattr(api_client, "forward_inbound", forward)

    client = TestClient(bot_app)
    response = client.post(
        "/telegram/webhook",
        json=_live_payload(
            update_id=8020,
            message_id=20,
            text="привет",
            chat_id=5551,
            username="admin",
        ),
    )

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ignored"
    assert body["reason"] == "platform_admin_not_customer"
    forward.assert_not_awaited()


@pytest.mark.asyncio
async def test_offline_backlog_flush_discards_platform_admin_chat(monkeypatch):
    monkeypatch.setattr(bot_main.settings, "telegram_alert_chat_id", "42")
    forward = AsyncMock(return_value={})
    monkeypatch.setattr(api_client, "forward_inbound", forward)
    _seed(42, (1, "привет"))

    await bot_main._flush_offline_backlog_after_debounce(chat_id=42)

    forward.assert_not_awaited()
    assert bot_main.offline_backlog_buffer.pending_chat_ids() == []


@pytest.mark.asyncio
async def test_flush_swallows_unexpected_errors(monkeypatch):
    forward = AsyncMock(return_value={})
    monkeypatch.setattr(api_client, "forward_inbound", forward)
    monkeypatch.setattr(
        bot_main.offline_backlog_buffer,
        "latest_received_at",
        Mock(side_effect=RuntimeError("boom")),
    )

    # Must not raise — a flush failure can never crash the event loop.
    await bot_main._flush_offline_backlog_after_debounce(chat_id=48)
    forward.assert_not_awaited()


def test_platform_admin_chat_id_none_when_unset(monkeypatch):
    monkeypatch.setattr(bot_main.settings, "telegram_alert_chat_id", None)
    assert bot_main._platform_admin_chat_id() is None


def test_platform_admin_chat_id_none_when_invalid(monkeypatch):
    monkeypatch.setattr(bot_main.settings, "telegram_alert_chat_id", "not-int")
    assert bot_main._platform_admin_chat_id() is None


def test_platform_admin_customer_skip_response_returns_none_for_non_admin():
    from services.bot_gateway.app.telegram_update import NormalizedTelegramMessage

    normalized = NormalizedTelegramMessage(
        update_id=1,
        source_message_id=1,
        chat_id=1,
        user_id=1,
        username="@customer",
        text="hi",
        date=datetime.now(UTC),
        caption=None,
        media_group_id=None,
        attachments=[],
    )
    assert (
        bot_main._platform_admin_customer_skip_response(
            trace_id="t1", normalized=normalized
        )
        is None
    )


def test_platform_admin_customer_skip_response_returns_none_for_empty_username():
    from services.bot_gateway.app.telegram_update import NormalizedTelegramMessage

    normalized = NormalizedTelegramMessage(
        update_id=3,
        source_message_id=3,
        chat_id=3,
        user_id=3,
        username="",
        text="hi",
        date=datetime.now(UTC),
        caption=None,
        media_group_id=None,
        attachments=[],
    )
    assert (
        bot_main._platform_admin_customer_skip_response(
            trace_id="t3", normalized=normalized
        )
        is None
    )


def test_platform_admin_customer_skip_response_for_admin_username(monkeypatch):
    from services.bot_gateway.app.telegram_update import NormalizedTelegramMessage

    monkeypatch.setattr(bot_main.settings, "admin_telegram_username", "@admin")
    monkeypatch.setattr(bot_main.settings, "hitl_config_admin_username", "@admin")
    normalized = NormalizedTelegramMessage(
        update_id=2,
        source_message_id=2,
        chat_id=9,
        user_id=9,
        username="@admin",
        text="hi",
        date=datetime.now(UTC),
        caption=None,
        media_group_id=None,
        attachments=[],
    )
    result = bot_main._platform_admin_customer_skip_response(
        trace_id="t2", normalized=normalized
    )
    assert result == {
        "status": "ignored",
        "reason": "platform_admin_not_customer",
        "trace_id": "t2",
    }


@pytest.mark.asyncio
async def test_process_telegram_update_returns_platform_admin_skip(monkeypatch):
    monkeypatch.setattr(bot_main.settings, "admin_telegram_username", "@admin")
    monkeypatch.setattr(bot_main.settings, "hitl_config_admin_username", "@admin")

    async def _not_operator(**_kwargs):
        return None

    monkeypatch.setattr(bot_main, "resolve_operator_for_sender", _not_operator)
    monkeypatch.setattr(bot_main, "persist_normalized_message", lambda **_: True)
    monkeypatch.setattr(
        bot_main,
        "dispatch_pending_prompt_edit",
        AsyncMock(return_value=None),
    )
    payload = _live_payload(
        update_id=99001,
        message_id=99,
        text="привет",
        chat_id=5552,
        username="admin",
    )
    result = await bot_main._process_telegram_update(
        payload, "trace-direct", BackgroundTasks()
    )
    assert result["status"] == "ignored"
    assert result["reason"] == "platform_admin_not_customer"

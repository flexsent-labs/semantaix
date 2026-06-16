from unittest.mock import AsyncMock

import pytest

from services.bot_gateway.app.callback_dispatch import dispatch_callback_query
from services.bot_gateway.app.telegram_callback import NormalizedCallbackQuery


@pytest.mark.asyncio
async def test_dispatch_callback_query_calls_namespace_handler_and_answers():
    normalized = NormalizedCallbackQuery(
        update_id=1,
        callback_query_id="cq-1",
        chat_id=10,
        sender_username="@admin",
        sender_user_id=7,
        data="op_reg:approve:5",
    )
    handler = AsyncMock(return_value={"decision": "approved", "answer_text": "Одобрено"})
    sender = AsyncMock()

    result = await dispatch_callback_query(
        normalized,
        handlers={"op_reg": handler},
        telegram_bot_sender=sender,
    )

    assert result["status"] == "processed"
    assert result["decision"] == "approved"
    handler.assert_awaited_once_with(normalized, "approve", "5")
    sender.answer_callback_query.assert_awaited_once_with(
        callback_query_id="cq-1",
        text="Одобрено",
    )


@pytest.mark.asyncio
async def test_dispatch_callback_query_answers_even_when_unhandled():
    normalized = NormalizedCallbackQuery(
        update_id=2,
        callback_query_id="cq-2",
        chat_id=10,
        sender_username="@user",
        sender_user_id=8,
        data="unknown:x:y",
    )
    sender = AsyncMock()

    result = await dispatch_callback_query(
        normalized,
        handlers={},
        telegram_bot_sender=sender,
    )

    assert result["decision"] == "unhandled_namespace"
    sender.answer_callback_query.assert_awaited_once_with(
        callback_query_id="cq-2",
        text="",
    )


@pytest.mark.asyncio
async def test_dispatch_callback_query_handler_noop():
    normalized = NormalizedCallbackQuery(
        update_id=4,
        callback_query_id="cq-4",
        chat_id=10,
        sender_username="@user",
        sender_user_id=8,
        data="op_reg:approve:1",
    )
    handler = AsyncMock(return_value=None)
    sender = AsyncMock()

    result = await dispatch_callback_query(
        normalized,
        handlers={"op_reg": handler},
        telegram_bot_sender=sender,
    )

    assert result["decision"] == "handler_noop"
    sender.answer_callback_query.assert_awaited_once()


@pytest.mark.asyncio
async def test_dispatch_callback_query_answer_failure_is_logged(caplog):
    normalized = NormalizedCallbackQuery(
        update_id=5,
        callback_query_id="cq-5",
        chat_id=10,
        sender_username="@user",
        sender_user_id=8,
        data="op_reg:approve:1",
    )
    handler = AsyncMock(return_value={"decision": "approved", "answer_text": "ok"})
    sender = AsyncMock()
    sender.answer_callback_query.side_effect = RuntimeError("telegram down")

    with caplog.at_level("WARNING", logger="services.bot_gateway.app.callback_dispatch"):
        result = await dispatch_callback_query(
            normalized,
            handlers={"op_reg": handler},
            telegram_bot_sender=sender,
        )

    assert result["decision"] == "approved"
    assert "callback_answer_failed" in caplog.text


@pytest.mark.asyncio
async def test_dispatch_callback_query_handles_malformed_data():
    normalized = NormalizedCallbackQuery(
        update_id=3,
        callback_query_id="cq-3",
        chat_id=10,
        sender_username="@user",
        sender_user_id=8,
        data="bad_payload",
    )
    sender = AsyncMock()

    result = await dispatch_callback_query(
        normalized,
        handlers={},
        telegram_bot_sender=sender,
    )

    assert result["namespace"] == "unknown"
    sender.answer_callback_query.assert_awaited_once()

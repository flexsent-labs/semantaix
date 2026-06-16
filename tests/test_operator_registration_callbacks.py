from unittest.mock import AsyncMock

import httpx
import pytest

from services.bot_gateway.app.api_client import ApiError
from services.bot_gateway.app.operator_registration_callbacks import (
    handle_operator_registration_callback,
)
from services.bot_gateway.app.telegram_callback import NormalizedCallbackQuery


def _callback(sender_username: str | None = "@admin") -> NormalizedCallbackQuery:
    return NormalizedCallbackQuery(
        update_id=1,
        callback_query_id="cq-1",
        chat_id=500,
        sender_username=sender_username,
        sender_user_id=99,
        data="op_reg:approve:1",
        source_message_id=777,
    )


def _api_error(detail: str) -> ApiError:
    request = httpx.Request("POST", "http://api")
    response = httpx.Response(409, request=request, json={"detail": detail})
    return ApiError("err", request=request, response=response, detail=detail)


@pytest.mark.asyncio
async def test_op_reg_callback_unauthorized_sender_is_ignored():
    api = AsyncMock()
    send_dm = AsyncMock()
    sender = AsyncMock()
    result = await handle_operator_registration_callback(
        _callback(sender_username="@not-admin"),
        "approve",
        "1",
        api_client=api,
        send_dm=send_dm,
        telegram_bot_sender=sender,
        admin_username="@admin",
    )
    assert result["reason"] == "unauthorized_op_reg_callback"
    api.approve_operator_register_request.assert_not_called()


@pytest.mark.asyncio
async def test_op_reg_callback_approve_success():
    api = AsyncMock()
    api.approve_operator_register_request.return_value = {"chat_id": 111}
    send_dm = AsyncMock()
    sender = AsyncMock()
    result = await handle_operator_registration_callback(
        _callback(),
        "approve",
        "42",
        api_client=api,
        send_dm=send_dm,
        telegram_bot_sender=sender,
        admin_username="@admin",
    )
    assert result["decision"] == "approved"
    send_dm.assert_awaited_once_with(111, "✓ Вы зарегистрированы как оператор.")
    sender.edit_message_reply_markup.assert_awaited_once()


@pytest.mark.asyncio
async def test_op_reg_callback_reject_success():
    api = AsyncMock()
    api.reject_operator_register_request.return_value = {"chat_id": 222}
    send_dm = AsyncMock()
    sender = AsyncMock()
    result = await handle_operator_registration_callback(
        _callback(),
        "reject",
        "42",
        api_client=api,
        send_dm=send_dm,
        telegram_bot_sender=sender,
        admin_username="@admin",
    )
    assert result["decision"] == "rejected"
    send_dm.assert_awaited_once()
    sender.edit_message_reply_markup.assert_awaited_once()


@pytest.mark.asyncio
async def test_op_reg_callback_request_not_pending_is_idempotent():
    api = AsyncMock()
    api.approve_operator_register_request.side_effect = _api_error("request_not_pending")
    send_dm = AsyncMock()
    sender = AsyncMock()
    result = await handle_operator_registration_callback(
        _callback(),
        "approve",
        "7",
        api_client=api,
        send_dm=send_dm,
        telegram_bot_sender=sender,
        admin_username="@admin",
    )
    assert result["decision"] == "already_processed"
    sender.edit_message_reply_markup.assert_awaited_once()


@pytest.mark.asyncio
async def test_op_reg_callback_approve_request_not_found():
    api = AsyncMock()
    api.approve_operator_register_request.side_effect = _api_error("request_not_found")
    send_dm = AsyncMock()
    sender = AsyncMock()
    result = await handle_operator_registration_callback(
        _callback(),
        "approve",
        "7",
        api_client=api,
        send_dm=send_dm,
        telegram_bot_sender=sender,
        admin_username="@admin",
    )
    assert result["reason"] == "request_not_found"


@pytest.mark.asyncio
async def test_op_reg_callback_approve_api_error():
    api = AsyncMock()
    api.approve_operator_register_request.side_effect = _api_error("server_error")
    send_dm = AsyncMock()
    sender = AsyncMock()
    result = await handle_operator_registration_callback(
        _callback(),
        "approve",
        "7",
        api_client=api,
        send_dm=send_dm,
        telegram_bot_sender=sender,
        admin_username="@admin",
    )
    assert result["decision"] == "api_error"


@pytest.mark.asyncio
async def test_op_reg_callback_reject_already_processed():
    api = AsyncMock()
    api.reject_operator_register_request.side_effect = _api_error("request_not_pending")
    send_dm = AsyncMock()
    sender = AsyncMock()
    result = await handle_operator_registration_callback(
        _callback(),
        "reject",
        "7",
        api_client=api,
        send_dm=send_dm,
        telegram_bot_sender=sender,
        admin_username="@admin",
    )
    assert result["decision"] == "already_processed"


@pytest.mark.asyncio
async def test_op_reg_callback_clear_markup_skipped_without_message_id():
    api = AsyncMock()
    api.approve_operator_register_request.return_value = {"chat_id": 111}
    send_dm = AsyncMock()
    sender = AsyncMock()
    normalized = NormalizedCallbackQuery(
        update_id=1,
        callback_query_id="cq-1",
        chat_id=500,
        sender_username="@admin",
        sender_user_id=99,
        data="op_reg:approve:1",
        source_message_id=None,
    )
    result = await handle_operator_registration_callback(
        normalized,
        "approve",
        "42",
        api_client=api,
        send_dm=send_dm,
        telegram_bot_sender=sender,
        admin_username="@admin",
    )
    assert result["decision"] == "approved"
    sender.edit_message_reply_markup.assert_not_called()


@pytest.mark.asyncio
async def test_op_reg_callback_invalid_id_and_unknown_action():
    api = AsyncMock()
    send_dm = AsyncMock()
    sender = AsyncMock()
    invalid = await handle_operator_registration_callback(
        _callback(),
        "approve",
        "bad",
        api_client=api,
        send_dm=send_dm,
        telegram_bot_sender=sender,
        admin_username="@admin",
    )
    assert invalid["reason"] == "invalid_request_id"

    unknown = await handle_operator_registration_callback(
        _callback(),
        "noop",
        "5",
        api_client=api,
        send_dm=send_dm,
        telegram_bot_sender=sender,
        admin_username="@admin",
    )
    assert unknown["reason"] == "unknown_action"

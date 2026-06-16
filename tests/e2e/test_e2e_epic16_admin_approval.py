"""Epic 16 — admin approve/reject callback paths (no live Telegram)."""

from __future__ import annotations

from unittest.mock import AsyncMock

import httpx
import pytest

from services.bot_gateway.app.api_client import ApiError
from services.bot_gateway.app.callback_dispatch import dispatch_callback_query
from services.bot_gateway.app.operator_registration_callbacks import (
    handle_operator_registration_callback,
)
from services.bot_gateway.app.telegram_callback import NormalizedCallbackQuery

pytestmark = [pytest.mark.e2e, pytest.mark.epic("16")]


def _callback(*, data: str, sender: str = "@admin") -> NormalizedCallbackQuery:
    return NormalizedCallbackQuery(
        update_id=1,
        callback_query_id="cq-admin",
        chat_id=500,
        sender_username=sender,
        sender_user_id=1,
        data=data,
        source_message_id=888,
    )


def _api_error(detail: str) -> ApiError:
    request = httpx.Request("POST", "http://api")
    response = httpx.Response(409, request=request, json={"detail": detail})
    return ApiError("err", request=request, response=response, detail=detail)


@pytest.mark.asyncio
async def test_epic16_admin_approve_callback_via_dispatch():
    api = AsyncMock()
    api.approve_operator_register_request.return_value = {"chat_id": 424242}
    send_dm = AsyncMock()
    sender = AsyncMock()

    async def handler(normalized, action, arg):
        return await handle_operator_registration_callback(
            normalized,
            action,
            arg,
            api_client=api,
            send_dm=send_dm,
            telegram_bot_sender=sender,
            admin_username="@admin",
        )

    result = await dispatch_callback_query(
        _callback(data="op_reg:approve:7"),
        handlers={"op_reg": handler},
        telegram_bot_sender=sender,
    )

    assert result["decision"] == "approved"
    api.approve_operator_register_request.assert_awaited_once_with(request_id=7)
    send_dm.assert_awaited_once_with(424242, "✓ Вы зарегистрированы как оператор.")
    sender.answer_callback_query.assert_awaited_once()


@pytest.mark.asyncio
async def test_epic16_admin_reject_callback_via_dispatch():
    api = AsyncMock()
    api.reject_operator_register_request.return_value = {"chat_id": 515151}
    send_dm = AsyncMock()
    sender = AsyncMock()

    async def handler(normalized, action, arg):
        return await handle_operator_registration_callback(
            normalized,
            action,
            arg,
            api_client=api,
            send_dm=send_dm,
            telegram_bot_sender=sender,
            admin_username="@admin",
        )

    result = await dispatch_callback_query(
        _callback(data="op_reg:reject:9"),
        handlers={"op_reg": handler},
        telegram_bot_sender=sender,
    )

    assert result["decision"] == "rejected"
    api.reject_operator_register_request.assert_awaited_once_with(request_id=9)
    send_dm.assert_awaited_once()
    sender.answer_callback_query.assert_awaited_once()


@pytest.mark.asyncio
async def test_epic16_admin_reject_not_found():
    api = AsyncMock()
    api.reject_operator_register_request.side_effect = _api_error("request_not_found")
    send_dm = AsyncMock()
    sender = AsyncMock()

    result = await handle_operator_registration_callback(
        _callback(data="op_reg:reject:99"),
        "reject",
        "99",
        api_client=api,
        send_dm=send_dm,
        telegram_bot_sender=sender,
        admin_username="@admin",
    )
    assert result["reason"] == "request_not_found"
    send_dm.assert_not_called()

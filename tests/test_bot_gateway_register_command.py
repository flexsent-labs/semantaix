from unittest.mock import AsyncMock

import httpx
import pytest

from services.bot_gateway.app.api_client import ApiError
from services.bot_gateway.app.operator_registration_commands import (
    handle_register_command,
    handle_start_command,
)
from services.bot_gateway.app.telegram_update import NormalizedTelegramMessage


def _normalized(text: str, username: str | None = "@newop") -> NormalizedTelegramMessage:
    return NormalizedTelegramMessage(
        update_id=1,
        source_message_id=10,
        chat_id=100,
        user_id=55,
        username=username,
        text=text,
    )


def _api_error(detail: str) -> ApiError:
    request = httpx.Request("POST", "http://api/operators/register-request")
    response = httpx.Response(409, request=request, json={"detail": detail})
    return ApiError("err", request=request, response=response, detail=detail)


@pytest.mark.asyncio
async def test_start_command_ignores_non_start_text():
    send_dm = AsyncMock()
    assert (
        await handle_start_command(
            normalized=_normalized("hello"),
            is_operator=False,
            is_platform_admin=False,
            send_dm=send_dm,
        )
        is None
    )
    send_dm.assert_not_called()


@pytest.mark.asyncio
async def test_start_command_sends_guest_copy():
    send_dm = AsyncMock()
    result = await handle_start_command(
        normalized=_normalized("/start"),
        is_operator=False,
        is_platform_admin=False,
        send_dm=send_dm,
    )
    assert result is not None
    assert result["route"] == "start_command"
    send_dm.assert_awaited_once()
    assert "/register" in send_dm.await_args.args[1]


@pytest.mark.asyncio
async def test_register_command_blocks_platform_admin():
    api = AsyncMock()
    send_dm = AsyncMock()
    result = await handle_register_command(
        normalized=_normalized("/register", username="@ajdevy"),
        is_operator=False,
        is_platform_admin=True,
        api_client=api,
        send_dm=send_dm,
    )
    assert result is not None
    assert result["decision"] == "platform_admin"
    api.create_operator_register_request.assert_not_called()
    send_dm.assert_awaited_once()


@pytest.mark.asyncio
async def test_start_command_sends_admin_copy():
    send_dm = AsyncMock()
    result = await handle_start_command(
        normalized=_normalized("/start", username="@ajdevy"),
        is_operator=False,
        is_platform_admin=True,
        send_dm=send_dm,
    )
    assert result is not None
    assert result["route"] == "start_command"
    assert "администратор" in send_dm.await_args.args[1]


@pytest.mark.asyncio
async def test_register_command_ignores_non_register_text():
    api = AsyncMock()
    send_dm = AsyncMock()
    assert (
        await handle_register_command(
            normalized=_normalized("hello"),
            is_operator=False,
            is_platform_admin=False,
            api_client=api,
            send_dm=send_dm,
        )
        is None
    )
    api.create_operator_register_request.assert_not_called()


@pytest.mark.asyncio
async def test_register_command_blocks_existing_operator():
    api = AsyncMock()
    send_dm = AsyncMock()
    result = await handle_register_command(
        normalized=_normalized("/register"),
        is_operator=True,
        is_platform_admin=False,
        api_client=api,
        send_dm=send_dm,
    )
    assert result is not None
    assert result["decision"] == "already_operator"
    api.create_operator_register_request.assert_not_called()
    send_dm.assert_awaited_once()


@pytest.mark.asyncio
async def test_register_command_creates_request_and_parses_display_name():
    api = AsyncMock()
    api.create_operator_register_request.return_value = {"request_id": 12, "status": "pending"}
    send_dm = AsyncMock()

    result = await handle_register_command(
        normalized=_normalized("/register Иван Петров"),
        is_operator=False,
        is_platform_admin=False,
        api_client=api,
        send_dm=send_dm,
    )

    assert result is not None
    assert result["decision"] == "created"
    api.create_operator_register_request.assert_awaited_once_with(
        username="@newop",
        chat_id=100,
        display_name="Иван Петров",
    )
    send_dm.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "detail,decision",
    [
        ("registration_pending", "pending"),
        ("registration_cooldown", "cooldown"),
        ("already_operator", "already_operator"),
        ("other_error", "api_error"),
    ],
)
async def test_register_command_maps_api_errors(detail: str, decision: str):
    api = AsyncMock()
    api.create_operator_register_request.side_effect = _api_error(detail)
    send_dm = AsyncMock()
    result = await handle_register_command(
        normalized=_normalized("/register"),
        is_operator=False,
        is_platform_admin=False,
        api_client=api,
        send_dm=send_dm,
    )
    assert result is not None
    assert result["decision"] == decision
    send_dm.assert_awaited_once()


@pytest.mark.asyncio
async def test_register_command_maps_platform_admin_api_error():
    api = AsyncMock()
    api.create_operator_register_request.side_effect = _api_error(
        "platform_admin_not_operator"
    )
    send_dm = AsyncMock()
    result = await handle_register_command(
        normalized=_normalized("/register"),
        is_operator=False,
        is_platform_admin=False,
        api_client=api,
        send_dm=send_dm,
    )
    assert result is not None
    assert result["decision"] == "platform_admin"
    send_dm.assert_awaited_once()


@pytest.mark.asyncio
async def test_register_command_handles_missing_username_and_network_error():
    api = AsyncMock()
    send_dm = AsyncMock()
    result = await handle_register_command(
        normalized=_normalized("/register", username=None),
        is_operator=False,
        is_platform_admin=False,
        api_client=api,
        send_dm=send_dm,
    )
    assert result is not None
    assert result["decision"] == "missing_username"

    api.create_operator_register_request.side_effect = httpx.RequestError(
        "boom", request=httpx.Request("POST", "http://api")
    )
    send_dm.reset_mock()
    result = await handle_register_command(
        normalized=_normalized("/register"),
        is_operator=False,
        is_platform_admin=False,
        api_client=api,
        send_dm=send_dm,
    )
    assert result is not None
    assert result["decision"] == "api_error"
    send_dm.assert_awaited_once()

import base64
from unittest.mock import AsyncMock

import httpx
import pytest

from services.bot_gateway.app.operator_telegram_link import start_operator_telegram_link
from services.bot_gateway.app.user_gateway_client import UserGatewayError


@pytest.mark.asyncio
async def test_operator_telegram_link_happy_path(monkeypatch):
    user_gateway = AsyncMock()
    user_gateway.qr_start.return_value = {
        "qr_image_b64": base64.b64encode(b"png").decode("ascii")
    }
    user_gateway.status.side_effect = [{"phase": "qr_pending"}, {"phase": "authenticated"}]
    send_dm = AsyncMock()
    sender = AsyncMock()
    api = AsyncMock()

    sleep_mock = AsyncMock()
    monkeypatch.setattr("services.bot_gateway.app.operator_telegram_link.asyncio.sleep", sleep_mock)

    result = await start_operator_telegram_link(
        operator_id=5,
        operator_chat_id=100,
        user_gateway_client=user_gateway,
        send_dm=send_dm,
        telegram_bot_sender=sender,
        api_client=api,
        poll_interval_seconds=0.01,
        max_polls=2,
    )

    assert result["decision"] == "connected"
    sender.send_document.assert_awaited_once()
    api.record_onboarding_event.assert_awaited_once_with(
        operator_id=5,
        event_type="telegram_link_connected",
    )
    assert send_dm.await_count >= 1


@pytest.mark.asyncio
async def test_operator_telegram_link_2fa_wrong_password_then_success(monkeypatch):
    user_gateway = AsyncMock()
    user_gateway.qr_start.return_value = {
        "qr_image_b64": base64.b64encode(b"png").decode("ascii")
    }
    user_gateway.status.side_effect = [
        {"phase": "2fa_pending"},
        {"phase": "2fa_pending"},
        {"phase": "authenticated"},
    ]
    request = httpx.Request("POST", "http://ug/auth/verify_2fa")
    response = httpx.Response(401, request=request, json={"detail": "invalid_password"})
    user_gateway.verify_2fa.side_effect = [
        UserGatewayError("err", request=request, response=response, detail="invalid_password"),
        {"status": "authenticated"},
    ]
    send_dm = AsyncMock()
    sender = AsyncMock()
    api = AsyncMock()

    passwords = ["bad", "good"]

    async def _password_provider():
        return passwords.pop(0)

    sleep_mock = AsyncMock()
    monkeypatch.setattr("services.bot_gateway.app.operator_telegram_link.asyncio.sleep", sleep_mock)

    result = await start_operator_telegram_link(
        operator_id=5,
        operator_chat_id=100,
        user_gateway_client=user_gateway,
        send_dm=send_dm,
        telegram_bot_sender=sender,
        api_client=api,
        poll_interval_seconds=0.01,
        max_polls=3,
        get_2fa_password=_password_provider,
    )
    assert result["decision"] == "connected"
    assert user_gateway.verify_2fa.await_count == 2


@pytest.mark.asyncio
async def test_operator_telegram_link_timeout(monkeypatch):
    user_gateway = AsyncMock()
    user_gateway.qr_start.return_value = {
        "qr_image_b64": base64.b64encode(b"png").decode("ascii")
    }
    user_gateway.status.return_value = {"phase": "qr_pending"}
    send_dm = AsyncMock()
    sender = AsyncMock()

    sleep_mock = AsyncMock()
    monkeypatch.setattr("services.bot_gateway.app.operator_telegram_link.asyncio.sleep", sleep_mock)

    result = await start_operator_telegram_link(
        operator_id=5,
        operator_chat_id=100,
        user_gateway_client=user_gateway,
        send_dm=send_dm,
        telegram_bot_sender=sender,
        poll_interval_seconds=0.01,
        max_polls=2,
    )
    assert result["decision"] == "timeout"
    assert send_dm.await_count >= 1

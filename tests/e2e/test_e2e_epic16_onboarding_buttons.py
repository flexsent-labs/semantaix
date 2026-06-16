"""Epic 16 — onboarding inline buttons (calendar + Telegram link)."""

from __future__ import annotations

from unittest.mock import AsyncMock

import httpx
import pytest

from services.bot_gateway.app.api_client import ApiError
from services.bot_gateway.app.onboarding_callbacks import handle_onboarding_callback
from services.bot_gateway.app.telegram_callback import NormalizedCallbackQuery

pytestmark = [pytest.mark.e2e, pytest.mark.epic("16")]


def _callback(action: str, operator_id: str, username: str = "@op") -> NormalizedCallbackQuery:
    return NormalizedCallbackQuery(
        update_id=1,
        callback_query_id="cq-onboard",
        chat_id=100,
        sender_username=username,
        sender_user_id=10,
        data=f"onboard:{action}:{operator_id}",
    )


@pytest.mark.asyncio
async def test_epic16_calendar_button_sends_consent_url():
    api = AsyncMock()
    api.get_operator_by_id.return_value = {
        "id": 5,
        "username": "@op",
        "project_id": 1,
        "chat_id": 100,
    }
    api.initiate_calendar_connect.return_value = {
        "consent_url": "https://calendar.example/oauth"
    }
    send_dm = AsyncMock()
    sender = AsyncMock()

    result = await handle_onboarding_callback(
        _callback("cal", "5"),
        "cal",
        "5",
        api_client=api,
        user_gateway_client=AsyncMock(),
        send_dm=send_dm,
        telegram_bot_sender=sender,
        internal_token="tok",
    )

    assert result["decision"] == "calendar_started"
    api.record_onboarding_event.assert_awaited_once_with(
        operator_id=5,
        event_type="calendar_started",
    )
    send_dm.assert_awaited_once()
    assert "https://calendar.example/oauth" in send_dm.await_args.args[1]


@pytest.mark.asyncio
async def test_epic16_telegram_button_starts_qr_link(monkeypatch):
    api = AsyncMock()
    api.get_operator_by_id.return_value = {
        "id": 5,
        "username": "@op",
        "project_id": 1,
        "chat_id": 101,
    }
    started = AsyncMock(return_value={"decision": "connected"})
    monkeypatch.setattr(
        "services.bot_gateway.app.onboarding_callbacks.start_operator_telegram_link",
        started,
    )
    send_dm = AsyncMock()
    sender = AsyncMock()

    result = await handle_onboarding_callback(
        _callback("tg", "5"),
        "tg",
        "5",
        api_client=api,
        user_gateway_client=AsyncMock(),
        send_dm=send_dm,
        telegram_bot_sender=sender,
        internal_token="tok",
    )

    assert result["decision"] == "connected"
    api.record_onboarding_event.assert_awaited_once_with(
        operator_id=5,
        event_type="telegram_link_started",
    )
    started.assert_awaited_once()


@pytest.mark.asyncio
async def test_epic16_calendar_connect_failure_sends_fallback():
    api = AsyncMock()
    api.get_operator_by_id.return_value = {
        "id": 5,
        "username": "@op",
        "project_id": 1,
        "chat_id": 100,
    }
    request = httpx.Request("POST", "http://api")
    response = httpx.Response(500, request=request)
    api.initiate_calendar_connect.side_effect = ApiError(
        "fail", request=request, response=response, detail="calendar_down"
    )
    send_dm = AsyncMock()

    result = await handle_onboarding_callback(
        _callback("cal", "5"),
        "cal",
        "5",
        api_client=api,
        user_gateway_client=AsyncMock(),
        send_dm=send_dm,
        telegram_bot_sender=AsyncMock(),
        internal_token="tok",
    )

    assert result["decision"] == "calendar_connect_failed"
    send_dm.assert_awaited_once()

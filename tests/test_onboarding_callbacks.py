from unittest.mock import AsyncMock

import pytest

from services.bot_gateway.app.onboarding_callbacks import handle_onboarding_callback
from services.bot_gateway.app.telegram_callback import NormalizedCallbackQuery


def _callback(username: str = "@op") -> NormalizedCallbackQuery:
    return NormalizedCallbackQuery(
        update_id=1,
        callback_query_id="cq",
        chat_id=100,
        sender_username=username,
        sender_user_id=10,
        data="onboard:cal:5",
    )


@pytest.mark.asyncio
async def test_onboarding_calendar_flow_records_event_and_sends_url():
    api = AsyncMock()
    api.get_operator_by_id.return_value = {
        "id": 5,
        "username": "@op",
        "project_id": 77,
        "chat_id": 100,
    }
    api.initiate_calendar_connect.return_value = {"consent_url": "https://example.test/oauth"}
    user_gateway = AsyncMock()
    send_dm = AsyncMock()
    sender = AsyncMock()

    result = await handle_onboarding_callback(
        _callback(),
        "cal",
        "5",
        api_client=api,
        user_gateway_client=user_gateway,
        send_dm=send_dm,
        telegram_bot_sender=sender,
        internal_token="tok",
    )

    assert result["decision"] == "calendar_started"
    api.record_onboarding_event.assert_awaited_once_with(
        operator_id=5, event_type="calendar_started"
    )
    send_dm.assert_awaited_once()


@pytest.mark.asyncio
async def test_onboarding_invalid_operator_id():
    result = await handle_onboarding_callback(
        _callback(),
        "cal",
        "bad",
        api_client=AsyncMock(),
        user_gateway_client=AsyncMock(),
        send_dm=AsyncMock(),
        telegram_bot_sender=AsyncMock(),
        internal_token="tok",
    )
    assert result["reason"] == "invalid_operator_id"


@pytest.mark.asyncio
async def test_onboarding_operator_not_found():
    api = AsyncMock()
    api.get_operator_by_id.return_value = None
    result = await handle_onboarding_callback(
        _callback(),
        "cal",
        "5",
        api_client=api,
        user_gateway_client=AsyncMock(),
        send_dm=AsyncMock(),
        telegram_bot_sender=AsyncMock(),
        internal_token="tok",
    )
    assert result["reason"] == "operator_not_found"


@pytest.mark.asyncio
async def test_onboarding_unknown_action():
    api = AsyncMock()
    api.get_operator_by_id.return_value = {
        "id": 5,
        "username": "@op",
        "project_id": 77,
        "chat_id": 100,
    }
    result = await handle_onboarding_callback(
        _callback(),
        "noop",
        "5",
        api_client=api,
        user_gateway_client=AsyncMock(),
        send_dm=AsyncMock(),
        telegram_bot_sender=AsyncMock(),
        internal_token="tok",
    )
    assert result["reason"] == "unknown_action"


@pytest.mark.asyncio
async def test_onboarding_calendar_missing_operator_fields():
    api = AsyncMock()
    api.get_operator_by_id.return_value = {"id": 5, "username": "@op", "project_id": None}
    send_dm = AsyncMock()
    result = await handle_onboarding_callback(
        _callback(),
        "cal",
        "5",
        api_client=api,
        user_gateway_client=AsyncMock(),
        send_dm=send_dm,
        telegram_bot_sender=AsyncMock(),
        internal_token="tok",
    )
    assert result["decision"] == "calendar_missing_operator_fields"
    send_dm.assert_awaited_once()


@pytest.mark.asyncio
async def test_onboarding_tg_missing_chat_id():
    api = AsyncMock()
    api.get_operator_by_id.return_value = {
        "id": 5,
        "username": "@op",
        "project_id": 77,
        "chat_id": None,
    }
    send_dm = AsyncMock()
    result = await handle_onboarding_callback(
        _callback(),
        "tg",
        "5",
        api_client=api,
        user_gateway_client=AsyncMock(),
        send_dm=send_dm,
        telegram_bot_sender=AsyncMock(),
        internal_token="tok",
    )
    assert result["decision"] == "telegram_missing_chat_id"
    send_dm.assert_awaited_once()


@pytest.mark.asyncio
async def test_onboarding_owner_mismatch_is_ignored():
    api = AsyncMock()
    api.get_operator_by_id.return_value = {"id": 5, "username": "@owner", "project_id": 77}
    user_gateway = AsyncMock()
    send_dm = AsyncMock()
    sender = AsyncMock()

    result = await handle_onboarding_callback(
        _callback(username="@other"),
        "cal",
        "5",
        api_client=api,
        user_gateway_client=user_gateway,
        send_dm=send_dm,
        telegram_bot_sender=sender,
        internal_token="tok",
    )
    assert result["reason"] == "onboarding_callback_owner_mismatch"


@pytest.mark.asyncio
async def test_onboarding_tg_flow_starts_link(monkeypatch):
    api = AsyncMock()
    api.get_operator_by_id.return_value = {
        "id": 5,
        "username": "@op",
        "project_id": 77,
        "chat_id": 101,
    }
    user_gateway = AsyncMock()
    send_dm = AsyncMock()
    sender = AsyncMock()

    started = AsyncMock(return_value={"decision": "connected"})
    monkeypatch.setattr(
        "services.bot_gateway.app.onboarding_callbacks.start_operator_telegram_link",
        started,
    )

    result = await handle_onboarding_callback(
        _callback(),
        "tg",
        "5",
        api_client=api,
        user_gateway_client=user_gateway,
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

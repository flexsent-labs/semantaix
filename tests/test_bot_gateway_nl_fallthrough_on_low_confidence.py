"""Low-confidence classification → handler returns ``None`` (falls through).

The LLM is instructed to set ``action: null`` when uncertain. The dispatch
handler MUST return ``None`` (not a result dict) so the caller in
bot_gateway/main.py keeps walking the pipeline and the message is treated as
an ordinary operator chatter / customer-shaped DM.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from services.bot_gateway.app.operator_service_nl import (
    handle_operator_service_nl_message,
)
from services.bot_gateway.app.telegram_update import NormalizedTelegramMessage


def _msg(text: str) -> NormalizedTelegramMessage:
    return NormalizedTelegramMessage(
        update_id=1,
        source_message_id=2,
        chat_id=42,
        user_id=99,
        username="@op",
        text=text,
    )


class _Sent:
    def __init__(self) -> None:
        self.calls: list[tuple[int, str]] = []

    async def __call__(self, chat_id: int, text: str) -> None:
        self.calls.append((chat_id, text))


class FakeApi:
    def __init__(self) -> None:
        self.find_operator_by_username = AsyncMock(
            return_value={
                "username": "@op",
                "chat_id": 42,
                "project_id": 7,
                "is_active": True,
            }
        )
        self.add_sales_service = AsyncMock()
        self.list_sales_services = AsyncMock()
        self.delete_sales_service = AsyncMock()


class _FakeOpenRouter:
    def __init__(self, payload: dict) -> None:
        self.complete_json = AsyncMock(return_value=payload)


@pytest.mark.asyncio
async def test_action_null_returns_none_and_does_not_dm_or_call_api() -> None:
    api = FakeApi()
    sent = _Sent()
    openrouter = _FakeOpenRouter({"action": None})
    result = await handle_operator_service_nl_message(
        normalized=_msg("привет, как дела?"),
        api_client=api,
        send_dm=sent,
        openrouter=openrouter,
        primary_operator_username="@op",
        admin_username="@admin",
        internal_token="bot-tok",
    )
    assert result is None
    assert sent.calls == []
    api.add_sales_service.assert_not_awaited()
    api.delete_sales_service.assert_not_awaited()
    api.list_sales_services.assert_not_awaited()


@pytest.mark.asyncio
async def test_empty_text_returns_none_without_llm_call() -> None:
    api = FakeApi()
    sent = _Sent()
    openrouter = _FakeOpenRouter({"action": None})
    result = await handle_operator_service_nl_message(
        normalized=_msg(""),
        api_client=api,
        send_dm=sent,
        openrouter=openrouter,
        primary_operator_username="@op",
        admin_username="@admin",
        internal_token="bot-tok",
    )
    assert result is None
    openrouter.complete_json.assert_not_awaited()


@pytest.mark.asyncio
async def test_schema_violation_falls_through_silently() -> None:
    api = FakeApi()
    sent = _Sent()
    # A response missing required fields should be treated as a no-classify
    # and fall through, just like ``action: null``.
    openrouter = _FakeOpenRouter({"action": "add", "name": None})
    result = await handle_operator_service_nl_message(
        normalized=_msg("hm"),
        api_client=api,
        send_dm=sent,
        openrouter=openrouter,
        primary_operator_username="@op",
        admin_username="@admin",
        internal_token="bot-tok",
    )
    assert result is None
    assert sent.calls == []
    api.add_sales_service.assert_not_awaited()

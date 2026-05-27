"""Unauthorized sender → no LLM call (cost-control).

The classifier runs on every operator inbound that didn't match a slash
command. Without an upstream auth check, every non-operator DM hitting the
bot would burn LLM tokens for no business value. The dispatch handler must
gate on the same operator registry the slash-command dispatcher uses, BEFORE
calling OpenRouter.
"""

from __future__ import annotations

import logging
from unittest.mock import AsyncMock

import pytest

from services.bot_gateway.app.operator_service_nl import (
    handle_operator_service_nl_message,
)
from services.bot_gateway.app.telegram_update import NormalizedTelegramMessage


def _msg(text: str, *, username: str | None) -> NormalizedTelegramMessage:
    return NormalizedTelegramMessage(
        update_id=1,
        source_message_id=2,
        chat_id=42,
        user_id=99,
        username=username,
        text=text,
    )


class _Sent:
    def __init__(self) -> None:
        self.calls: list[tuple[int, str]] = []

    async def __call__(self, chat_id: int, text: str) -> None:
        self.calls.append((chat_id, text))


class FakeApi:
    def __init__(self) -> None:
        # Unknown sender → registry returns None.
        self.find_operator_by_username = AsyncMock(return_value=None)
        self.add_sales_service = AsyncMock()
        self.list_sales_services = AsyncMock()
        self.delete_sales_service = AsyncMock()


class _SpyOpenRouter:
    """Tracks whether complete_json was ever invoked."""

    def __init__(self) -> None:
        self.complete_json = AsyncMock(return_value={"action": "list"})


@pytest.mark.asyncio
async def test_unauthorized_sender_does_not_call_llm_or_api_or_dm(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.WARNING)
    api = FakeApi()
    sent = _Sent()
    openrouter = _SpyOpenRouter()
    result = await handle_operator_service_nl_message(
        normalized=_msg("какие у нас услуги?", username="@stranger"),
        api_client=api,
        send_dm=sent,
        openrouter=openrouter,
        primary_operator_username="@op",
        admin_username="@admin",
        internal_token="bot-tok",
    )
    assert result is None
    assert sent.calls == []
    openrouter.complete_json.assert_not_awaited()
    api.add_sales_service.assert_not_awaited()
    api.list_sales_services.assert_not_awaited()
    api.delete_sales_service.assert_not_awaited()
    events = [r for r in caplog.records if r.message == "unauthorized_service_nl"]
    assert events, "expected unauthorized_service_nl log line"
    assert getattr(events[0], "from_username") == "@stranger"


@pytest.mark.asyncio
async def test_missing_username_does_not_call_llm() -> None:
    api = FakeApi()
    sent = _Sent()
    openrouter = _SpyOpenRouter()
    result = await handle_operator_service_nl_message(
        normalized=_msg("добавь услугу X", username=None),
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
async def test_inactive_operator_does_not_call_llm() -> None:
    api = FakeApi()
    api.find_operator_by_username = AsyncMock(
        return_value={
            "username": "@op",
            "chat_id": 42,
            "project_id": 7,
            "is_active": False,
        }
    )
    sent = _Sent()
    openrouter = _SpyOpenRouter()
    result = await handle_operator_service_nl_message(
        normalized=_msg("какие у нас услуги?", username="@op"),
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
async def test_admin_without_project_id_is_ignored() -> None:
    """Defensive: admin with no operator-registry row → can't route to a
    project. We ignore rather than crash."""
    api = FakeApi()
    api.find_operator_by_username = AsyncMock(return_value=None)
    sent = _Sent()
    openrouter = _SpyOpenRouter()
    result = await handle_operator_service_nl_message(
        normalized=_msg("какие у нас услуги?", username="@admin"),
        api_client=api,
        send_dm=sent,
        openrouter=openrouter,
        primary_operator_username="@op",
        admin_username="@admin",
        internal_token="bot-tok",
    )
    assert result is None
    openrouter.complete_json.assert_not_awaited()

"""Slash command takes precedence over NL classifier.

Slash dispatcher runs before the NL handler in ``main.py``. As a regression
guard, the NL handler itself MUST also short-circuit on slash-prefixed
text without burning an LLM call — the spy on the OpenRouter client must
record zero invocations.
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


class _SpyOpenRouter:
    def __init__(self) -> None:
        self.complete_json = AsyncMock(return_value={"action": "add", "name": "x"})


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "slash_text",
    [
        "/service_add Медовеевка Лайт",
        "/service_remove 12",
        "/service_list",
        "/sales_state",
        "/kb_add",
        "/help",
        " /service_add x",  # leading whitespace still counts as slash
    ],
)
async def test_slash_prefixed_text_short_circuits_without_llm(slash_text: str) -> None:
    api = FakeApi()
    sent = _Sent()
    openrouter = _SpyOpenRouter()
    result = await handle_operator_service_nl_message(
        normalized=_msg(slash_text),
        api_client=api,
        send_dm=sent,
        openrouter=openrouter,
        primary_operator_username="@op",
        admin_username="@admin",
        internal_token="bot-tok",
    )
    assert result is None
    openrouter.complete_json.assert_not_awaited()
    assert sent.calls == []
    api.add_sales_service.assert_not_awaited()


@pytest.mark.asyncio
async def test_non_slash_text_still_invokes_llm() -> None:
    """Sanity check that the slash short-circuit is anchored at the start
    only — a phrase that happens to contain a slash mid-text still hits the
    LLM (the slash is part of the operator's free-text content)."""
    api = FakeApi()
    api.add_sales_service = AsyncMock(return_value={"id": 1})
    sent = _Sent()
    openrouter = _SpyOpenRouter()
    result = await handle_operator_service_nl_message(
        normalized=_msg("добавь услугу A/B testing"),
        api_client=api,
        send_dm=sent,
        openrouter=openrouter,
        primary_operator_username="@op",
        admin_username="@admin",
        internal_token="bot-tok",
    )
    assert result is not None
    openrouter.complete_json.assert_awaited_once()

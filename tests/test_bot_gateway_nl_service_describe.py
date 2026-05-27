"""NL → describe: soft-delete + add against an existing service.

Story 12.02 explicitly disallows in-place edit of a service. Story 12.02b's
``describe`` op-type is a sugar over (delete + add) — semantically an upsert
in v1. The handler MUST:

1. Look up the existing service id by name (via the same ``list_sales_services``
   call the slash path uses).
2. ``delete_sales_service`` it (soft delete on the api side).
3. ``add_sales_service`` again with the new description.
4. DM ``Обновлено: <name> (id=<new_id>)`` — the new id is what the api just
   returned, not the old id.
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
    def __init__(self, *, existing_services: list[dict] | None = None) -> None:
        self.find_operator_by_username = AsyncMock(
            return_value={
                "username": "@op",
                "chat_id": 42,
                "project_id": 7,
                "is_active": True,
            }
        )
        self.list_sales_services = AsyncMock(
            return_value={"services": existing_services or []}
        )
        self.add_sales_service = AsyncMock(return_value={"id": 13})
        self.delete_sales_service = AsyncMock(return_value={"ok": True})


class _FakeOpenRouter:
    def __init__(self, payload: dict) -> None:
        self.complete_json = AsyncMock(return_value=payload)


@pytest.mark.asyncio
async def test_describe_existing_service_soft_deletes_and_adds_with_new_description() -> None:
    api = FakeApi(
        existing_services=[
            {
                "id": 12,
                "project_id": 7,
                "name": "каньонинг",
                "description_md": "старое описание",
                "is_active": True,
            }
        ]
    )
    sent = _Sent()
    openrouter = _FakeOpenRouter(
        {
            "action": "describe",
            "name": "каньонинг",
            "description": "спуск по верёвке",
        }
    )
    result = await handle_operator_service_nl_message(
        normalized=_msg("опиши каньонинг как спуск по верёвке"),
        api_client=api,
        send_dm=sent,
        openrouter=openrouter,
        primary_operator_username="@op",
        admin_username="@admin",
        internal_token="bot-tok",
    )
    assert result is not None
    assert result["status"] == "ok"
    assert result["route"] == "service_describe"
    assert result["service_id"] == "13"
    # Soft-delete the old row.
    api.delete_sales_service.assert_awaited_once_with(
        service_id=12, internal_token="bot-tok"
    )
    # Add a fresh row with the new description.
    api.add_sales_service.assert_awaited_once_with(
        project_id=7,
        name="каньонинг",
        description_md="спуск по верёвке",
        tags=None,
        internal_token="bot-tok",
    )
    assert sent.calls == [(42, "Обновлено: каньонинг (id=13)")]


@pytest.mark.asyncio
async def test_describe_nonexistent_service_dms_not_found_and_does_not_create() -> None:
    """A describe on a service that doesn't exist is NOT a covert add. We
    want the operator to use ``добавь`` for new services so they intend the
    creation; ``опиши`` only applies to existing rows."""
    api = FakeApi(existing_services=[])
    sent = _Sent()
    openrouter = _FakeOpenRouter(
        {"action": "describe", "name": "новое", "description": "x"}
    )
    result = await handle_operator_service_nl_message(
        normalized=_msg("опиши новое как x"),
        api_client=api,
        send_dm=sent,
        openrouter=openrouter,
        primary_operator_username="@op",
        admin_username="@admin",
        internal_token="bot-tok",
    )
    assert result is not None
    assert result["status"] == "error"
    assert result["route"] == "service_describe"
    api.delete_sales_service.assert_not_awaited()
    api.add_sales_service.assert_not_awaited()
    assert sent.calls == [(42, "Не найдено: новое")]


@pytest.mark.asyncio
async def test_describe_missing_description_dms_usage_and_does_nothing() -> None:
    api = FakeApi(
        existing_services=[
            {"id": 12, "project_id": 7, "name": "каньонинг", "is_active": True}
        ]
    )
    sent = _Sent()
    openrouter = _FakeOpenRouter(
        {"action": "describe", "name": "каньонинг", "description": None}
    )
    result = await handle_operator_service_nl_message(
        normalized=_msg("опиши каньонинг"),
        api_client=api,
        send_dm=sent,
        openrouter=openrouter,
        primary_operator_username="@op",
        admin_username="@admin",
        internal_token="bot-tok",
    )
    assert result is not None
    assert result["status"] == "error"
    assert result["route"] == "service_describe"
    api.delete_sales_service.assert_not_awaited()
    api.add_sales_service.assert_not_awaited()
    assert sent.calls and "описан" in sent.calls[0][1].lower()

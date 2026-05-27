"""NL → service_add integration test (Story 12.02b).

Drives ``handle_operator_service_nl_message`` end-to-end with a stubbed
:class:`ApiClient` and a stubbed OpenRouter client whose ``complete_json``
returns ``{"action": "add", "name": "Медовеевка Лайт", "description": null}``
for the operator's free-text DM. The handler must reuse the same
``ApiClient.add_sales_service`` call + ``Добавлено: <name> (id=N)`` reply
that the ``/service_add`` slash handler uses — DRY across the slash and NL
paths.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from services.bot_gateway.app.operator_service_nl import (
    handle_operator_service_nl_message,
)
from services.bot_gateway.app.telegram_update import NormalizedTelegramMessage


def _msg(text: str, *, username: str = "@op") -> NormalizedTelegramMessage:
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
        self.find_operator_by_username = AsyncMock(
            return_value={
                "username": "@op",
                "chat_id": 42,
                "project_id": 7,
                "is_active": True,
            }
        )
        self.add_sales_service = AsyncMock(return_value={"id": 12})
        self.list_sales_services = AsyncMock(return_value={"services": []})
        self.delete_sales_service = AsyncMock(return_value={"ok": True})


class _FakeOpenRouter:
    def __init__(self, payload: dict) -> None:
        self.complete_json = AsyncMock(return_value=payload)


@pytest.mark.asyncio
async def test_nl_add_without_description_calls_api_and_dms_confirmation() -> None:
    api = FakeApi()
    sent = _Sent()
    openrouter = _FakeOpenRouter(
        {"action": "add", "name": "Медовеевка Лайт", "description": None}
    )
    result = await handle_operator_service_nl_message(
        normalized=_msg("добавь услугу Медовеевка Лайт"),
        api_client=api,
        send_dm=sent,
        openrouter=openrouter,
        primary_operator_username="@op",
        admin_username="@admin",
        internal_token="bot-tok",
    )
    assert result is not None
    assert result["status"] == "ok"
    assert result["route"] == "service_add"
    assert result["service_id"] == "12"
    api.add_sales_service.assert_awaited_once_with(
        project_id=7,
        name="Медовеевка Лайт",
        description_md=None,
        tags=None,
        internal_token="bot-tok",
    )
    assert sent.calls == [(42, "Добавлено: Медовеевка Лайт (id=12)")]


@pytest.mark.asyncio
async def test_nl_add_with_description_passes_description_to_api() -> None:
    api = FakeApi()
    sent = _Sent()
    openrouter = _FakeOpenRouter(
        {"action": "add", "name": "каньонинг", "description": "спуск по верёвке"}
    )
    result = await handle_operator_service_nl_message(
        normalized=_msg("добавь услугу каньонинг — спуск по верёвке"),
        api_client=api,
        send_dm=sent,
        openrouter=openrouter,
        primary_operator_username="@op",
        admin_username="@admin",
        internal_token="bot-tok",
    )
    assert result is not None and result["status"] == "ok"
    api.add_sales_service.assert_awaited_once_with(
        project_id=7,
        name="каньонинг",
        description_md="спуск по верёвке",
        tags=None,
        internal_token="bot-tok",
    )
    assert sent.calls == [(42, "Добавлено: каньонинг (id=12)")]


@pytest.mark.asyncio
async def test_nl_list_returns_same_view_as_slash_service_list() -> None:
    api = FakeApi()
    api.list_sales_services = AsyncMock(
        return_value={
            "services": [
                {
                    "id": 12,
                    "project_id": 7,
                    "name": "каньонинг",
                    "description_md": "Каньонинг — это…",
                    "tags": [],
                    "is_active": True,
                }
            ]
        }
    )
    sent = _Sent()
    openrouter = _FakeOpenRouter({"action": "list"})
    result = await handle_operator_service_nl_message(
        normalized=_msg("какие у нас услуги?"),
        api_client=api,
        send_dm=sent,
        openrouter=openrouter,
        primary_operator_username="@op",
        admin_username="@admin",
        internal_token="bot-tok",
    )
    assert result is not None and result["status"] == "ok"
    assert result["route"] == "service_list"
    api.list_sales_services.assert_awaited_once_with(
        project_id=7, internal_token="bot-tok"
    )
    assert sent.calls == [(42, "12. каньонинг — Каньонинг — это…")]


@pytest.mark.asyncio
async def test_nl_list_when_empty_uses_same_hint_as_slash() -> None:
    api = FakeApi()
    sent = _Sent()
    openrouter = _FakeOpenRouter({"action": "list"})
    result = await handle_operator_service_nl_message(
        normalized=_msg("список услуг"),
        api_client=api,
        send_dm=sent,
        openrouter=openrouter,
        primary_operator_username="@op",
        admin_username="@admin",
        internal_token="bot-tok",
    )
    assert result is not None and result["status"] == "ok"
    assert result["route"] == "service_list"
    assert sent.calls == [
        (42, "Услуг пока нет. Добавьте первую через /service_add <название>.")
    ]


@pytest.mark.asyncio
async def test_nl_remove_by_name_resolves_id_then_deletes() -> None:
    api = FakeApi()
    api.list_sales_services = AsyncMock(
        return_value={
            "services": [
                {
                    "id": 12,
                    "project_id": 7,
                    "name": "каньонинг",
                    "description_md": "",
                    "is_active": True,
                }
            ]
        }
    )
    sent = _Sent()
    openrouter = _FakeOpenRouter({"action": "remove", "name": "каньонинг"})
    result = await handle_operator_service_nl_message(
        normalized=_msg("удали услугу каньонинг"),
        api_client=api,
        send_dm=sent,
        openrouter=openrouter,
        primary_operator_username="@op",
        admin_username="@admin",
        internal_token="bot-tok",
    )
    assert result is not None and result["status"] == "ok"
    assert result["route"] == "service_remove"
    api.delete_sales_service.assert_awaited_once_with(
        service_id=12, internal_token="bot-tok"
    )
    assert sent.calls == [(42, "Удалено: id=12")]


@pytest.mark.asyncio
async def test_nl_remove_when_name_not_found_dms_not_found() -> None:
    api = FakeApi()
    api.list_sales_services = AsyncMock(return_value={"services": []})
    sent = _Sent()
    openrouter = _FakeOpenRouter({"action": "remove", "name": "несуществует"})
    result = await handle_operator_service_nl_message(
        normalized=_msg("удали услугу несуществует"),
        api_client=api,
        send_dm=sent,
        openrouter=openrouter,
        primary_operator_username="@op",
        admin_username="@admin",
        internal_token="bot-tok",
    )
    assert result is not None
    assert result["status"] == "error"
    assert result["route"] == "service_remove"
    api.delete_sales_service.assert_not_awaited()
    assert sent.calls == [(42, "Не найдено: несуществует")]


@pytest.mark.asyncio
async def test_nl_remove_case_insensitive_name_match() -> None:
    api = FakeApi()
    api.list_sales_services = AsyncMock(
        return_value={
            "services": [
                {"id": 5, "project_id": 7, "name": "каньонинг", "is_active": True}
            ]
        }
    )
    sent = _Sent()
    openrouter = _FakeOpenRouter({"action": "remove", "name": "Каньонинг"})
    result = await handle_operator_service_nl_message(
        normalized=_msg("удали услугу Каньонинг"),
        api_client=api,
        send_dm=sent,
        openrouter=openrouter,
        primary_operator_username="@op",
        admin_username="@admin",
        internal_token="bot-tok",
    )
    assert result is not None and result["status"] == "ok"
    api.delete_sales_service.assert_awaited_once_with(
        service_id=5, internal_token="bot-tok"
    )

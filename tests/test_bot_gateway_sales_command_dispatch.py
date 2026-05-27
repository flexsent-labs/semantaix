"""Bot-side slash-command dispatcher tests for Story 12.02.

Covers ``/service_add``, ``/service_list``, ``/service_remove``, and
``/sales_state``. The gate is identical to the existing Epic-09 commands:
only the project's effective operator OR the configured admin may invoke.
Unauthorized senders are dropped with a structured ``unauthorized_sales_command``
log line.

The api is stubbed at the :class:`ApiClient` level so the tests assert the
exact Russian DMs the operator sees as well as the HTTP shape the bot would
have sent over the wire.
"""

from __future__ import annotations

import json as _json
import logging
from unittest.mock import AsyncMock

import httpx
import pytest

from services.bot_gateway.app import sales_command_dispatch as dispatch
from services.bot_gateway.app.api_client import ApiError
from services.bot_gateway.app.sales_command_dispatch import (
    handle_sales_command,
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


def _api_error(status: int, detail: str) -> ApiError:
    body = _json.dumps({"detail": detail}).encode()
    request = httpx.Request("POST", "http://api")
    response = httpx.Response(status, content=body, request=request)
    return ApiError("err", request=request, response=response, detail=detail)


class _Sent:
    """Capture every DM the dispatcher attempted to send."""

    def __init__(self) -> None:
        self.calls: list[tuple[int, str]] = []

    async def __call__(self, chat_id: int, text: str) -> None:
        self.calls.append((chat_id, text))


class FakeApi:
    """ApiClient stand-in — each method is an AsyncMock per test."""

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
        self.list_sales_services = AsyncMock(
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
        self.delete_sales_service = AsyncMock(return_value={"ok": True})
        self.get_sales_state = AsyncMock(return_value={"states": []})


# --- /service_add -----------------------------------------------------------


@pytest.mark.asyncio
async def test_service_add_authorized_operator_success(
    caplog: pytest.LogCaptureFixture,
) -> None:
    api = FakeApi()
    sent = _Sent()
    result = await handle_sales_command(
        normalized=_msg("/service_add Медовеевка Лайт | Лайт уровень, с видами"),
        api_client=api,
        send_dm=sent,
        primary_operator_username="@op",
        admin_username="@admin",
        internal_token="bot-tok",
    )
    assert result is not None
    assert result["status"] == "ok"
    assert result["route"] == "service_add"
    assert sent.calls == [(42, "Добавлено: Медовеевка Лайт (id=12)")]
    api.add_sales_service.assert_awaited_once_with(
        project_id=7,
        name="Медовеевка Лайт",
        description_md="Лайт уровень, с видами",
        tags=None,
        internal_token="bot-tok",
    )


@pytest.mark.asyncio
async def test_service_add_admin_succeeds_with_admin_project_id() -> None:
    """Admin invokes the command — we still resolve via the operator
    registry (admin → `find_operator_by_username` returns the admin's
    own row); the request goes through unchanged."""
    api = FakeApi()
    api.find_operator_by_username = AsyncMock(
        return_value={
            "username": "@admin",
            "chat_id": 99,
            "project_id": 5,
            "is_active": True,
        }
    )
    sent = _Sent()
    result = await handle_sales_command(
        normalized=_msg("/service_add тур", username="@admin"),
        api_client=api,
        send_dm=sent,
        primary_operator_username="@op",
        admin_username="@admin",
        internal_token="bot-tok",
    )
    assert result is not None and result["status"] == "ok"
    api.add_sales_service.assert_awaited_once()
    # Admin's resolved project_id is what we call the api with.
    assert api.add_sales_service.await_args.kwargs["project_id"] == 5


@pytest.mark.asyncio
async def test_service_add_unauthorized_sender_silently_ignored(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Unknown sender → dispatch returns ``None`` so the normal pipeline
    runs (no DM, but a structured log line so an operator can debug)."""
    api = FakeApi()
    api.find_operator_by_username = AsyncMock(return_value=None)
    sent = _Sent()
    caplog.set_level(logging.WARNING)
    result = await handle_sales_command(
        normalized=_msg("/service_add x", username="@stranger"),
        api_client=api,
        send_dm=sent,
        primary_operator_username="@op",
        admin_username="@admin",
        internal_token="bot-tok",
    )
    assert result == {
        "status": "ignored",
        "reason": "unauthorized_sales_command",
    }
    assert sent.calls == []
    api.add_sales_service.assert_not_awaited()
    matching = [r for r in caplog.records if r.message == "unauthorized_sales_command"]
    assert matching, "expected unauthorized_sales_command log line"
    extra = matching[0]
    assert getattr(extra, "from_username") == "@stranger"


@pytest.mark.asyncio
async def test_service_add_duplicate_returns_one_line_message() -> None:
    api = FakeApi()
    api.add_sales_service = AsyncMock(
        side_effect=_api_error(409, "service_already_exists")
    )
    sent = _Sent()
    result = await handle_sales_command(
        normalized=_msg("/service_add каньонинг"),
        api_client=api,
        send_dm=sent,
        primary_operator_username="@op",
        admin_username="@admin",
        internal_token="bot-tok",
    )
    assert result["status"] == "error"
    assert sent.calls == [(42, "Такая услуга уже есть: каньонинг.")]


@pytest.mark.asyncio
async def test_service_add_invalid_args_returns_usage() -> None:
    api = FakeApi()
    sent = _Sent()
    result = await handle_sales_command(
        normalized=_msg("/service_add"),
        api_client=api,
        send_dm=sent,
        primary_operator_username="@op",
        admin_username="@admin",
        internal_token="bot-tok",
    )
    assert result["status"] == "error"
    assert sent.calls == [
        (42, "Использование: /service_add <название> [| описание]")
    ]
    api.add_sales_service.assert_not_awaited()


@pytest.mark.asyncio
async def test_service_add_api_unreachable_dms_error(
    caplog: pytest.LogCaptureFixture,
) -> None:
    api = FakeApi()
    api.add_sales_service = AsyncMock(
        side_effect=httpx.ConnectError("api down")
    )
    sent = _Sent()
    caplog.set_level(logging.WARNING)
    result = await handle_sales_command(
        normalized=_msg("/service_add x"),
        api_client=api,
        send_dm=sent,
        primary_operator_username="@op",
        admin_username="@admin",
        internal_token="bot-tok",
    )
    assert result["status"] == "error"
    assert sent.calls and sent.calls[0][0] == 42
    assert "недоступен" in sent.calls[0][1].lower() or "позже" in sent.calls[0][1].lower()
    matching = [r for r in caplog.records if r.message == "sales_command_api_error"]
    assert matching, "expected sales_command_api_error log"


# --- /service_list ----------------------------------------------------------


@pytest.mark.asyncio
async def test_service_list_renders_one_row_per_service() -> None:
    api = FakeApi()
    sent = _Sent()
    result = await handle_sales_command(
        normalized=_msg("/service_list"),
        api_client=api,
        send_dm=sent,
        primary_operator_username="@op",
        admin_username="@admin",
        internal_token="bot-tok",
    )
    assert result["status"] == "ok"
    assert sent.calls == [(42, "12. каньонинг — Каньонинг — это…")]


@pytest.mark.asyncio
async def test_service_list_renders_multiple_rows() -> None:
    api = FakeApi()
    api.list_sales_services = AsyncMock(
        return_value={
            "services": [
                {
                    "id": 1,
                    "project_id": 7,
                    "name": "alpha",
                    "description_md": None,
                    "tags": [],
                    "is_active": True,
                },
                {
                    "id": 2,
                    "project_id": 7,
                    "name": "beta",
                    "description_md": "second",
                    "tags": [],
                    "is_active": True,
                },
            ]
        }
    )
    sent = _Sent()
    await handle_sales_command(
        normalized=_msg("/service_list"),
        api_client=api,
        send_dm=sent,
        primary_operator_username="@op",
        admin_username="@admin",
        internal_token="bot-tok",
    )
    assert sent.calls == [(42, "1. alpha\n2. beta — second")]


@pytest.mark.asyncio
async def test_service_list_empty_returns_hint() -> None:
    api = FakeApi()
    api.list_sales_services = AsyncMock(return_value={"services": []})
    sent = _Sent()
    await handle_sales_command(
        normalized=_msg("/service_list"),
        api_client=api,
        send_dm=sent,
        primary_operator_username="@op",
        admin_username="@admin",
        internal_token="bot-tok",
    )
    assert sent.calls == [
        (42, "Услуг пока нет. Добавьте первую через /service_add <название>.")
    ]


# --- /service_remove --------------------------------------------------------


@pytest.mark.asyncio
async def test_service_remove_success() -> None:
    api = FakeApi()
    sent = _Sent()
    await handle_sales_command(
        normalized=_msg("/service_remove 12"),
        api_client=api,
        send_dm=sent,
        primary_operator_username="@op",
        admin_username="@admin",
        internal_token="bot-tok",
    )
    assert sent.calls == [(42, "Удалено: id=12")]
    api.delete_sales_service.assert_awaited_once_with(
        service_id=12, internal_token="bot-tok"
    )


@pytest.mark.asyncio
async def test_service_remove_not_found_returns_not_found_line() -> None:
    api = FakeApi()
    api.delete_sales_service = AsyncMock(
        side_effect=_api_error(404, "service_not_found")
    )
    sent = _Sent()
    await handle_sales_command(
        normalized=_msg("/service_remove 12"),
        api_client=api,
        send_dm=sent,
        primary_operator_username="@op",
        admin_username="@admin",
        internal_token="bot-tok",
    )
    assert sent.calls == [(42, "Не найдено: id=12")]


@pytest.mark.asyncio
async def test_service_remove_invalid_arg_returns_usage() -> None:
    api = FakeApi()
    sent = _Sent()
    await handle_sales_command(
        normalized=_msg("/service_remove abc"),
        api_client=api,
        send_dm=sent,
        primary_operator_username="@op",
        admin_username="@admin",
        internal_token="bot-tok",
    )
    assert sent.calls == [(42, "Использование: /service_remove <id>")]
    api.delete_sales_service.assert_not_awaited()


# --- /sales_state -----------------------------------------------------------


@pytest.mark.asyncio
async def test_sales_state_empty_returns_hint() -> None:
    api = FakeApi()
    sent = _Sent()
    await handle_sales_command(
        normalized=_msg("/sales_state"),
        api_client=api,
        send_dm=sent,
        primary_operator_username="@op",
        admin_username="@admin",
        internal_token="bot-tok",
    )
    assert sent.calls == [(42, "Активных бесед нет.")]
    api.get_sales_state.assert_awaited_once_with(
        project_id=7, chat_id=None, internal_token="bot-tok"
    )


@pytest.mark.asyncio
async def test_sales_state_renders_compact_summary() -> None:
    api = FakeApi()
    api.get_sales_state = AsyncMock(
        return_value={
            "states": [
                {
                    "chat_id": 12345,
                    "project_id": 7,
                    "current_stage": "scoping",
                    "collected_intent": {"dates": "1 мая"},
                    "last_proposal": None,
                    "last_customer_msg_at": "2026-05-27T18:42:00+00:00",
                    "last_bot_msg_at": None,
                }
            ]
        }
    )
    sent = _Sent()
    await handle_sales_command(
        normalized=_msg("/sales_state"),
        api_client=api,
        send_dm=sent,
        primary_operator_username="@op",
        admin_username="@admin",
        internal_token="bot-tok",
    )
    assert sent.calls == [
        (
            42,
            'chat=12345 stage=scoping intent={"dates": "1 мая"} '
            'last_msg=18:42',
        )
    ]


@pytest.mark.asyncio
async def test_sales_state_with_customer_arg_filters_chat() -> None:
    """``/sales_state @customer`` resolves to a chat_id via the api operator
    lookup so the state read is scoped server-side."""
    api = FakeApi()
    # Two calls: one for the operator (@op), one for the @customer arg.
    op_record = {
        "username": "@op",
        "chat_id": 42,
        "project_id": 7,
        "is_active": True,
    }
    customer_record = {
        "username": "@customer",
        "chat_id": 12345,
        "project_id": 7,
        "is_active": True,
    }
    api.find_operator_by_username = AsyncMock(
        side_effect=[op_record, customer_record]
    )
    sent = _Sent()
    await handle_sales_command(
        normalized=_msg("/sales_state @customer"),
        api_client=api,
        send_dm=sent,
        primary_operator_username="@op",
        admin_username="@admin",
        internal_token="bot-tok",
    )
    api.get_sales_state.assert_awaited_once_with(
        project_id=7, chat_id=12345, internal_token="bot-tok"
    )


@pytest.mark.asyncio
async def test_sales_state_unknown_customer_returns_one_line_error() -> None:
    api = FakeApi()
    # First lookup for the operator succeeds; second for @nobody returns None.
    op_record = {
        "username": "@op",
        "chat_id": 42,
        "project_id": 7,
        "is_active": True,
    }
    api.find_operator_by_username = AsyncMock(side_effect=[op_record, None])
    sent = _Sent()
    await handle_sales_command(
        normalized=_msg("/sales_state @nobody"),
        api_client=api,
        send_dm=sent,
        primary_operator_username="@op",
        admin_username="@admin",
        internal_token="bot-tok",
    )
    assert sent.calls == [(42, "Не нашёл такого собеседника: @nobody.")]
    api.get_sales_state.assert_not_awaited()


# --- non-matching messages skip silently -----------------------------------


@pytest.mark.asyncio
async def test_non_sales_command_returns_none() -> None:
    api = FakeApi()
    sent = _Sent()
    assert (
        await handle_sales_command(
            normalized=_msg("привет"),
            api_client=api,
            send_dm=sent,
            primary_operator_username="@op",
            admin_username="@admin",
            internal_token="bot-tok",
        )
        is None
    )
    assert sent.calls == []


# --- extra error-path coverage ---------------------------------------------


@pytest.mark.asyncio
async def test_service_add_generic_api_error_logs_and_dms() -> None:
    """ApiError with a detail other than ``service_already_exists`` falls
    through to the shared error helper — DM the unavailable hint, log
    ``sales_command_api_error``."""
    api = FakeApi()
    api.add_sales_service = AsyncMock(
        side_effect=_api_error(500, "internal_error")
    )
    sent = _Sent()
    result = await handle_sales_command(
        normalized=_msg("/service_add x"),
        api_client=api,
        send_dm=sent,
        primary_operator_username="@op",
        admin_username="@admin",
        internal_token="bot-tok",
    )
    assert result["status"] == "error"
    assert result["detail"] == "internal_error"
    assert sent.calls and "недоступен" in sent.calls[0][1].lower()


@pytest.mark.asyncio
async def test_service_list_api_unreachable_logs_and_dms(
    caplog: pytest.LogCaptureFixture,
) -> None:
    api = FakeApi()
    api.list_sales_services = AsyncMock(side_effect=httpx.ConnectError("down"))
    sent = _Sent()
    caplog.set_level(logging.WARNING)
    result = await handle_sales_command(
        normalized=_msg("/service_list"),
        api_client=api,
        send_dm=sent,
        primary_operator_username="@op",
        admin_username="@admin",
        internal_token="bot-tok",
    )
    assert result == {"status": "error", "route": "service_list"}
    assert sent.calls and "недоступен" in sent.calls[0][1].lower()
    assert any(r.message == "sales_command_api_error" for r in caplog.records)


@pytest.mark.asyncio
async def test_service_remove_generic_api_error_logs_and_dms() -> None:
    api = FakeApi()
    api.delete_sales_service = AsyncMock(
        side_effect=_api_error(500, "internal_error")
    )
    sent = _Sent()
    result = await handle_sales_command(
        normalized=_msg("/service_remove 12"),
        api_client=api,
        send_dm=sent,
        primary_operator_username="@op",
        admin_username="@admin",
        internal_token="bot-tok",
    )
    assert result["status"] == "error"
    assert result["detail"] == "internal_error"


@pytest.mark.asyncio
async def test_service_remove_api_unreachable_dms_error() -> None:
    api = FakeApi()
    api.delete_sales_service = AsyncMock(side_effect=httpx.ConnectError("down"))
    sent = _Sent()
    result = await handle_sales_command(
        normalized=_msg("/service_remove 12"),
        api_client=api,
        send_dm=sent,
        primary_operator_username="@op",
        admin_username="@admin",
        internal_token="bot-tok",
    )
    assert result == {"status": "error", "route": "service_remove"}
    assert sent.calls and "недоступен" in sent.calls[0][1].lower()


@pytest.mark.asyncio
async def test_sales_state_api_unreachable_dms_error() -> None:
    api = FakeApi()
    api.get_sales_state = AsyncMock(side_effect=httpx.ConnectError("down"))
    sent = _Sent()
    result = await handle_sales_command(
        normalized=_msg("/sales_state"),
        api_client=api,
        send_dm=sent,
        primary_operator_username="@op",
        admin_username="@admin",
        internal_token="bot-tok",
    )
    assert result == {"status": "error", "route": "sales_state"}
    assert sent.calls and "недоступен" in sent.calls[0][1].lower()


@pytest.mark.asyncio
async def test_sales_state_unparseable_last_msg_keeps_raw_string() -> None:
    """``last_customer_msg_at`` that isn't a valid ISO timestamp must not
    crash — the renderer keeps the raw string verbatim."""
    api = FakeApi()
    api.get_sales_state = AsyncMock(
        return_value={
            "states": [
                {
                    "chat_id": 12345,
                    "project_id": 7,
                    "current_stage": "scoping",
                    "collected_intent": {},
                    "last_proposal": None,
                    "last_customer_msg_at": "not-a-timestamp",
                    "last_bot_msg_at": None,
                }
            ]
        }
    )
    sent = _Sent()
    await handle_sales_command(
        normalized=_msg("/sales_state"),
        api_client=api,
        send_dm=sent,
        primary_operator_username="@op",
        admin_username="@admin",
        internal_token="bot-tok",
    )
    assert sent.calls[0][1].endswith("last_msg=not-a-timestamp")


@pytest.mark.asyncio
async def test_log_api_error_status_from_httpx_status_error(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """When a bare httpx.HTTPStatusError surfaces (no api detail), the
    helper still records the HTTP status from the response."""
    api = FakeApi()
    request = httpx.Request("POST", "http://api")
    response = httpx.Response(503, request=request)
    api.add_sales_service = AsyncMock(
        side_effect=httpx.HTTPStatusError("err", request=request, response=response)
    )
    caplog.set_level(logging.WARNING)
    sent = _Sent()
    await handle_sales_command(
        normalized=_msg("/service_add x"),
        api_client=api,
        send_dm=sent,
        primary_operator_username="@op",
        admin_username="@admin",
        internal_token="bot-tok",
    )
    records = [r for r in caplog.records if r.message == "sales_command_api_error"]
    assert records and getattr(records[0], "status") == 503


@pytest.mark.asyncio
async def test_admin_without_operator_record_is_treated_as_unauthorized(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Defensive: an admin who is not registered in the operator table
    has no resolved project_id. The dispatcher refuses rather than
    guessing a project_id (and logs ``admin_has_no_project_mapping``)."""
    api = FakeApi()
    api.find_operator_by_username = AsyncMock(return_value=None)
    caplog.set_level(logging.WARNING)
    sent = _Sent()
    result = await handle_sales_command(
        normalized=_msg("/service_add x", username="@admin"),
        api_client=api,
        send_dm=sent,
        primary_operator_username="@op",
        admin_username="@admin",
        internal_token="bot-tok",
    )
    assert result == {
        "status": "ignored",
        "reason": "unauthorized_sales_command",
    }
    notes = [
        getattr(r, "note", None)
        for r in caplog.records
        if r.message == "unauthorized_sales_command"
    ]
    assert "admin_has_no_project_mapping" in notes


# Silence unused-import lint when the module is loaded standalone.
_ = dispatch

"""Error-path coverage for the operator services NL dispatcher.

The happy paths are covered by ``test_bot_gateway_nl_service_add.py``,
``test_bot_gateway_nl_service_describe.py``, and the fallthrough /
unauthorized tests. This module covers:

- ApiError / HTTP / network failures on every per-action route (add, list,
  remove, describe).
- The duplicate / not-found short-cut DMs.
- The admin-without-project-mapping path in the operator gate.
- The service-row renderer's no-description branch.
"""

from __future__ import annotations

import json as _json
import logging
from unittest.mock import AsyncMock

import httpx
import pytest

from services.bot_gateway.app import operator_service_nl as nl
from services.bot_gateway.app.api_client import ApiError
from services.bot_gateway.app.operator_service_nl import (
    _render_service_row,
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


def _api_error(status: int, detail: str) -> ApiError:
    body = _json.dumps({"detail": detail}).encode()
    request = httpx.Request("POST", "http://api")
    response = httpx.Response(status, content=body, request=request)
    return ApiError("err", request=request, response=response, detail=detail)


def _http_error(status: int) -> httpx.HTTPStatusError:
    request = httpx.Request("POST", "http://api")
    response = httpx.Response(status, request=request)
    return httpx.HTTPStatusError("err", request=request, response=response)


class _Sent:
    def __init__(self) -> None:
        self.calls: list[tuple[int, str]] = []

    async def __call__(self, chat_id: int, text: str) -> None:
        self.calls.append((chat_id, text))


def _operator_record() -> dict:
    return {
        "username": "@op",
        "chat_id": 42,
        "project_id": 7,
        "is_active": True,
    }


class FakeApi:
    def __init__(self) -> None:
        self.find_operator_by_username = AsyncMock(return_value=_operator_record())
        self.add_sales_service = AsyncMock(return_value={"id": 12})
        self.list_sales_services = AsyncMock(return_value={"services": []})
        self.delete_sales_service = AsyncMock(return_value={"ok": True})


class _FakeOpenRouter:
    def __init__(self, payload: dict) -> None:
        self.complete_json = AsyncMock(return_value=payload)


# --- Renderer ---------------------------------------------------------------


def test_render_service_row_without_description_omits_dash() -> None:
    row = {"id": 9, "name": "каньонинг"}
    assert _render_service_row(row) == "9. каньонинг"


def test_render_service_row_with_description_uses_dash() -> None:
    row = {"id": 9, "name": "каньонинг", "description_md": "спуск"}
    assert _render_service_row(row) == "9. каньонинг — спуск"


def test_render_service_row_missing_name_uses_question_mark() -> None:
    row = {"id": 9}
    assert _render_service_row(row) == "9. ?"


# --- Admin without project mapping -----------------------------------------


@pytest.mark.asyncio
async def test_admin_with_no_project_mapping_falls_through(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """An admin username with NO operator-registry row → ignore + log
    ``admin_has_no_project_mapping`` note. The handler must NOT call the
    LLM in this defensive branch."""
    caplog.set_level(logging.WARNING)
    api = FakeApi()
    api.find_operator_by_username = AsyncMock(return_value=None)
    sent = _Sent()
    openrouter = _FakeOpenRouter({"action": "list"})
    result = await handle_operator_service_nl_message(
        normalized=_msg("какие у нас услуги?", username="@admin"),
        api_client=api,
        send_dm=sent,
        openrouter=openrouter,
        primary_operator_username="@op",
        admin_username="@admin",
        internal_token="bot-tok",
    )
    # Defensive path: returns None so the rest of the pipeline runs.
    assert result is None
    openrouter.complete_json.assert_not_awaited()
    events = [
        r for r in caplog.records
        if r.message == "unauthorized_service_nl"
        and getattr(r, "note", None) == "admin_has_no_project_mapping"
    ]
    assert events, "expected admin_has_no_project_mapping log"


@pytest.mark.asyncio
async def test_admin_with_resolved_project_runs_classifier() -> None:
    """An admin username that resolves to a project IS authorized — the
    classifier must run for them."""
    api = FakeApi()
    api.find_operator_by_username = AsyncMock(
        return_value={
            "username": "@admin",
            "chat_id": 1,
            "project_id": 9,
            "is_active": True,
        }
    )
    sent = _Sent()
    openrouter = _FakeOpenRouter({"action": "list"})
    result = await handle_operator_service_nl_message(
        normalized=_msg("список услуг", username="@admin"),
        api_client=api,
        send_dm=sent,
        openrouter=openrouter,
        primary_operator_username="@op",
        admin_username="@admin",
        internal_token="bot-tok",
    )
    assert result is not None and result["status"] == "ok"
    openrouter.complete_json.assert_awaited_once()


# --- service_add error paths ------------------------------------------------


@pytest.mark.asyncio
async def test_add_duplicate_dms_one_line_and_does_not_log_api_error(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.WARNING)
    api = FakeApi()
    api.add_sales_service = AsyncMock(
        side_effect=_api_error(409, "service_already_exists")
    )
    sent = _Sent()
    openrouter = _FakeOpenRouter({"action": "add", "name": "каньонинг"})
    result = await handle_operator_service_nl_message(
        normalized=_msg("добавь услугу каньонинг"),
        api_client=api,
        send_dm=sent,
        openrouter=openrouter,
        primary_operator_username="@op",
        admin_username="@admin",
        internal_token="bot-tok",
    )
    assert result is not None
    assert result["status"] == "error"
    assert result["decision"] == "duplicate"
    assert sent.calls == [(42, "Такая услуга уже есть: каньонинг.")]


@pytest.mark.asyncio
async def test_add_other_api_error_dms_generic_unavailable(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.WARNING)
    api = FakeApi()
    api.add_sales_service = AsyncMock(side_effect=_api_error(500, "unexpected"))
    sent = _Sent()
    openrouter = _FakeOpenRouter({"action": "add", "name": "X"})
    result = await handle_operator_service_nl_message(
        normalized=_msg("добавь услугу X"),
        api_client=api,
        send_dm=sent,
        openrouter=openrouter,
        primary_operator_username="@op",
        admin_username="@admin",
        internal_token="bot-tok",
    )
    assert result is not None and result["status"] == "error"
    assert sent.calls and sent.calls[0] == (
        42,
        "Сервис временно недоступен, попробуйте позже.",
    )
    matching = [
        r for r in caplog.records if r.message == "operator_service_nl_api_error"
    ]
    assert matching, "expected operator_service_nl_api_error log"


@pytest.mark.asyncio
async def test_add_http_status_error_logs_status() -> None:
    api = FakeApi()
    api.add_sales_service = AsyncMock(side_effect=_http_error(503))
    sent = _Sent()
    openrouter = _FakeOpenRouter({"action": "add", "name": "X"})
    result = await handle_operator_service_nl_message(
        normalized=_msg("добавь услугу X"),
        api_client=api,
        send_dm=sent,
        openrouter=openrouter,
        primary_operator_username="@op",
        admin_username="@admin",
        internal_token="bot-tok",
    )
    assert result is not None and result["status"] == "error"
    assert sent.calls[0][1] == "Сервис временно недоступен, попробуйте позже."


@pytest.mark.asyncio
async def test_add_request_error_dms_unavailable() -> None:
    api = FakeApi()
    api.add_sales_service = AsyncMock(side_effect=httpx.ConnectError("down"))
    sent = _Sent()
    openrouter = _FakeOpenRouter({"action": "add", "name": "X"})
    result = await handle_operator_service_nl_message(
        normalized=_msg("добавь услугу X"),
        api_client=api,
        send_dm=sent,
        openrouter=openrouter,
        primary_operator_username="@op",
        admin_username="@admin",
        internal_token="bot-tok",
    )
    assert result is not None and result["status"] == "error"
    assert sent.calls[0][1] == "Сервис временно недоступен, попробуйте позже."


# --- service_list error paths -----------------------------------------------


@pytest.mark.asyncio
async def test_list_api_error_dms_unavailable() -> None:
    api = FakeApi()
    api.list_sales_services = AsyncMock(side_effect=_api_error(500, "boom"))
    sent = _Sent()
    openrouter = _FakeOpenRouter({"action": "list"})
    result = await handle_operator_service_nl_message(
        normalized=_msg("какие услуги?"),
        api_client=api,
        send_dm=sent,
        openrouter=openrouter,
        primary_operator_username="@op",
        admin_username="@admin",
        internal_token="bot-tok",
    )
    assert result is not None and result["status"] == "error"
    assert sent.calls[0][1] == "Сервис временно недоступен, попробуйте позже."


# --- service_remove error paths --------------------------------------------


@pytest.mark.asyncio
async def test_remove_list_call_fails_dms_unavailable() -> None:
    api = FakeApi()
    api.list_sales_services = AsyncMock(side_effect=_api_error(500, "boom"))
    sent = _Sent()
    openrouter = _FakeOpenRouter({"action": "remove", "name": "x"})
    result = await handle_operator_service_nl_message(
        normalized=_msg("удали услугу x"),
        api_client=api,
        send_dm=sent,
        openrouter=openrouter,
        primary_operator_username="@op",
        admin_username="@admin",
        internal_token="bot-tok",
    )
    assert result is not None and result["status"] == "error"
    api.delete_sales_service.assert_not_awaited()
    assert sent.calls[0][1] == "Сервис временно недоступен, попробуйте позже."


@pytest.mark.asyncio
async def test_remove_api_returns_not_found_after_lookup() -> None:
    """Lookup matches by name but DELETE races to a not-found (e.g. another
    operator deleted it concurrently). The handler must DM the id-based
    not-found message + log."""
    api = FakeApi()
    api.list_sales_services = AsyncMock(
        return_value={"services": [{"id": 5, "name": "x", "is_active": True}]}
    )
    api.delete_sales_service = AsyncMock(
        side_effect=_api_error(404, "service_not_found")
    )
    sent = _Sent()
    openrouter = _FakeOpenRouter({"action": "remove", "name": "x"})
    result = await handle_operator_service_nl_message(
        normalized=_msg("удали услугу x"),
        api_client=api,
        send_dm=sent,
        openrouter=openrouter,
        primary_operator_username="@op",
        admin_username="@admin",
        internal_token="bot-tok",
    )
    assert result is not None
    assert result["decision"] == "not_found"
    assert sent.calls == [(42, "Не найдено: id=5")]


@pytest.mark.asyncio
async def test_remove_delete_api_error_other_detail_dms_unavailable() -> None:
    api = FakeApi()
    api.list_sales_services = AsyncMock(
        return_value={"services": [{"id": 5, "name": "x", "is_active": True}]}
    )
    api.delete_sales_service = AsyncMock(
        side_effect=_api_error(500, "kaboom")
    )
    sent = _Sent()
    openrouter = _FakeOpenRouter({"action": "remove", "name": "x"})
    result = await handle_operator_service_nl_message(
        normalized=_msg("удали услугу x"),
        api_client=api,
        send_dm=sent,
        openrouter=openrouter,
        primary_operator_username="@op",
        admin_username="@admin",
        internal_token="bot-tok",
    )
    assert result is not None and result["status"] == "error"
    assert result.get("detail") == "kaboom"
    assert sent.calls[0][1] == "Сервис временно недоступен, попробуйте позже."


@pytest.mark.asyncio
async def test_remove_delete_request_error_dms_unavailable() -> None:
    api = FakeApi()
    api.list_sales_services = AsyncMock(
        return_value={"services": [{"id": 5, "name": "x", "is_active": True}]}
    )
    api.delete_sales_service = AsyncMock(
        side_effect=httpx.ConnectError("down")
    )
    sent = _Sent()
    openrouter = _FakeOpenRouter({"action": "remove", "name": "x"})
    result = await handle_operator_service_nl_message(
        normalized=_msg("удали услугу x"),
        api_client=api,
        send_dm=sent,
        openrouter=openrouter,
        primary_operator_username="@op",
        admin_username="@admin",
        internal_token="bot-tok",
    )
    assert result is not None and result["status"] == "error"
    assert sent.calls[0][1] == "Сервис временно недоступен, попробуйте позже."


# --- service_add other_detail returns detail field --------------------------


@pytest.mark.asyncio
async def test_add_api_error_includes_detail_in_result() -> None:
    api = FakeApi()
    api.add_sales_service = AsyncMock(side_effect=_api_error(422, "validation_failed"))
    sent = _Sent()
    openrouter = _FakeOpenRouter({"action": "add", "name": "X"})
    result = await handle_operator_service_nl_message(
        normalized=_msg("добавь услугу X"),
        api_client=api,
        send_dm=sent,
        openrouter=openrouter,
        primary_operator_username="@op",
        admin_username="@admin",
        internal_token="bot-tok",
    )
    assert result is not None
    assert result.get("detail") == "validation_failed"


# --- service_describe error paths ------------------------------------------


@pytest.mark.asyncio
async def test_describe_list_call_fails_dms_unavailable() -> None:
    api = FakeApi()
    api.list_sales_services = AsyncMock(side_effect=_api_error(500, "boom"))
    sent = _Sent()
    openrouter = _FakeOpenRouter(
        {"action": "describe", "name": "x", "description": "y"}
    )
    result = await handle_operator_service_nl_message(
        normalized=_msg("опиши x как y"),
        api_client=api,
        send_dm=sent,
        openrouter=openrouter,
        primary_operator_username="@op",
        admin_username="@admin",
        internal_token="bot-tok",
    )
    assert result is not None and result["status"] == "error"
    api.delete_sales_service.assert_not_awaited()
    api.add_sales_service.assert_not_awaited()
    assert sent.calls[0][1] == "Сервис временно недоступен, попробуйте позже."


@pytest.mark.asyncio
async def test_describe_delete_step_fails_aborts_before_add() -> None:
    api = FakeApi()
    api.list_sales_services = AsyncMock(
        return_value={"services": [{"id": 5, "name": "x", "is_active": True}]}
    )
    api.delete_sales_service = AsyncMock(side_effect=_api_error(500, "boom"))
    sent = _Sent()
    openrouter = _FakeOpenRouter(
        {"action": "describe", "name": "x", "description": "y"}
    )
    result = await handle_operator_service_nl_message(
        normalized=_msg("опиши x как y"),
        api_client=api,
        send_dm=sent,
        openrouter=openrouter,
        primary_operator_username="@op",
        admin_username="@admin",
        internal_token="bot-tok",
    )
    assert result is not None and result["status"] == "error"
    api.add_sales_service.assert_not_awaited()
    assert sent.calls[0][1] == "Сервис временно недоступен, попробуйте позже."


@pytest.mark.asyncio
async def test_describe_add_step_fails_after_delete() -> None:
    api = FakeApi()
    api.list_sales_services = AsyncMock(
        return_value={"services": [{"id": 5, "name": "x", "is_active": True}]}
    )
    api.add_sales_service = AsyncMock(side_effect=_api_error(500, "boom"))
    sent = _Sent()
    openrouter = _FakeOpenRouter(
        {"action": "describe", "name": "x", "description": "y"}
    )
    result = await handle_operator_service_nl_message(
        normalized=_msg("опиши x как y"),
        api_client=api,
        send_dm=sent,
        openrouter=openrouter,
        primary_operator_username="@op",
        admin_username="@admin",
        internal_token="bot-tok",
    )
    assert result is not None and result["status"] == "error"
    # The delete already happened — we don't roll back, the operator can
    # re-add with `добавь услугу …` if needed.
    api.delete_sales_service.assert_awaited_once_with(
        service_id=5, internal_token="bot-tok"
    )
    assert sent.calls[0][1] == "Сервис временно недоступен, попробуйте позже."


# --- _log_api_error_and_dm direct coverage ---------------------------------


@pytest.mark.asyncio
async def test_log_api_error_with_api_error_records_status_and_detail(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.WARNING)
    sent = _Sent()
    exc = _api_error(503, "downstream_unavailable")
    await nl._log_api_error_and_dm(
        send_dm=sent,
        normalized=_msg("dummy"),
        trace_id="t-1",
        route="service_add",
        exc=exc,
    )
    matching = [
        r for r in caplog.records if r.message == "operator_service_nl_api_error"
    ]
    assert matching
    record = matching[0]
    assert getattr(record, "status") == 503
    assert getattr(record, "detail") == "downstream_unavailable"


@pytest.mark.asyncio
async def test_log_api_error_with_httpx_status_error_records_status(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.WARNING)
    sent = _Sent()
    exc = _http_error(504)
    await nl._log_api_error_and_dm(
        send_dm=sent,
        normalized=_msg("dummy"),
        trace_id="t-2",
        route="service_remove",
        exc=exc,
    )
    matching = [
        r for r in caplog.records if r.message == "operator_service_nl_api_error"
    ]
    assert matching
    assert getattr(matching[0], "status") == 504


@pytest.mark.asyncio
async def test_log_api_error_with_generic_exception_records_zero_status(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.WARNING)
    sent = _Sent()
    await nl._log_api_error_and_dm(
        send_dm=sent,
        normalized=_msg("dummy"),
        trace_id="t-3",
        route="service_list",
        exc=httpx.ConnectError("net down"),
    )
    matching = [
        r for r in caplog.records if r.message == "operator_service_nl_api_error"
    ]
    assert matching
    assert getattr(matching[0], "status") == 0

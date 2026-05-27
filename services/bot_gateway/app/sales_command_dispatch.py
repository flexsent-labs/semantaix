"""Slash-command dispatcher for the Story 12.02 sales catalog commands.

Routes ``/service_add``, ``/service_list``, ``/service_remove``, and
``/sales_state`` to the api's ``/sales/services`` + ``/sales/state``
endpoints.

Gating mirrors the Epic-09 commands: the sender must be a registered active
operator (via :func:`resolve_operator_for_sender`) OR the configured admin.
Unauthorized senders are dropped with a structured
``unauthorized_sales_command`` log event and **no** DM (mirrors the silent
behaviour of the services-NL dialog so non-operator chats don't leak
operator-only error strings).

Validation-error UX: every command returns exactly one line to the operator,
with the canonical usage example. The api's ``ApiError.detail`` is mapped to
a small set of Russian strings; everything else falls back to a generic
"сервис недоступен" line with a structured ``sales_command_api_error`` log so
the operator + on-call can diagnose without seeing the raw exception.
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Awaitable, Callable
from typing import Any

import httpx

from services.bot_gateway.app.api_client import ApiClient, ApiError
from services.bot_gateway.app.operator_resolver import (
    ResolvedOperator,
    resolve_operator_for_sender,
)
from services.bot_gateway.app.sales_commands import (
    SalesCommandUsageError,
    parse_sales_state,
    parse_service_add,
    parse_service_remove,
)
from services.bot_gateway.app.telegram_update import NormalizedTelegramMessage

logger = logging.getLogger(__name__)

SendDmFn = Callable[[int, str], Awaitable[Any]]

_SERVICE_ADD_RE = re.compile(r"^\s*/service_add\b", re.IGNORECASE)
_SERVICE_LIST_RE = re.compile(r"^\s*/service_list\b", re.IGNORECASE)
_SERVICE_REMOVE_RE = re.compile(r"^\s*/service_remove\b", re.IGNORECASE)
_SALES_STATE_RE = re.compile(r"^\s*/sales_state\b", re.IGNORECASE)

_EMPTY_LIST_HINT = (
    "Услуг пока нет. Добавьте первую через /service_add <название>."
)
_NO_ACTIVE_CHATS = "Активных бесед нет."
_API_UNAVAILABLE = "Сервис временно недоступен, попробуйте позже."


async def handle_sales_command(
    *,
    normalized: NormalizedTelegramMessage,
    api_client: ApiClient,
    send_dm: SendDmFn,
    primary_operator_username: str,
    admin_username: str,
    internal_token: str,
) -> dict[str, str] | None:
    """Top-level dispatcher; returns ``None`` for non-sales-command messages.

    Returns a dict with at least ``status`` + ``route`` (or ``reason`` when
    ignored) so the caller in ``main.py`` can emit the existing
    ``telegram_update_routed`` log line.
    """
    text = normalized.text or ""
    if _SERVICE_ADD_RE.match(text):
        return await _dispatch(
            normalized=normalized,
            api_client=api_client,
            send_dm=send_dm,
            primary_operator_username=primary_operator_username,
            admin_username=admin_username,
            internal_token=internal_token,
            route="service_add",
            handler=_handle_service_add,
        )
    if _SERVICE_LIST_RE.match(text):
        return await _dispatch(
            normalized=normalized,
            api_client=api_client,
            send_dm=send_dm,
            primary_operator_username=primary_operator_username,
            admin_username=admin_username,
            internal_token=internal_token,
            route="service_list",
            handler=_handle_service_list,
        )
    if _SERVICE_REMOVE_RE.match(text):
        return await _dispatch(
            normalized=normalized,
            api_client=api_client,
            send_dm=send_dm,
            primary_operator_username=primary_operator_username,
            admin_username=admin_username,
            internal_token=internal_token,
            route="service_remove",
            handler=_handle_service_remove,
        )
    if _SALES_STATE_RE.match(text):
        return await _dispatch(
            normalized=normalized,
            api_client=api_client,
            send_dm=send_dm,
            primary_operator_username=primary_operator_username,
            admin_username=admin_username,
            internal_token=internal_token,
            route="sales_state",
            handler=_handle_sales_state,
        )
    return None


async def _dispatch(
    *,
    normalized: NormalizedTelegramMessage,
    api_client: ApiClient,
    send_dm: SendDmFn,
    primary_operator_username: str,
    admin_username: str,
    internal_token: str,
    route: str,
    handler: Callable[..., Awaitable[dict[str, str]]],
) -> dict[str, str]:
    trace_id = f"tg-update-{normalized.update_id}"
    resolved = await resolve_operator_for_sender(
        username=normalized.username,
        api_client=api_client,
        primary_operator_username=primary_operator_username,
    )
    is_admin = (
        normalized.username is not None
        and normalized.username == admin_username
    )
    if (
        resolved is None
        or resolved.project_id is None
        or not resolved.is_active
    ) and not is_admin:
        logger.warning(
            "unauthorized_sales_command",
            extra={
                "trace_id": trace_id,
                "from_username": normalized.username,
                "route": route,
            },
        )
        return {"status": "ignored", "reason": "unauthorized_sales_command"}

    # Admin without an operator registry row still needs a project_id. The
    # admin should have one registered for themselves (per Epic 10's admin
    # bootstrap). Fall back to "ignored" with the same log line if we can't
    # find one — defensive but unreachable in the documented setup.
    if resolved is None or resolved.project_id is None:
        logger.warning(
            "unauthorized_sales_command",
            extra={
                "trace_id": trace_id,
                "from_username": normalized.username,
                "route": route,
                "note": "admin_has_no_project_mapping",
            },
        )
        return {"status": "ignored", "reason": "unauthorized_sales_command"}

    return await handler(
        normalized=normalized,
        resolved=resolved,
        api_client=api_client,
        send_dm=send_dm,
        internal_token=internal_token,
        trace_id=trace_id,
    )


# --- /service_add -----------------------------------------------------------


async def _handle_service_add(
    *,
    normalized: NormalizedTelegramMessage,
    resolved: ResolvedOperator,
    api_client: ApiClient,
    send_dm: SendDmFn,
    internal_token: str,
    trace_id: str,
) -> dict[str, str]:
    try:
        name, description = parse_service_add(normalized.text or "")
    except SalesCommandUsageError as exc:
        await send_dm(normalized.chat_id, str(exc))
        return {
            "status": "error",
            "route": "service_add",
            "decision": "usage",
        }
    try:
        body = await api_client.add_sales_service(
            project_id=int(resolved.project_id or 0),
            name=name,
            description_md=description,
            tags=None,
            internal_token=internal_token,
        )
    except ApiError as exc:
        if exc.detail == "service_already_exists":
            await send_dm(
                normalized.chat_id, f"Такая услуга уже есть: {name}."
            )
            return {
                "status": "error",
                "route": "service_add",
                "decision": "duplicate",
            }
        await _log_api_error_and_dm(
            send_dm=send_dm,
            normalized=normalized,
            trace_id=trace_id,
            route="service_add",
            exc=exc,
        )
        return {"status": "error", "route": "service_add", "detail": exc.detail or ""}
    except (httpx.HTTPStatusError, httpx.RequestError, OSError) as exc:
        await _log_api_error_and_dm(
            send_dm=send_dm,
            normalized=normalized,
            trace_id=trace_id,
            route="service_add",
            exc=exc,
        )
        return {"status": "error", "route": "service_add", "reason": "api_unreachable"}
    service_id = int(body.get("id", 0))
    await send_dm(
        normalized.chat_id, f"Добавлено: {name} (id={service_id})"
    )
    return {
        "status": "ok",
        "route": "service_add",
        "service_id": str(service_id),
    }


# --- /service_list ----------------------------------------------------------


async def _handle_service_list(
    *,
    normalized: NormalizedTelegramMessage,
    resolved: ResolvedOperator,
    api_client: ApiClient,
    send_dm: SendDmFn,
    internal_token: str,
    trace_id: str,
) -> dict[str, str]:
    try:
        body = await api_client.list_sales_services(
            project_id=int(resolved.project_id or 0),
            internal_token=internal_token,
        )
    except (ApiError, httpx.HTTPStatusError, httpx.RequestError, OSError) as exc:
        await _log_api_error_and_dm(
            send_dm=send_dm,
            normalized=normalized,
            trace_id=trace_id,
            route="service_list",
            exc=exc,
        )
        return {"status": "error", "route": "service_list"}
    services = body.get("services") or []
    if not services:
        await send_dm(normalized.chat_id, _EMPTY_LIST_HINT)
        return {
            "status": "ok",
            "route": "service_list",
            "decision": "empty",
        }
    lines = [_render_service_row(s) for s in services]
    await send_dm(normalized.chat_id, "\n".join(lines))
    return {
        "status": "ok",
        "route": "service_list",
        "count": str(len(services)),
    }


def _render_service_row(service: dict) -> str:
    name = str(service.get("name", "")).strip() or "?"
    description = (service.get("description_md") or "").strip()
    sid = service.get("id", "?")
    if description:
        return f"{sid}. {name} — {description}"
    return f"{sid}. {name}"


# --- /service_remove --------------------------------------------------------


async def _handle_service_remove(
    *,
    normalized: NormalizedTelegramMessage,
    resolved: ResolvedOperator,
    api_client: ApiClient,
    send_dm: SendDmFn,
    internal_token: str,
    trace_id: str,
) -> dict[str, str]:
    try:
        service_id = parse_service_remove(normalized.text or "")
    except SalesCommandUsageError as exc:
        await send_dm(normalized.chat_id, str(exc))
        return {
            "status": "error",
            "route": "service_remove",
            "decision": "usage",
        }
    try:
        await api_client.delete_sales_service(
            service_id=service_id, internal_token=internal_token
        )
    except ApiError as exc:
        if exc.detail == "service_not_found":
            await send_dm(normalized.chat_id, f"Не найдено: id={service_id}")
            return {
                "status": "error",
                "route": "service_remove",
                "decision": "not_found",
            }
        await _log_api_error_and_dm(
            send_dm=send_dm,
            normalized=normalized,
            trace_id=trace_id,
            route="service_remove",
            exc=exc,
        )
        return {
            "status": "error",
            "route": "service_remove",
            "detail": exc.detail or "",
        }
    except (httpx.HTTPStatusError, httpx.RequestError, OSError) as exc:
        await _log_api_error_and_dm(
            send_dm=send_dm,
            normalized=normalized,
            trace_id=trace_id,
            route="service_remove",
            exc=exc,
        )
        return {"status": "error", "route": "service_remove"}
    await send_dm(normalized.chat_id, f"Удалено: id={service_id}")
    return {
        "status": "ok",
        "route": "service_remove",
        "service_id": str(service_id),
    }


# --- /sales_state -----------------------------------------------------------


async def _handle_sales_state(
    *,
    normalized: NormalizedTelegramMessage,
    resolved: ResolvedOperator,
    api_client: ApiClient,
    send_dm: SendDmFn,
    internal_token: str,
    trace_id: str,
) -> dict[str, str]:
    customer_arg = parse_sales_state(normalized.text or "")
    chat_id_filter: int | None = None
    if customer_arg is not None:
        customer = await resolve_operator_for_sender(
            username=customer_arg,
            api_client=api_client,
            primary_operator_username="",
        )
        if customer is None or customer.chat_id is None:
            await send_dm(
                normalized.chat_id,
                f"Не нашёл такого собеседника: {customer_arg}.",
            )
            return {
                "status": "error",
                "route": "sales_state",
                "decision": "unknown_customer",
            }
        chat_id_filter = int(customer.chat_id)
    try:
        body = await api_client.get_sales_state(
            project_id=int(resolved.project_id or 0),
            chat_id=chat_id_filter,
            internal_token=internal_token,
        )
    except (ApiError, httpx.HTTPStatusError, httpx.RequestError, OSError) as exc:
        await _log_api_error_and_dm(
            send_dm=send_dm,
            normalized=normalized,
            trace_id=trace_id,
            route="sales_state",
            exc=exc,
        )
        return {"status": "error", "route": "sales_state"}
    states = body.get("states") or []
    if not states:
        await send_dm(normalized.chat_id, _NO_ACTIVE_CHATS)
        return {
            "status": "ok",
            "route": "sales_state",
            "decision": "empty",
        }
    lines = [_render_state_row(s) for s in states]
    await send_dm(normalized.chat_id, "\n".join(lines))
    return {
        "status": "ok",
        "route": "sales_state",
        "count": str(len(states)),
    }


def _render_state_row(state: dict) -> str:
    chat_id = state.get("chat_id", "?")
    stage = state.get("current_stage", "?")
    intent = state.get("collected_intent") or {}
    intent_text = json.dumps(intent, ensure_ascii=False, sort_keys=True)
    last_msg_iso = state.get("last_customer_msg_at") or state.get(
        "last_bot_msg_at"
    )
    last_msg = _format_hhmm(last_msg_iso) if last_msg_iso else "—"
    return (
        f"chat={chat_id} stage={stage} intent={intent_text} "
        f"last_msg={last_msg}"
    )


def _format_hhmm(iso_string: str) -> str:
    """Render an ISO timestamp as ``HH:MM``; falls back to the raw string."""
    from datetime import datetime

    try:
        parsed = datetime.fromisoformat(iso_string)
    except ValueError:
        return iso_string
    return parsed.strftime("%H:%M")


# --- shared error helper ---------------------------------------------------


async def _log_api_error_and_dm(
    *,
    send_dm: SendDmFn,
    normalized: NormalizedTelegramMessage,
    trace_id: str,
    route: str,
    exc: Exception,
) -> None:
    status = 0
    detail: str | None = None
    if isinstance(exc, ApiError):
        detail = exc.detail
        if exc.response is not None:
            status = exc.response.status_code
    elif isinstance(exc, httpx.HTTPStatusError):
        if exc.response is not None:
            status = exc.response.status_code
    logger.warning(
        "sales_command_api_error",
        extra={
            "trace_id": trace_id,
            "route": route,
            "from_username": normalized.username,
            "status": status,
            "detail": detail,
            "error": str(exc),
        },
    )
    await send_dm(normalized.chat_id, _API_UNAVAILABLE)

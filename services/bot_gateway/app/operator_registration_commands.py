from __future__ import annotations

import logging
import re
from collections.abc import Awaitable, Callable
from typing import Any

import httpx

from services.bot_gateway.app.api_client import ApiClient, ApiError
from services.bot_gateway.app.operator_onboarding_messages import (
    REGISTER_TEXT_PLATFORM_ADMIN,
    START_TEXT_ADMIN,
    START_TEXT_GUEST,
    START_TEXT_OPERATOR,
)
from services.bot_gateway.app.telegram_update import NormalizedTelegramMessage

logger = logging.getLogger(__name__)

SendDmFn = Callable[[int, str], Awaitable[Any]]

_REGISTER_RE = re.compile(r"^/register(?:@\w+)?(?:\s+(.+))?$", re.IGNORECASE)
_START_RE = re.compile(r"^\s*/start(?:@\w+)?(?:\s+.*)?$", re.IGNORECASE)

_REGISTER_OK = "Заявка отправлена. Администратор получит уведомление."
_REGISTER_PENDING = "У вас уже есть активная заявка."
_REGISTER_COOLDOWN = "Заявка была отклонена. Повторная подача возможна через 24 часа."
_REGISTER_ALREADY_OPERATOR = "Вы уже зарегистрированы как оператор."
_REGISTER_FAILED = "Не удалось отправить заявку. Попробуйте позже."


async def handle_start_command(
    *,
    normalized: NormalizedTelegramMessage,
    is_operator: bool,
    is_platform_admin: bool,
    send_dm: SendDmFn,
) -> dict[str, str] | None:
    if not _START_RE.match(normalized.text or ""):
        return None
    if is_platform_admin:
        text = START_TEXT_ADMIN
    elif is_operator:
        text = START_TEXT_OPERATOR
    else:
        text = START_TEXT_GUEST
    await send_dm(normalized.chat_id, text)
    return {"status": "accepted", "route": "start_command", "decision": "sent"}


async def handle_register_command(
    *,
    normalized: NormalizedTelegramMessage,
    is_operator: bool,
    is_platform_admin: bool,
    api_client: ApiClient,
    send_dm: SendDmFn,
) -> dict[str, str] | None:
    match = _REGISTER_RE.match(normalized.text or "")
    if match is None:
        return None

    if is_platform_admin:
        await send_dm(normalized.chat_id, REGISTER_TEXT_PLATFORM_ADMIN)
        return {
            "status": "accepted",
            "route": "register_command",
            "decision": "platform_admin",
        }

    if is_operator:
        await send_dm(normalized.chat_id, _REGISTER_ALREADY_OPERATOR)
        return {"status": "accepted", "route": "register_command", "decision": "already_operator"}

    username = normalized.username
    if username is None:
        await send_dm(normalized.chat_id, _REGISTER_FAILED)
        return {"status": "accepted", "route": "register_command", "decision": "missing_username"}

    display_name_raw = match.group(1) or ""
    display_name = display_name_raw.strip() or None
    try:
        result = await api_client.create_operator_register_request(
            username=username,
            chat_id=normalized.chat_id,
            display_name=display_name,
        )
    except ApiError as exc:
        if exc.detail == "registration_pending":
            await send_dm(normalized.chat_id, _REGISTER_PENDING)
            return {"status": "accepted", "route": "register_command", "decision": "pending"}
        if exc.detail == "registration_cooldown":
            await send_dm(normalized.chat_id, _REGISTER_COOLDOWN)
            return {"status": "accepted", "route": "register_command", "decision": "cooldown"}
        if exc.detail == "already_operator":
            await send_dm(normalized.chat_id, _REGISTER_ALREADY_OPERATOR)
            return {
                "status": "accepted",
                "route": "register_command",
                "decision": "already_operator",
            }
        if exc.detail == "platform_admin_not_operator":
            await send_dm(normalized.chat_id, REGISTER_TEXT_PLATFORM_ADMIN)
            return {
                "status": "accepted",
                "route": "register_command",
                "decision": "platform_admin",
            }
        logger.warning(
            "operator_register_request_failed",
            extra={"username": username, "detail": exc.detail},
        )
        await send_dm(normalized.chat_id, _REGISTER_FAILED)
        return {"status": "accepted", "route": "register_command", "decision": "api_error"}
    except (httpx.HTTPStatusError, httpx.RequestError):
        logger.warning("operator_register_request_failed", extra={"username": username})
        await send_dm(normalized.chat_id, _REGISTER_FAILED)
        return {"status": "accepted", "route": "register_command", "decision": "api_error"}

    request_id = str(result.get("request_id", ""))
    logger.info(
        "operator_register_requested",
        extra={"username": username, "request_id": request_id},
    )
    await send_dm(normalized.chat_id, _REGISTER_OK)
    return {"status": "accepted", "route": "register_command", "decision": "created"}

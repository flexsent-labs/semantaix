from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from typing import Any

import httpx

from services.bot_gateway.app.api_client import ApiClient, ApiError
from services.bot_gateway.app.operator_telegram_link import start_operator_telegram_link
from services.bot_gateway.app.telegram_callback import NormalizedCallbackQuery
from services.bot_gateway.app.user_gateway_client import UserGatewayClient

logger = logging.getLogger(__name__)

SendDmFn = Callable[[int, str], Awaitable[Any]]

_CALENDAR_CONNECT_INSTRUCTION = (
    "🔗 Чтобы подключить календарь, откройте ссылку и разрешите доступ "
    "(только чтение занятости):\n{consent_url}\n\n"
    "После подтверждения вернитесь в Telegram — доступ заработает автоматически, "
    "и календарь включится для вашего проекта."
)
_CALENDAR_FALLBACK = "Не получилось начать подключение календаря — попробуйте чуть позже."
_TG_FALLBACK = "Сначала дождитесь обновления сервера (story 16-06)."


async def handle_onboarding_callback(
    normalized: NormalizedCallbackQuery,
    action: str,
    arg: str,
    *,
    api_client: ApiClient,
    user_gateway_client: UserGatewayClient,
    send_dm: SendDmFn,
    telegram_bot_sender: Any,
    internal_token: str,
) -> dict[str, str]:
    try:
        operator_id = int(arg)
    except ValueError:
        return {
            "status": "ignored",
            "route": "onboard_callback",
            "reason": "invalid_operator_id",
            "answer_text": "",
        }

    operator = await api_client.get_operator_by_id(operator_id=operator_id)
    if operator is None:
        return {
            "status": "ignored",
            "route": "onboard_callback",
            "reason": "operator_not_found",
            "answer_text": "",
        }
    sender_username = normalized.sender_username or ""
    owner_username = str(operator.get("username") or "")
    if sender_username != owner_username:
        logger.warning(
            "onboarding_callback_owner_mismatch",
            extra={"sender_username": sender_username, "owner_username": owner_username},
        )
        return {
            "status": "ignored",
            "route": "onboard_callback",
            "reason": "onboarding_callback_owner_mismatch",
            "answer_text": "",
        }

    if action == "cal":
        return await _handle_calendar(
            operator=operator,
            operator_id=operator_id,
            normalized=normalized,
            api_client=api_client,
            send_dm=send_dm,
            internal_token=internal_token,
        )
    if action == "tg":
        return await _handle_telegram(
            operator=operator,
            operator_id=operator_id,
            api_client=api_client,
            user_gateway_client=user_gateway_client,
            send_dm=send_dm,
            telegram_bot_sender=telegram_bot_sender,
        )
    return {
        "status": "ignored",
        "route": "onboard_callback",
        "reason": "unknown_action",
        "answer_text": "",
    }


async def _handle_calendar(
    *,
    operator: dict[str, object],
    operator_id: int,
    normalized: NormalizedCallbackQuery,
    api_client: ApiClient,
    send_dm: SendDmFn,
    internal_token: str,
) -> dict[str, str]:
    project_id_raw = operator.get("project_id")
    username = str(operator.get("username") or "")
    if not isinstance(project_id_raw, int) or not username:
        await send_dm(normalized.chat_id, _CALENDAR_FALLBACK)
        return {
            "status": "accepted",
            "route": "onboard_callback",
            "decision": "calendar_missing_operator_fields",
            "answer_text": "Ошибка",
        }
    try:
        connect = await api_client.initiate_calendar_connect(
            project_id=project_id_raw,
            operator=username,
            internal_token=internal_token,
        )
    except (ApiError, httpx.HTTPStatusError, httpx.RequestError):
        await send_dm(normalized.chat_id, _CALENDAR_FALLBACK)
        return {
            "status": "accepted",
            "route": "onboard_callback",
            "decision": "calendar_connect_failed",
            "answer_text": "Ошибка",
        }

    consent_url = str(connect.get("consent_url") or "")
    if not consent_url:
        await send_dm(normalized.chat_id, _CALENDAR_FALLBACK)
        return {
            "status": "accepted",
            "route": "onboard_callback",
            "decision": "calendar_missing_url",
            "answer_text": "Ошибка",
        }
    await send_dm(normalized.chat_id, _CALENDAR_CONNECT_INSTRUCTION.format(consent_url=consent_url))
    await api_client.record_onboarding_event(
        operator_id=operator_id,
        event_type="calendar_started",
    )
    return {
        "status": "accepted",
        "route": "onboard_callback",
        "decision": "calendar_started",
        "answer_text": "Ссылка отправлена",
    }


async def _handle_telegram(
    *,
    operator: dict[str, object],
    operator_id: int,
    api_client: ApiClient,
    user_gateway_client: UserGatewayClient,
    send_dm: SendDmFn,
    telegram_bot_sender: Any,
) -> dict[str, str]:
    operator_chat_id = operator.get("chat_id")
    if not isinstance(operator_chat_id, int):
        await send_dm(operator_id, _TG_FALLBACK)
        return {
            "status": "accepted",
            "route": "onboard_callback",
            "decision": "telegram_missing_chat_id",
            "answer_text": "Ошибка",
        }
    await api_client.record_onboarding_event(
        operator_id=operator_id,
        event_type="telegram_link_started",
    )
    result = await start_operator_telegram_link(
        operator_id=operator_id,
        operator_chat_id=operator_chat_id,
        user_gateway_client=user_gateway_client,
        send_dm=send_dm,
        telegram_bot_sender=telegram_bot_sender,
        api_client=api_client,
    )
    decision = result.get("decision", "telegram_started")
    return {
        "status": "accepted",
        "route": "onboard_callback",
        "decision": str(decision),
        "answer_text": "Запущено",
    }

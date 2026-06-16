from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from typing import Any

import httpx

from services.bot_gateway.app.api_client import ApiClient, ApiError
from services.bot_gateway.app.telegram_callback import NormalizedCallbackQuery

logger = logging.getLogger(__name__)

SendDmFn = Callable[[int, str], Awaitable[Any]]

_APPROVED_DM = "✓ Вы зарегистрированы как оператор."
_REJECTED_DM = "✗ Заявка отклонена. Повторная подача возможна через 24 часа."


async def handle_operator_registration_callback(
    normalized: NormalizedCallbackQuery,
    action: str,
    arg: str,
    *,
    api_client: ApiClient,
    send_dm: SendDmFn,
    telegram_bot_sender: Any,
    admin_username: str,
) -> dict[str, str]:
    sender = normalized.sender_username
    if sender != admin_username:
        logger.warning(
            "unauthorized_op_reg_callback",
            extra={"sender_username": sender, "action": action},
        )
        return {
            "status": "ignored",
            "route": "op_reg_callback",
            "reason": "unauthorized_op_reg_callback",
            "answer_text": "",
        }

    try:
        request_id = int(arg)
    except ValueError:
        return {
            "status": "ignored",
            "route": "op_reg_callback",
            "reason": "invalid_request_id",
            "answer_text": "",
        }

    if action == "approve":
        return await _approve(
            normalized=normalized,
            request_id=request_id,
            api_client=api_client,
            send_dm=send_dm,
            telegram_bot_sender=telegram_bot_sender,
        )
    if action == "reject":
        return await _reject(
            normalized=normalized,
            request_id=request_id,
            api_client=api_client,
            send_dm=send_dm,
            telegram_bot_sender=telegram_bot_sender,
        )
    return {
        "status": "ignored",
        "route": "op_reg_callback",
        "reason": "unknown_action",
        "answer_text": "",
    }


async def _clear_markup(normalized: NormalizedCallbackQuery, telegram_bot_sender: Any) -> None:
    if normalized.source_message_id is None:
        return
    await telegram_bot_sender.edit_message_reply_markup(
        chat_id=normalized.chat_id,
        message_id=normalized.source_message_id,
        reply_markup=None,
    )


async def _approve(
    *,
    normalized: NormalizedCallbackQuery,
    request_id: int,
    api_client: ApiClient,
    send_dm: SendDmFn,
    telegram_bot_sender: Any,
) -> dict[str, str]:
    answer_text = "Одобрено"
    try:
        operator = await api_client.approve_operator_register_request(request_id=request_id)
    except ApiError as exc:
        if exc.detail == "request_not_pending":
            await _clear_markup(normalized, telegram_bot_sender)
            return {
                "status": "accepted",
                "route": "op_reg_callback",
                "decision": "already_processed",
                "answer_text": "Уже обработано",
            }
        if exc.detail == "request_not_found":
            return {
                "status": "ignored",
                "route": "op_reg_callback",
                "reason": "request_not_found",
                "answer_text": "Не найдено",
            }
        logger.warning(
            "op_reg_approve_failed",
            extra={"request_id": request_id, "detail": exc.detail},
        )
        return {
            "status": "accepted",
            "route": "op_reg_callback",
            "decision": "api_error",
            "answer_text": "Ошибка",
        }
    except (httpx.HTTPStatusError, httpx.RequestError):
        logger.warning("op_reg_approve_failed", extra={"request_id": request_id})
        return {
            "status": "accepted",
            "route": "op_reg_callback",
            "decision": "api_error",
            "answer_text": "Ошибка",
        }

    chat_id = operator.get("chat_id")
    if isinstance(chat_id, int):
        await send_dm(chat_id, _APPROVED_DM)
    await _clear_markup(normalized, telegram_bot_sender)
    return {
        "status": "accepted",
        "route": "op_reg_callback",
        "decision": "approved",
        "answer_text": answer_text,
    }


async def _reject(
    *,
    normalized: NormalizedCallbackQuery,
    request_id: int,
    api_client: ApiClient,
    send_dm: SendDmFn,
    telegram_bot_sender: Any,
) -> dict[str, str]:
    answer_text = "Отклонено"
    try:
        request = await api_client.reject_operator_register_request(request_id=request_id)
    except ApiError as exc:
        if exc.detail == "request_not_pending":
            await _clear_markup(normalized, telegram_bot_sender)
            return {
                "status": "accepted",
                "route": "op_reg_callback",
                "decision": "already_processed",
                "answer_text": "Уже обработано",
            }
        if exc.detail == "request_not_found":
            return {
                "status": "ignored",
                "route": "op_reg_callback",
                "reason": "request_not_found",
                "answer_text": "Не найдено",
            }
        logger.warning(
            "op_reg_reject_failed",
            extra={"request_id": request_id, "detail": exc.detail},
        )
        return {
            "status": "accepted",
            "route": "op_reg_callback",
            "decision": "api_error",
            "answer_text": "Ошибка",
        }
    except (httpx.HTTPStatusError, httpx.RequestError):
        logger.warning("op_reg_reject_failed", extra={"request_id": request_id})
        return {
            "status": "accepted",
            "route": "op_reg_callback",
            "decision": "api_error",
            "answer_text": "Ошибка",
        }

    chat_id = request.get("chat_id")
    if isinstance(chat_id, int):
        await send_dm(chat_id, _REJECTED_DM)
    await _clear_markup(normalized, telegram_bot_sender)
    return {
        "status": "accepted",
        "route": "op_reg_callback",
        "decision": "rejected",
        "answer_text": answer_text,
    }

from __future__ import annotations

import asyncio
import base64
import logging
import tempfile
from pathlib import Path
from typing import Any

from services.bot_gateway.app.api_client import ApiClient
from services.bot_gateway.app.user_gateway_client import UserGatewayClient, UserGatewayError

logger = logging.getLogger(__name__)

_QR_CAPTION = "Сканируйте QR-код в Telegram. Код действует 30 секунд."
_2FA_PROMPT = "Требуется 2FA. Ответьте сообщением с паролем двухфакторной аутентификации."
_TIMEOUT_DM = "Не удалось завершить привязку. Запустите кнопку ещё раз."
_SUCCESS_DM = (
    "✓ Аккаунт привязан. Клиенты могут писать вам в личные сообщения — "
    "ответы будут приходить с этого аккаунта."
)


async def start_operator_telegram_link(
    *,
    operator_id: int,
    operator_chat_id: int,
    user_gateway_client: UserGatewayClient,
    send_dm,
    telegram_bot_sender: Any,
    api_client: ApiClient | None = None,
    poll_interval_seconds: float = 3.0,
    max_polls: int = 30,
    get_2fa_password=None,
) -> dict[str, str]:
    try:
        started = await user_gateway_client.qr_start(operator_id=operator_id)
    except UserGatewayError as exc:
        if exc.detail == "already_authenticated":
            await send_dm(operator_chat_id, _SUCCESS_DM)
            return {"status": "accepted", "decision": "already_authenticated"}
        await send_dm(operator_chat_id, "Не удалось начать привязку Telegram. Попробуйте позже.")
        return {"status": "accepted", "decision": "qr_start_failed"}

    qr_image_b64 = str(started.get("qr_image_b64") or "")
    if qr_image_b64:
        await _send_qr_document(
            qr_image_b64=qr_image_b64,
            operator_chat_id=operator_chat_id,
            telegram_bot_sender=telegram_bot_sender,
        )

    need_password_prompt = True
    for _ in range(max_polls):
        status = await user_gateway_client.status(operator_id=operator_id)
        phase = str(status.get("phase") or "")
        if phase == "authenticated":
            await send_dm(operator_chat_id, _SUCCESS_DM)
            if api_client is not None:
                await api_client.record_onboarding_event(
                    operator_id=operator_id,
                    event_type="telegram_link_connected",
                )
            return {"status": "accepted", "decision": "connected"}
        if phase == "2fa_pending":
            if need_password_prompt:
                await send_dm(operator_chat_id, _2FA_PROMPT)
                need_password_prompt = False
            if get_2fa_password is None:
                return {"status": "accepted", "decision": "awaiting_2fa"}
            password = await get_2fa_password()
            if not password:
                await asyncio.sleep(poll_interval_seconds)
                continue
            try:
                await user_gateway_client.verify_2fa(
                    operator_id=operator_id, password=password
                )
            except UserGatewayError as exc:
                if exc.detail == "invalid_password":
                    await send_dm(operator_chat_id, "Неверный пароль 2FA. Попробуйте ещё раз.")
                else:
                    await send_dm(operator_chat_id, "Не удалось проверить 2FA. Попробуйте позже.")
                    return {"status": "accepted", "decision": "verify_2fa_failed"}
        await asyncio.sleep(poll_interval_seconds)

    await send_dm(operator_chat_id, _TIMEOUT_DM)
    return {"status": "accepted", "decision": "timeout"}


async def _send_qr_document(
    *,
    qr_image_b64: str,
    operator_chat_id: int,
    telegram_bot_sender: Any,
) -> None:
    try:
        qr_bytes = base64.b64decode(qr_image_b64)
    except Exception:
        logger.warning("operator_link_qr_decode_failed")
        return

    temp_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(delete=False, suffix=".png") as tmp:
            tmp.write(qr_bytes)
            temp_path = Path(tmp.name)
        await telegram_bot_sender.send_document(
            chat_id=operator_chat_id,
            local_path=temp_path,
            caption=_QR_CAPTION,
        )
    except Exception:
        logger.warning("operator_link_qr_send_failed", exc_info=True)
    finally:
        if temp_path is not None:
            temp_path.unlink(missing_ok=True)

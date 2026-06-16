from __future__ import annotations

from services.api.app.operator_registration import (
    OperatorRegistrationRepository,
    RegistrationRequest,
)
from services.api.app.operators import Operator
from services.api.app.telegram_bot_sender import TelegramBotSender


def _admin_reply_markup(request_id: int) -> dict[str, object]:
    return {
        "inline_keyboard": [
            [
                {
                    "text": "✓ Одобрить",
                    "callback_data": f"op_reg:approve:{request_id}",
                },
                {
                    "text": "✗ Отклонить",
                    "callback_data": f"op_reg:reject:{request_id}",
                },
            ]
        ]
    }


def _onboarding_reply_markup(operator_id: int) -> dict[str, object]:
    return {
        "inline_keyboard": [
            [
                {
                    "text": "📅 Подключить Google Calendar",
                    "callback_data": f"onboard:cal:{operator_id}",
                },
                {
                    "text": "📱 Привязать Telegram-аккаунт",
                    "callback_data": f"onboard:tg:{operator_id}",
                },
            ]
        ]
    }


def build_operator_registration_notifier(
    *,
    telegram_sender: TelegramBotSender,
    registration_repository: OperatorRegistrationRepository,
    admin_chat_id_getter,
) -> tuple:
    async def notify_admin_new_request(request: RegistrationRequest) -> None:
        chat_id = admin_chat_id_getter()
        if chat_id is None:
            return
        display = request.display_name or "—"
        text = (
            "Новая заявка оператора:\n"
            f"username: {request.username}\n"
            f"chat_id: {request.chat_id}\n"
            f"имя: {display}"
        )
        await telegram_sender.send_message(
            chat_id=chat_id,
            text=text,
            reply_markup=_admin_reply_markup(request.id),
        )

    async def send_onboarding_dm(*, operator: Operator, request_id: int) -> None:
        if operator.chat_id is None:
            return
        text = (
            "Добро пожаловать! Рекомендуемые шаги настройки:\n\n"
            "1. Подключите Google Calendar — для ответов о свободном времени.\n"
            "2. Привяжите Telegram-аккаунт — это линия, на которую будут "
            "писать клиенты. Ответы клиентам будут приходить с этого аккаунта.\n\n"
            "Нажмите кнопку ниже:"
        )
        await telegram_sender.send_message(
            chat_id=operator.chat_id,
            text=text,
            reply_markup=_onboarding_reply_markup(operator.id),
        )
        registration_repository.record_onboarding_event(
            operator_id=operator.id,
            event_type="onboarding_sent",
        )

    return notify_admin_new_request, send_onboarding_dm

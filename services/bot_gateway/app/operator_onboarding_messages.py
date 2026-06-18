"""Guest-facing copy and Telegram command menu for operator self-registration."""

from __future__ import annotations

GUEST_HELP_TEXT = (
    "👋 Semantaix — бот для операторов.\n"
    "\n"
    "Чтобы начать работу:\n"
    "1. /register — подать заявку на регистрацию оператора.\n"
    "2. Дождитесь одобрения администратора в Telegram.\n"
    "3. После одобрения подключите календарь: /connect_calendar.\n"
    "\n"
    "Полезные команды:\n"
    "• /register [Имя] — заявка оператора (имя необязательно).\n"
    "• /whoami — ваш @username и статус регистрации.\n"
    "• /help — эта справка.\n"
    "\n"
    "Клиентские вопросы обрабатываются на линии оператора после настройки."
)

START_TEXT_GUEST = (
    "Привет! Я Semantaix — помогаю операторам отвечать клиентам.\n"
    "\n"
    "Чтобы получить доступ, отправьте /register — заявка уйдёт администратору "
    "на подтверждение. После одобрения вы сможете подключить Google Calendar "
    "командой /connect_calendar.\n"
    "\n"
    "Справка: /help"
)

START_TEXT_OPERATOR = (
    "С возвращением! Вы зарегистрированы как оператор.\n"
    "\n"
    "• /connect_calendar — подключить Google Calendar.\n"
    "• /help — полный список команд.\n"
    "• /whoami — проверить статус."
)

START_TEXT_ADMIN = (
    "Привет! Вы вошли как администратор платформы Semantaix.\n"
    "\n"
    "Ваш аккаунт не является оператором — вы одобряете заявки других "
    "пользователей через кнопки в личных сообщениях после их /register.\n"
    "\n"
    "Операторы после одобрения подключают календарь сами: /connect_calendar."
)

REGISTER_TEXT_PLATFORM_ADMIN = (
    "Вы администратор платформы — регистрироваться как оператор не нужно. "
    "Новые заявки приходят вам в личные сообщения после /register от других "
    "пользователей."
)

CALENDAR_REGISTER_HINT = (
    "Команда доступна только зарегистрированным операторам.\n"
    "Отправьте /register — заявка уйдёт администратору на подтверждение."
)


def telegram_command_menu() -> list[dict[str, str]]:
    """Bot API ``setMyCommands`` payload (default scope — all private chats)."""
    return [
        {"command": "start", "description": "Начать и узнать, как зарегистрироваться"},
        {"command": "register", "description": "Подать заявку оператора"},
        {"command": "help", "description": "Справка по командам"},
        {"command": "whoami", "description": "Мой @username и статус"},
        {
            "command": "connect_calendar",
            "description": "Подключить Google Calendar (после одобрения)",
        },
    ]

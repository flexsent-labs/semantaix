from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class NormalizedCallbackQuery:
    update_id: int
    callback_query_id: str
    chat_id: int
    sender_username: str | None
    sender_user_id: int
    data: str
    source_message_id: int | None = None


def normalize_callback_query(payload: dict[str, Any]) -> NormalizedCallbackQuery | None:
    """Normalize Telegram callback_query payloads.

    Returns None for non-callback updates; raises ValueError for malformed
    callback updates.
    """
    update_id = payload.get("update_id")
    if not isinstance(update_id, int):
        raise ValueError("missing_or_invalid_update_id")

    callback_query = payload.get("callback_query")
    if callback_query is None:
        return None
    if not isinstance(callback_query, dict):
        raise ValueError("invalid_callback_query")

    callback_query_id = callback_query.get("id")
    if not isinstance(callback_query_id, str) or not callback_query_id:
        raise ValueError("missing_or_invalid_callback_query_id")

    from_user = callback_query.get("from")
    if not isinstance(from_user, dict):
        raise ValueError("missing_or_invalid_from")
    sender_user_id = from_user.get("id")
    if not isinstance(sender_user_id, int):
        raise ValueError("missing_or_invalid_sender_user_id")
    raw_username = from_user.get("username")
    sender_username = (
        f"@{raw_username}" if isinstance(raw_username, str) and raw_username else None
    )

    raw_data = callback_query.get("data")
    if not isinstance(raw_data, str) or not raw_data:
        raise ValueError("missing_or_invalid_callback_data")

    source_message_id: int | None = None
    chat_id: int | None = None
    message = callback_query.get("message")
    if isinstance(message, dict):
        message_id = message.get("message_id")
        if isinstance(message_id, int):
            source_message_id = message_id
        chat = message.get("chat")
        if isinstance(chat, dict):
            chat_id_value = chat.get("id")
            if isinstance(chat_id_value, int):
                chat_id = chat_id_value

    if chat_id is None:
        # Some callback payloads omit "message" (e.g. old fixture shape). For
        # private chats we can still route via sender id.
        chat_id = sender_user_id

    return NormalizedCallbackQuery(
        update_id=update_id,
        callback_query_id=callback_query_id,
        chat_id=chat_id,
        sender_username=sender_username,
        sender_user_id=sender_user_id,
        data=raw_data,
        source_message_id=source_message_id,
    )

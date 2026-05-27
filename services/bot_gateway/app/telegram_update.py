from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

AttachmentKind = Literal["document", "photo", "video", "audio", "voice"]


class TelegramUpdateValidationError(ValueError):
    pass


@dataclass(frozen=True)
class TelegramAttachment:
    file_id: str
    kind: AttachmentKind
    mime_type: str | None = None
    file_size: int | None = None
    file_name: str | None = None


@dataclass(frozen=True)
class NormalizedTelegramMessage:
    update_id: int
    source_message_id: int
    chat_id: int
    user_id: int
    username: str | None
    text: str
    reply_to_text: str | None = None
    caption: str | None = None
    media_group_id: str | None = None
    attachments: tuple[TelegramAttachment, ...] = ()
    # Story 12.05 — surface the first attachment of the replied-to message so
    # ``/material`` (a reply to media) can extract it without re-parsing the
    # raw Telegram payload. Stays ``None`` for non-reply or text-only replies.
    reply_to_attachment: TelegramAttachment | None = None
    reply_to_caption: str | None = None


def _extract_document(message: dict[str, Any]) -> TelegramAttachment | None:
    document = message.get("document")
    if not isinstance(document, dict):
        return None
    file_id = document.get("file_id")
    if not isinstance(file_id, str) or not file_id:
        return None
    file_size = document.get("file_size")
    mime_type = document.get("mime_type")
    file_name = document.get("file_name")
    return TelegramAttachment(
        file_id=file_id,
        kind="document",
        mime_type=mime_type if isinstance(mime_type, str) else None,
        file_size=file_size if isinstance(file_size, int) else None,
        file_name=file_name if isinstance(file_name, str) else None,
    )


def _extract_photo(message: dict[str, Any]) -> TelegramAttachment | None:
    photos = message.get("photo")
    if not isinstance(photos, list) or not photos:
        return None
    largest: dict[str, Any] | None = None
    largest_size = -1
    for photo in photos:
        if not isinstance(photo, dict):
            continue
        size = photo.get("file_size")
        if not isinstance(size, int):
            size = (photo.get("width") or 0) * (photo.get("height") or 0)
        if size > largest_size:
            largest_size = size
            largest = photo
    if largest is None:
        return None
    file_id = largest.get("file_id")
    if not isinstance(file_id, str) or not file_id:
        return None
    file_size = largest.get("file_size")
    return TelegramAttachment(
        file_id=file_id,
        kind="photo",
        mime_type="image/jpeg",
        file_size=file_size if isinstance(file_size, int) else None,
        file_name=None,
    )


def _extract_simple_media(
    message: dict[str, Any],
    field_name: str,
    kind: AttachmentKind,
) -> TelegramAttachment | None:
    media = message.get(field_name)
    if not isinstance(media, dict):
        return None
    file_id = media.get("file_id")
    if not isinstance(file_id, str) or not file_id:
        return None
    file_size = media.get("file_size")
    mime_type = media.get("mime_type")
    file_name = media.get("file_name")
    return TelegramAttachment(
        file_id=file_id,
        kind=kind,
        mime_type=mime_type if isinstance(mime_type, str) else None,
        file_size=file_size if isinstance(file_size, int) else None,
        file_name=file_name if isinstance(file_name, str) else None,
    )


def _collect_attachments(message: dict[str, Any]) -> tuple[TelegramAttachment, ...]:
    out: list[TelegramAttachment] = []
    document = _extract_document(message)
    if document:
        out.append(document)
    photo = _extract_photo(message)
    if photo:
        out.append(photo)
    for field_name, kind in (("video", "video"), ("audio", "audio"), ("voice", "voice")):
        media = _extract_simple_media(message, field_name, kind)  # type: ignore[arg-type]
        if media:
            out.append(media)
    return tuple(out)


def normalize_update(payload: dict[str, Any]) -> NormalizedTelegramMessage | None:
    """Normalize supported Telegram updates.

    Returns:
      - NormalizedTelegramMessage when the update carries a usable text body OR one
        or more recognized attachments (document/photo/video/audio/voice). When the
        message has only attachments, `text` is set to "" and the caption (if any)
        is exposed separately.
      - None when the update is valid but intentionally ignored (callback_query,
        edited_message, fully empty messages, etc.).
    Raises:
      - TelegramUpdateValidationError for malformed payloads.
    """
    update_id = payload.get("update_id")
    if not isinstance(update_id, int):
        raise TelegramUpdateValidationError("missing_or_invalid_update_id")

    message = payload.get("message")
    if message is None:
        return None
    if not isinstance(message, dict):
        raise TelegramUpdateValidationError("invalid_message_object")

    message_id = message.get("message_id")
    if not isinstance(message_id, int):
        raise TelegramUpdateValidationError("missing_or_invalid_message_id")

    chat = message.get("chat")
    if not isinstance(chat, dict):
        raise TelegramUpdateValidationError("missing_or_invalid_chat")
    chat_id = chat.get("id")
    if not isinstance(chat_id, int):
        raise TelegramUpdateValidationError("missing_or_invalid_chat_id")

    from_user = message.get("from")
    if not isinstance(from_user, dict):
        raise TelegramUpdateValidationError("missing_or_invalid_from")
    user_id = from_user.get("id")
    if not isinstance(user_id, int):
        raise TelegramUpdateValidationError("missing_or_invalid_user_id")
    username = from_user.get("username")
    normalized_username = f"@{username}" if isinstance(username, str) and username else None

    text_field = message.get("text")
    text_value: str = ""
    if isinstance(text_field, str):
        text_value = text_field.strip()

    caption_field = message.get("caption")
    caption_value: str | None = None
    if isinstance(caption_field, str) and caption_field.strip():
        caption_value = caption_field

    media_group_id_field = message.get("media_group_id")
    media_group_id: str | None = None
    if isinstance(media_group_id_field, str) and media_group_id_field:
        media_group_id = media_group_id_field

    attachments = _collect_attachments(message)

    if text_value == "" and not attachments:
        return None

    reply_to_text: str | None = None
    reply_to_attachment: TelegramAttachment | None = None
    reply_to_caption: str | None = None
    reply_to = message.get("reply_to_message")
    if isinstance(reply_to, dict):
        candidate = reply_to.get("text")
        if isinstance(candidate, str) and candidate.strip():
            reply_to_text = candidate
        reply_attachments = _collect_attachments(reply_to)
        if reply_attachments:
            reply_to_attachment = reply_attachments[0]
        reply_caption_field = reply_to.get("caption")
        if isinstance(reply_caption_field, str) and reply_caption_field.strip():
            reply_to_caption = reply_caption_field

    return NormalizedTelegramMessage(
        update_id=update_id,
        source_message_id=message_id,
        chat_id=chat_id,
        user_id=user_id,
        username=normalized_username,
        text=text_value,
        reply_to_text=reply_to_text,
        caption=caption_value,
        media_group_id=media_group_id,
        attachments=attachments,
        reply_to_attachment=reply_to_attachment,
        reply_to_caption=reply_to_caption,
    )

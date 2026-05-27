import asyncio
import logging
import re
import uuid
from datetime import UTC, datetime
from pathlib import Path

import httpx
from fastapi import BackgroundTasks, HTTPException, Request

from platform_common.app_factory import create_service_app
from platform_common.settings import get_settings
from services.api.app.hitl import HitlTicketRepository
from services.api.app.openrouter_client import OpenRouterClient
from services.api.app.russian_text import get_russian_normalizer
from services.api.app.telegram_bot_sender import TelegramBotSender
from services.bot_gateway.app.admin_commands import handle_admin_project_command
from services.bot_gateway.app.admin_nl_dialog import handle_admin_nl_dialog
from services.bot_gateway.app.api_client import ApiClient, ApiError
from services.bot_gateway.app.calendar_commands import handle_calendar_command
from services.bot_gateway.app.kb_intent import KbIntent, detect_kb_intent
from services.bot_gateway.app.kb_session import OperatorKbSessionRepository
from services.bot_gateway.app.material_command_dispatch import (
    handle_material_command,
)
from services.bot_gateway.app.media_group_buffer import (
    MediaGroupBuffer,
)
from services.bot_gateway.app.operator_files import (
    OperatorFileRecord,
    OperatorFileRepository,
)
from services.bot_gateway.app.operator_service_nl import (
    handle_operator_service_nl_message,
)
from services.bot_gateway.app.persistence import persist_normalized_message
from services.bot_gateway.app.prompt_commands import (
    dispatch_pending_prompt_edit,
    handle_prompt_command,
)
from services.bot_gateway.app.sales_command_dispatch import handle_sales_command
from services.bot_gateway.app.services_nl_dialog import handle_services_nl_message
from services.bot_gateway.app.telegram_file_download import (
    TelegramFileDownloader,
    TelegramFileDownloadError,
)
from services.bot_gateway.app.telegram_file_send import (
    TelegramFileSender,
    TelegramFileSendError,
)
from services.bot_gateway.app.telegram_update import (
    NormalizedTelegramMessage,
    TelegramAttachment,
    TelegramUpdateValidationError,
    normalize_update,
)

app = create_service_app("bot_gateway")
logger = logging.getLogger(__name__)
settings = get_settings()
hitl_ticket_repository = HitlTicketRepository(settings.hitl_ticket_db_path)
kb_session_repository = OperatorKbSessionRepository(settings.hitl_ticket_db_path)
operator_file_repository = OperatorFileRepository(settings.operator_files_db_path)
media_group_buffer = MediaGroupBuffer(settings.hitl_ticket_db_path)
api_client = ApiClient(
    base_url=settings.api_internal_base_url,
    internal_token=settings.admin_internal_token,
)
telegram_bot_sender = TelegramBotSender(
    bot_token=settings.telegram_bot_token,
    base_url=settings.telegram_bot_api_base_url,
)
telegram_file_sender = TelegramFileSender(
    bot_token=settings.telegram_bot_token,
    base_url=settings.telegram_bot_api_base_url,
)
# Story 12.02b — OpenRouter client used by the operator services NL classifier.
# Single shared instance: each ``complete_json`` call opens its own httpx
# AsyncClient with a 30s timeout, so a long-running LLM call cannot block
# concurrent webhook handlers.
operator_service_nl_openrouter = OpenRouterClient()

_BOT_TOKEN_RE = re.compile(r"bot\d+:[A-Za-z0-9_-]+")


def _redact_token(message: str) -> str:
    """Strip any bot token from a string so it never reaches a log line or DM."""
    return _BOT_TOKEN_RE.sub("bot<REDACTED>", message)

_TICKET_REF = re.compile(r"HITL\s+ticket\s+#(\d+)", re.IGNORECASE)

# Persona dialog: when the operator types `/persona` with no arguments we send
# the prompt below; their reply becomes the new persona. The marker prefix is
# what `_handle_persona_command` matches on `reply_to_text` to know it's our
# own prompt and not just any operator message.
_PERSONA_MARKER = "📝 Как нас будут звать?"
_PERSONA_PROMPT = (
    "📝 Как нас будут звать? Ответьте на это сообщение в формате «Имя» "
    "или «Имя Фамилия»."
)
_PERSONA_TRIGGER_RE = re.compile(
    r"^/persona(\b|$)"
    r"|^переименуй\b"
    r"|^переименовать\b"
    r"|^смени(те)?\s+имя\b"
    r"|^поменяй(те)?\s+имя\b"
    r"|^новое\s+имя\b",
    re.IGNORECASE,
)
# After a natural trigger, an optional Russian preposition («на» / «в») may
# precede the actual name — strip it before tokenising.
_PERSONA_PREPOSITION_RE = re.compile(r"^\s*(?:на|в)\s+", re.IGNORECASE)

_HELP_TRIGGER_RE = re.compile(r"^\s*/help\b", re.IGNORECASE)
_WHOAMI_TRIGGER_RE = re.compile(r"^\s*/whoami\b", re.IGNORECASE)

_HELP_TEXT = (
    "🤖 Команды оператора:\n"
    "\n"
    "📚 База знаний\n"
    "• /kb_add — добавить вложение или текст в базу знаний "
    "(прикрепите файл или ответьте текстом).\n"
    "• /kb_add confidential — то же, но пометить как конфиденциально "
    "(не цитируется клиентам).\n"
    "• Свободный текст: «добавь в базу …», «сохрани в kb …», "
    "«запомни это для базы знаний …» — то же, что /kb_add.\n"
    "• Multi-message: после фразы вроде «хочу добавить материалы в базу знаний» "
    "можно дослать PDF/DOCX/TXT/PPTX отдельными сообщениями — "
    "следующие 10 минут они будут попадать в KB автоматически.\n"
    "• /kb_cancel — закрыть открытую KB-сессию досрочно.\n"
    "\n"
    "📂 Библиотека файлов\n"
    "• /files [N] — последние сохранённые загрузки (по умолчанию 10).\n"
    "• /send #<id> @username  — переслать сохранённый файл клиенту.\n"
    "• /send #<id> <chat_id>  — то же, но по числовому chat_id (в т.ч. отрицательному).\n"
    "  Работает с файлами любого размера — даже теми, что больше 20 МБ "
    "и не помещаются в KB.\n"
    "• /file_delete #<id> — удалить файл из базы знаний "
    "(вторым сообщением `/file_delete #<id> confirm` подтвердите).\n"
    "  Каскадно удаляются и RAG-чанки, и кандидат знаний, и бинарь на диске.\n"
    "  Оператор удаляет только свои файлы; админ может удалить любой.\n"
    "• /files_delete_all — удалить все ваши файлы "
    "(подтвердите `/files_delete_all confirm`).\n"
    "\n"
    "👤 Имя бота\n"
    "• /persona Имя — переименовать бота (фамилия не обязательна).\n"
    "• /persona Имя Фамилия — переименовать бота с фамилией.\n"
    "• «смени имя на Анна» / «переименуй в Анна» — то же одной фразой.\n"
    "• /persona без аргументов — задать имя через диалог.\n"
    "\n"
    "⚙️ Маршрутизация HITL (админ)\n"
    "• /hitl_config @username chat_id — назначить оператора "
    "и чат для алертов.\n"
    "\n"
    "📅 Календарь\n"
    "• /connect_calendar — подключить свой календарь "
    "(бот пришлёт ссылку для входа Google).\n"
    "• /disconnect_calendar — отключить календарь и удалить сохранённый доступ.\n"
    "\n"
    "💬 Ответ клиенту\n"
    "• Просто ответьте на сообщение бота, в котором указан "
    "«HITL ticket #N» — реплика уйдёт клиенту и закроет тикет. "
    "Если у вас один открытый тикет, ответ можно отправить и без цитирования.\n"
    "\n"
    "🩺 Диагностика\n"
    "• /whoami — показать @username отправителя, назначенного оператора и chat_id.\n"
    "\n"
    "ℹ️ /help — показать эту справку."
)


def _effective_operator_username() -> str:
    return (
        hitl_ticket_repository.get_runtime_config("hitl_primary_operator_username")
        or settings.hitl_primary_operator_username
    )


def _handle_admin_hitl_command(*, username: str | None, text: str) -> dict[str, str] | None:
    if not text.startswith("/hitl_config"):
        return None

    if username != settings.hitl_config_admin_username:
        return {"status": "ignored", "reason": "unauthorized_hitl_config"}

    parts = text.split()
    if len(parts) != 3:
        return {"status": "ignored", "reason": "invalid_hitl_config_format"}

    _, operator_username, chat_id = parts
    if not operator_username.startswith("@"):
        return {"status": "ignored", "reason": "invalid_operator_username"}
    if not chat_id.isdigit():
        return {"status": "ignored", "reason": "invalid_chat_id"}

    hitl_ticket_repository.set_runtime_config(
        key="hitl_primary_operator_username",
        value=operator_username,
        updated_by=username,
    )
    hitl_ticket_repository.set_runtime_config(
        key="hitl_primary_operator_chat_id",
        value=chat_id,
        updated_by=username,
    )
    hitl_ticket_repository.set_runtime_config(
        key="telegram_alert_chat_id",
        value=chat_id,
        updated_by=username,
    )
    hitl_ticket_repository.set_runtime_config(
        key="hitl_primary_operator_chat_id",
        value=chat_id,
        updated_by=username,
    )
    return {
        "status": "configured",
        "hitl_primary_operator_username": operator_username,
        "telegram_alert_chat_id": chat_id,
        "hitl_primary_operator_chat_id": chat_id,
    }


async def _safe_send_text(*, chat_id: int, text: str, purpose: str = "unspecified") -> None:
    """Send a Telegram reply to the operator; swallow missing-token errors.

    The bot_gateway needs to talk back to operators for the persona dialog,
    but unit tests run without a real bot token. We log + drop instead of
    failing the webhook (Telegram would retry it).

    `purpose` tags the call site (e.g. "persona_partial_prompt") so a failed
    outbound surfaces in logs as exactly which user-visible message was lost,
    not just "something failed somewhere". The original silent-failure bug was
    invisible because every persona-related send went through this one helper
    with no caller distinction.
    """
    try:
        await telegram_bot_sender.send_message(chat_id=chat_id, text=text)
    except Exception as exc:  # broad: best-effort outbound message
        logger.warning(
            "bot_gateway_outbound_failed",
            extra={"chat_id": chat_id, "purpose": purpose, "error": str(exc)},
            exc_info=True,
        )


async def _apply_persona(
    *, chat_id: int, username: str, first_name: str, last_name: str
) -> dict[str, str]:
    try:
        result = await api_client.set_persona(
            first_name=first_name,
            last_name=last_name,
            updated_by=username,
        )
    except Exception as exc:
        logger.warning(
            "persona_update_failed",
            extra={"username": username, "error": str(exc)},
        )
        await _safe_send_text(
            chat_id=chat_id,
            text="Не получилось обновить имя — попробуйте чуть позже.",
            purpose="persona_apply_fail_notice",
        )
        return {"status": "persona_update_failed"}
    applied_first = str(result.get("first_name", first_name))
    applied_last = str(result.get("last_name", last_name))
    full_name = f"{applied_first} {applied_last}".strip()
    await _safe_send_text(
        chat_id=chat_id,
        text=f"Готово, теперь меня зовут {full_name}.",
        purpose="persona_apply_ok_confirmation",
    )
    logger.info(
        "persona_updated",
        extra={
            "username": username,
            "first_name": applied_first,
            "last_name": applied_last,
        },
    )
    return {
        "status": "persona_updated",
        "first_name": applied_first,
        "last_name": applied_last,
    }


async def _handle_persona_command(
    *, normalized: NormalizedTelegramMessage
) -> dict[str, str] | None:
    username = normalized.username
    text = normalized.text
    reply_to = normalized.reply_to_text
    is_persona_reply = reply_to is not None and reply_to.startswith(_PERSONA_MARKER)
    trigger_match = _PERSONA_TRIGGER_RE.match(text)

    if not is_persona_reply and trigger_match is None:
        return None

    expected_operator = _effective_operator_username()
    if username != expected_operator:
        # Visible self-diagnosing reply: the original silent "ignored" response
        # was the source of the "ничего не происходит" bug — the operator had
        # no way to tell whether the bot disagreed about who they were.
        await _safe_send_text(
            chat_id=normalized.chat_id,
            text=(
                "⚠️ Сменить имя бота может только назначенный оператор. "
                f"Сейчас оператор — {expected_operator}. "
                f"Ваш аккаунт — {username or '(без username)'}. "
                "Попросите администратора назначить вас через /hitl_config."
            ),
            purpose="persona_unauthorized_notice",
        )
        logger.warning(
            "persona_unauthorized",
            extra={"username": username, "expected_operator": expected_operator},
        )
        return {"status": "ignored", "reason": "unauthorized_persona"}

    if is_persona_reply:
        # Reply to the full prompt: take up to 2 tokens. Single-token replies
        # apply with an empty surname (the surname is optional). 0 tokens is
        # already filtered upstream as `attachment_only`.
        parts = text.split()
        first_name = parts[0]
        last_name = parts[1] if len(parts) >= 2 else ""
        logger.info(
            "persona_full_reply_accepted",
            extra={
                "username": username,
                "first_name": first_name,
                "last_name": last_name,
                "token_count": len(parts),
            },
        )
        return await _apply_persona(
            chat_id=normalized.chat_id,
            username=username,
            first_name=first_name,
            last_name=last_name,
        )

    # Slash command takes its name tokens directly (no preposition stripping).
    if text.lower().startswith("/persona"):
        parts = text.split()
        if len(parts) >= 2:
            first_name = parts[1]
            last_name = parts[2] if len(parts) >= 3 else ""
            logger.info(
                "persona_slash_oneshot",
                extra={
                    "username": username,
                    "first_name": first_name,
                    "last_name": last_name,
                    "token_count": len(parts) - 1,
                },
            )
            return await _apply_persona(
                chat_id=normalized.chat_id,
                username=username,
                first_name=first_name,
                last_name=last_name,
            )
        await _safe_send_text(
            chat_id=normalized.chat_id,
            text=_PERSONA_PROMPT,
            purpose="persona_slash_full_prompt",
        )
        logger.info("persona_slash_full_prompt_sent", extra={"username": username})
        return {"status": "persona_prompt_sent"}

    # Natural-language trigger ("смени имя …", "переименуй …", "новое имя …").
    # Extract the tail after the matched trigger; strip an optional Russian
    # preposition («на» / «в»); take up to two tokens as first/last name.
    assert trigger_match is not None  # narrowed by the early-return above
    matched_trigger = trigger_match.group(0).strip().lower()
    tail = text[trigger_match.end():]
    tail = _PERSONA_PREPOSITION_RE.sub("", tail, count=1)
    name_tokens = tail.split()
    if name_tokens:
        first_name = name_tokens[0]
        last_name = name_tokens[1] if len(name_tokens) >= 2 else ""
        logger.info(
            "persona_natural_oneshot",
            extra={
                "username": username,
                "first_name": first_name,
                "last_name": last_name,
                "trigger": matched_trigger,
                "token_count": len(name_tokens),
            },
        )
        return await _apply_persona(
            chat_id=normalized.chat_id,
            username=username,
            first_name=first_name,
            last_name=last_name,
        )

    await _safe_send_text(
        chat_id=normalized.chat_id,
        text=_PERSONA_PROMPT,
        purpose="persona_natural_full_prompt",
    )
    logger.info(
        "persona_natural_full_prompt_sent",
        extra={"username": username, "trigger": matched_trigger},
    )
    return {"status": "persona_prompt_sent"}


async def _handle_help_command(
    *, normalized: NormalizedTelegramMessage
) -> dict[str, str] | None:
    if not _HELP_TRIGGER_RE.match(normalized.text or ""):
        return None
    operator_username = _effective_operator_username()
    if not normalized.username or normalized.username != operator_username:
        return None
    await _send_dm(normalized.chat_id, _HELP_TEXT)
    return {"status": "help_sent"}


async def _handle_whoami_command(
    *, normalized: NormalizedTelegramMessage
) -> dict[str, str] | None:
    """Diagnostic: tell any sender what @username the bot sees and which
    operator it's configured to recognise. Intentionally open to all senders
    so that someone hitting a silent "unauthorized" can self-diagnose without
    server logs."""
    if not _WHOAMI_TRIGGER_RE.match(normalized.text or ""):
        return None
    expected = _effective_operator_username()
    sender = normalized.username or "(без username)"
    match_line = "✅ совпадает" if normalized.username == expected else "❌ не совпадает"
    text = (
        f"🪪 username: {sender}\n"
        f"🛡️ оператор: {expected}\n"
        f"📨 chat_id: {normalized.chat_id}\n"
        f"{match_line}"
    )
    await _safe_send_text(
        chat_id=normalized.chat_id, text=text, purpose="whoami_diagnostic"
    )
    logger.info(
        "whoami_sent",
        extra={
            "username": normalized.username,
            "expected_operator": expected,
            "chat_id": normalized.chat_id,
            "match": normalized.username == expected,
        },
    )
    return {"status": "whoami_sent"}


def _extract_ticket_id(reply_to_text: str | None) -> int | None:
    if not reply_to_text:
        return None
    match = _TICKET_REF.search(reply_to_text)
    if not match:
        return None
    try:
        return int(match.group(1))
    except ValueError:  # pragma: no cover - regex already constrains to digits
        return None


def _fallback_open_ticket_for_operator(operator_username: str) -> int | None:
    """Return the single open-assigned ticket id for this operator, if any."""
    open_tickets = hitl_ticket_repository.list_active_for_operator(operator_username)
    if len(open_tickets) == 1:
        return open_tickets[0].id
    return None


def _format_disambiguation_dm(open_tickets: list) -> str:
    """Render an operator-facing prompt listing active tickets to reply to.

    Called when the operator sends a free-form message without a quoted
    HITL ticket reference and there are 2+ assigned tickets — without this
    prompt the message would be silently dropped, hiding the operator's
    reply from the customer.
    """
    lines = ["Не понял, к какому тикету относится ответ. Активные тикеты:"]
    for ticket in open_tickets:
        snippet = ticket.conversation_ref or ""
        if len(snippet) > 60:
            snippet = snippet[:60].rstrip() + "…"
        lines.append(f"  • HITL ticket #{ticket.id}: «{snippet}»")
    lines.append(
        "Ответьте через Reply на сообщение бота с нужным «HITL ticket #N»."
    )
    return "\n".join(lines)


_KB_ATTACHMENT_TYPE_MAP: dict[str, str] = {
    "document": "_from_mime_or_name",
    "photo": "image",
    "audio": "audio",
    "voice": "audio",
    "video": "video",
}


def _kb_source_file_type(attachment: TelegramAttachment) -> str | None:
    """Map a Telegram attachment to a `source_file_type` accepted by the API."""
    explicit = _KB_ATTACHMENT_TYPE_MAP.get(attachment.kind)
    if explicit and explicit != "_from_mime_or_name":
        return explicit
    name = (attachment.file_name or "").lower()
    if name.endswith(".pdf"):
        return "pdf"
    if name.endswith(".docx"):
        return "docx"
    if name.endswith(".pptx"):
        return "pptx"
    if name.endswith(".xlsx"):
        return "xlsx"
    if name.endswith(".csv"):
        return "csv"
    if name.endswith(".html") or name.endswith(".htm"):
        return "html"
    if name.endswith(".md") or name.endswith(".markdown"):
        return "md"
    if name.endswith(".rtf"):
        return "rtf"
    if name.endswith(".epub"):
        return "epub"
    if name.endswith(".zip"):
        return "zip"
    if name.endswith(".txt"):
        return "txt"
    mime = (attachment.mime_type or "").lower()
    if mime == "application/pdf":
        return "pdf"
    if mime in {
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        "application/msword",
    }:
        return "docx"
    if mime in {
        "application/vnd.openxmlformats-officedocument.presentationml.presentation",
        "application/vnd.ms-powerpoint",
    }:
        return "pptx"
    if mime in {
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        "application/vnd.ms-excel",
    }:
        return "xlsx"
    if mime == "text/csv":
        return "csv"
    if mime == "text/html":
        return "html"
    if mime == "text/markdown":
        return "md"
    if mime in {"application/rtf", "text/rtf"}:
        return "rtf"
    if mime == "application/epub+zip":
        return "epub"
    if mime in {"application/zip", "application/x-zip-compressed"}:
        return "zip"
    if mime.startswith("text/"):
        return "txt"
    if mime.startswith("image/"):
        return "image"
    if mime.startswith("audio/"):
        return "audio"
    if mime.startswith("video/"):
        return "video"
    return None


def _kb_extension_for(attachment: TelegramAttachment, source_file_type: str) -> str:
    if attachment.file_name and "." in attachment.file_name:
        return attachment.file_name.rsplit(".", 1)[-1]
    fallback = {
        "pdf": "pdf",
        "docx": "docx",
        "pptx": "pptx",
        "txt": "txt",
        "image": "jpg",
        "audio": "ogg",
        "video": "mp4",
        "xlsx": "xlsx",
        "csv": "csv",
        "html": "html",
        "md": "md",
        "rtf": "rtf",
        "epub": "epub",
        "zip": "zip",
    }
    return fallback.get(source_file_type, "bin")


async def _send_dm(chat_id: int, text: str) -> None:
    base_url = settings.telegram_bot_api_base_url.rstrip("/")
    url = f"{base_url}/bot{settings.telegram_bot_token}/sendMessage"
    async with httpx.AsyncClient(timeout=15) as client:
        try:
            await client.post(url, json={"chat_id": chat_id, "text": text})
        except Exception as exc:  # best-effort DM; do not block on Telegram errors
            logger.warning("operator_dm_failed", extra={"chat_id": chat_id, "error": str(exc)})


def _kb_attachment_count_word(count: int) -> str:
    if count == 1:
        return "файл"
    if 2 <= count <= 4:
        return "файла"
    return "файлов"


def _material_downloader_factory(storage_dir: Path) -> TelegramFileDownloader:
    """Construct a :class:`TelegramFileDownloader` rooted at the per-project
    sales materials subdir; the ``/material`` handler passes
    ``<storage_root>/<project_id>/`` so files land under
    ``.data/sales_materials/<project_id>/<file>`` (spec exit criterion).
    Reuses the same Telegram fetch path as ``/kb_add``.
    """
    return TelegramFileDownloader(
        bot_token=settings.telegram_bot_token,
        storage_dir=storage_dir,
        max_bytes=settings.operator_upload_max_bytes,
        base_url=settings.telegram_bot_api_base_url,
        local_mode=settings.telegram_bot_api_local_mode,
    )


async def _process_operator_upload(
    *,
    normalized: NormalizedTelegramMessage,
    intent: KbIntent,
) -> None:
    downloader = TelegramFileDownloader(
        bot_token=settings.telegram_bot_token,
        storage_dir=settings.operator_upload_storage_dir,
        max_bytes=settings.operator_upload_max_bytes,
        base_url=settings.telegram_bot_api_base_url,
        local_mode=settings.telegram_bot_api_local_mode,
    )
    successes: list[dict] = []
    successes_meta: list[tuple[str | None, str | None]] = []
    failures: list[tuple[str, str, str | None]] = []

    if not normalized.attachments:
        inline_body = (intent.cleaned_text or "").strip()
        try:
            result = await api_client.submit_operator_upload(
                operator_username=normalized.username or "",
                source_file_type="inline_text",
                source_file_name=None,
                stored_binary_path=None,
                is_confidential=intent.confidential,
                inline_text=inline_body,
                timeout_seconds=settings.operator_upload_api_timeout_seconds,
            )
            successes.append(result)
            successes_meta.append((None, None))
        except ApiError as exc:
            reason = _redact_token(exc.detail or str(exc))
            failures.append(("inline_text", reason, None))
        except Exception as exc:
            failures.append(("inline_text", _redact_token(str(exc)), None))
    else:
        for attachment in normalized.attachments:
            label = attachment.file_name or attachment.file_id
            source_file_type = _kb_source_file_type(attachment)
            if source_file_type is None:
                record = operator_file_repository.record_upload(
                    chat_id=normalized.chat_id,
                    username=normalized.username or "",
                    source_message_id=normalized.source_message_id,
                    attachment=attachment,
                    is_confidential=intent.confidential,
                    stored_binary_path=None,
                    download_status="failed:unsupported_attachment_type",
                    source_file_type=None,
                    kb_ingest_status="skipped",
                )
                failures.append(
                    (label, "unsupported_attachment_type", record.short_id)
                )
                continue
            if (
                attachment.file_size is not None
                and attachment.file_size > settings.operator_upload_max_bytes
            ):
                record = operator_file_repository.record_upload(
                    chat_id=normalized.chat_id,
                    username=normalized.username or "",
                    source_message_id=normalized.source_message_id,
                    attachment=attachment,
                    is_confidential=intent.confidential,
                    stored_binary_path=None,
                    download_status="too_large",
                    source_file_type=source_file_type,
                    kb_ingest_status="skipped",
                )
                failures.append((label, "file_too_large", record.short_id))
                continue
            extension = _kb_extension_for(attachment, source_file_type)
            try:
                downloaded = await downloader.download(
                    file_id=attachment.file_id,
                    suggested_extension=extension,
                    mime_type=attachment.mime_type,
                )
            except TelegramFileDownloadError as exc:
                record = operator_file_repository.record_upload(
                    chat_id=normalized.chat_id,
                    username=normalized.username or "",
                    source_message_id=normalized.source_message_id,
                    attachment=attachment,
                    is_confidential=intent.confidential,
                    stored_binary_path=None,
                    download_status=(
                        "too_large"
                        if exc.reason == "file_too_large"
                        else f"failed:{exc.reason}"
                    ),
                    source_file_type=source_file_type,
                    kb_ingest_status="skipped",
                )
                reason = (
                    f"telegram_get_file_failed:{exc.description}"
                    if exc.reason == "telegram_get_file_failed" and exc.description
                    else exc.reason
                )
                failures.append((label, reason, record.short_id))
                continue
            except Exception as exc:
                logger.warning(
                    "operator_upload_download_unexpected_error",
                    extra={"error": _redact_token(str(exc))},
                )
                record = operator_file_repository.record_upload(
                    chat_id=normalized.chat_id,
                    username=normalized.username or "",
                    source_message_id=normalized.source_message_id,
                    attachment=attachment,
                    is_confidential=intent.confidential,
                    stored_binary_path=None,
                    download_status="failed:unexpected",
                    source_file_type=source_file_type,
                    kb_ingest_status="skipped",
                )
                failures.append((label, "download_failed", record.short_id))
                continue
            record = operator_file_repository.record_upload(
                chat_id=normalized.chat_id,
                username=normalized.username or "",
                source_message_id=normalized.source_message_id,
                attachment=attachment,
                is_confidential=intent.confidential,
                stored_binary_path=str(downloaded.path),
                download_status="ok",
                source_file_type=source_file_type,
                kb_ingest_status="pending",
            )
            try:
                result = await api_client.submit_operator_upload(
                    operator_username=normalized.username or "",
                    source_file_type=source_file_type,
                    source_file_name=attachment.file_name,
                    stored_binary_path=str(downloaded.path),
                    is_confidential=intent.confidential,
                    operator_short_id=record.short_id,
                    timeout_seconds=settings.operator_upload_api_timeout_seconds,
                )
                successes.append(result)
                successes_meta.append((record.short_id, attachment.file_name))
                operator_file_repository.update_kb_status(
                    short_id=record.short_id,
                    kb_ingest_status="ok",
                    kb_inserted_chunks=int(result.get("inserted_chunks", 0) or 0),
                )
                candidate_id = result.get("candidate_id")
                if candidate_id is not None:
                    operator_file_repository.set_candidate_id(
                        short_id=record.short_id,
                        knowledge_candidate_id=int(candidate_id),
                    )
            except ApiError as exc:
                redacted = _redact_token(exc.detail or str(exc))
                operator_file_repository.update_kb_status(
                    short_id=record.short_id,
                    kb_ingest_status=f"failed:{redacted}",
                    kb_inserted_chunks=None,
                )
                failures.append(
                    (label, f"api_failed:{redacted}", record.short_id)
                )
            except Exception as exc:
                redacted = _redact_token(str(exc))
                operator_file_repository.update_kb_status(
                    short_id=record.short_id,
                    kb_ingest_status=f"failed:{redacted}",
                    kb_inserted_chunks=None,
                )
                failures.append(
                    (label, f"api_failed:{redacted}", record.short_id)
                )

    total_chunks = sum(int(item.get("inserted_chunks", 0) or 0) for item in successes)
    confidential_count = sum(
        1 for item in successes if item.get("is_confidential")
    )
    deduped = sum(1 for item in successes if item.get("deduplicated"))
    summary_lines = [
        f"✅ Добавлено в базу: {len(successes)} {_kb_attachment_count_word(len(successes))}, "
        f"{total_chunks} чанков, {confidential_count} помечен(о) confidential."
    ]
    for short_id, name in successes_meta:
        if short_id and name:
            summary_lines.append(f"   • #{short_id} · {name}")
    if deduped:
        summary_lines.append(f"♻️ Из них уже было в базе: {deduped}.")
    material_lines = await _analyze_kb_uploads_for_materials(
        successes=successes,
        successes_meta=successes_meta,
    )
    summary_lines.extend(material_lines)
    for label, reason, short_id in failures:
        friendly = _friendly_failure_reason(
            reason, max_bytes=settings.operator_upload_max_bytes
        )
        suffix = f" (id: #{short_id})" if short_id else ""
        summary_lines.append(
            f"⚠️ Не удалось обработать {label}: {friendly}{suffix}"
        )
    await _send_dm(normalized.chat_id, "\n".join(summary_lines))


async def _analyze_kb_uploads_for_materials(
    *,
    successes: list[dict],
    successes_meta: list[tuple[str | None, str | None]],
) -> list[str]:
    """Stories 12.05b + 12.05c hook: for each non-confidential KB upload,
    fan out the materials analyzer + services extractor in parallel and
    append the resulting lines to the KB-upload ack.

    The fan-out is per-upload: each upload runs both hooks via
    ``asyncio.gather`` so a single KB upload triggers at most two LLM
    calls. Each hook is independent — an exception in one MUST NOT
    block the other (each fan-out branch is wrapped in its own
    try/except). Failures are silent to the operator and structured
    logs carry only the short_id + project_id + error (never the file
    text).

    Per the story rules, output order is: materials line FIRST, then
    services line. When both are empty the ack stays bare.
    """
    lines: list[str] = []
    token = settings.internal_service_token or ""
    if not token:
        return lines
    for item, (short_id, _name) in zip(successes, successes_meta):
        if short_id is None:
            continue
        if item.get("is_confidential"):
            continue
        project_id = item.get("project_id")
        if project_id is None:
            continue
        materials_outcome, services_outcome = await asyncio.gather(
            _safe_analyze_kb_material(
                short_id=short_id,
                project_id=int(project_id),
                token=token,
            ),
            _safe_extract_kb_services(
                short_id=short_id,
                project_id=int(project_id),
                token=token,
            ),
        )
        if materials_outcome is not None and materials_outcome.get("registered"):
            material_id = materials_outcome.get("material_id")
            if material_id is not None:
                lines.append(
                    f"📎 Добавлен в материалы для клиентов "
                    f"(id={material_id})."
                )
        if services_outcome is not None:
            added = services_outcome.get("added") or []
            names = [
                str(entry.get("name"))
                for entry in added
                if isinstance(entry, dict)
                and isinstance(entry.get("name"), str)
                and entry.get("name").strip()
            ]
            if names:
                lines.append(
                    "📦 Услуги добавлены: " + ", ".join(names) + "."
                )
    return lines


async def _safe_analyze_kb_material(
    *, short_id: str, project_id: int, token: str
) -> dict | None:
    """Run the 12.05b materials analyzer; swallow + log any error.

    Returns ``None`` when the analyzer call raised — the caller treats
    that as "no line to append" and keeps the bare KB ack visible.
    """
    try:
        return await api_client.analyze_kb_material(
            project_id=project_id,
            operator_file_short_id=short_id,
            internal_token=token,
        )
    except Exception as exc:
        logger.warning(
            "sales_kb_material_analyze_failed",
            extra={
                "operator_file_short_id": short_id,
                "project_id": project_id,
                "error": _redact_token(str(exc)),
            },
        )
        return None


async def _safe_extract_kb_services(
    *, short_id: str, project_id: int, token: str
) -> dict | None:
    """Run the 12.05c services extractor; swallow + log any error.

    Returns ``None`` when the extract call raised — the caller treats
    that as "no line to append" and keeps the materials line + bare KB
    ack visible.
    """
    try:
        return await api_client.extract_kb_services(
            project_id=project_id,
            operator_file_short_id=short_id,
            internal_token=token,
        )
    except Exception as exc:
        logger.warning(
            "sales_services_extract_failed",
            extra={
                "operator_file_short_id": short_id,
                "project_id": project_id,
                "error": _redact_token(str(exc)),
            },
        )
        return None


_API_DETAIL_FRIENDLY: dict[str, str] = {
    "empty_text": (
        "из файла не удалось извлечь текст — возможно, это скан или "
        "слайды без текстового слоя. Попробуйте экспортировать PDF "
        "с текстовым слоем или прислать оригинал в DOCX/PPTX."
    ),
    "unsupported_source_file_type": "тип файла не поддерживается на стороне API",
    "missing_stored_binary_path": "внутренняя ошибка: не передан путь к файлу",
    "binary_not_found": "файл не найден на диске API",
    "empty_inline_text": "пустой текст — нечего сохранять",
    "zip_corrupt": "ZIP-архив повреждён",
    "zip_too_many_members": "слишком много файлов в ZIP-архиве",
    "zip_too_large": "ZIP-архив слишком большой",
    "nested_zip_not_supported": "вложенные ZIP-архивы не поддерживаются",
    "pdf_too_many_pages_for_ocr": (
        "PDF слишком длинный для распознавания — попробуйте уменьшить "
        "количество страниц или приложить текстовый оригинал."
    ),
    "audio_too_long": "запись слишком длинная — сократите файл и повторите.",
    "ffprobe_no_duration": "не удалось определить длительность медиафайла.",
    "operator_upload_failed": "внутренняя ошибка извлечения текста на стороне API.",
}


def _friendly_failure_reason(reason: str, *, max_bytes: int) -> str:
    """Translate an internal failure reason to operator-facing Russian.

    Falls back to the raw reason (token-redacted) for unknown categories so
    diagnostic information is preserved without leaking the bot token.
    """
    if reason == "file_too_large":
        mb = max_bytes // (1024 * 1024)
        return (
            f"файл больше {mb} МБ — Telegram Bot API не позволяет ботам "
            f"скачивать такие файлы. Файл сохранён в библиотеке: можно "
            f"переслать через /send."
        )
    if reason == "unsupported_attachment_type":
        return "тип файла не поддерживается"
    if reason == "telegram_network_error":
        return "не удалось связаться с Telegram, попробуйте ещё раз"
    if reason == "telegram_cdn_error":
        return "Telegram CDN недоступен, попробуйте ещё раз"
    if reason.startswith("telegram_get_file_failed"):
        head, _, description = reason.partition(":")
        if description:
            return f"Telegram отклонил getFile: {description}"
        return "Telegram отклонил getFile"
    if reason.startswith("api_failed:"):
        detail = reason[len("api_failed:") :]
        friendly = _API_DETAIL_FRIENDLY.get(detail)
        if friendly is not None:
            return f"API: {friendly}"
        return reason
    if reason == "download_failed":
        return "не удалось скачать файл"
    friendly = _API_DETAIL_FRIENDLY.get(reason)
    if friendly is not None:
        return f"API: {friendly}"
    return _redact_token(reason)


_KB_CANCEL_RE = re.compile(r"^\s*/kb_cancel\b", re.IGNORECASE)

_KB_SESSION_WAIT_ACK = (
    "Принял. Жду файлы — пришлите PDF/DOCX/TXT/PPTX следующими сообщениями "
    "(в течение 10 минут). Отмена: /kb_cancel."
)


async def _handle_kb_cancel(
    normalized: NormalizedTelegramMessage,
) -> dict[str, str] | None:
    # Caller (_handle_kb_command) already gates on operator username.
    if not _KB_CANCEL_RE.match(normalized.text or ""):
        return None
    kb_session_repository.clear(
        chat_id=normalized.chat_id,
        username=normalized.username or "",
    )
    await _send_dm(normalized.chat_id, "Сессия закрыта.")
    return {"status": "kb_session_cleared"}


async def _handle_kb_command(
    normalized: NormalizedTelegramMessage,
    background_tasks: BackgroundTasks,
) -> dict[str, str] | None:
    operator_username = _effective_operator_username()
    if not normalized.username or normalized.username != operator_username:
        return None

    cancel_result = await _handle_kb_cancel(normalized)
    if cancel_result is not None:
        return cancel_result

    intent = detect_kb_intent(
        text=normalized.text,
        caption=normalized.caption,
        normalizer=get_russian_normalizer(),
    )
    if intent is None:
        return None

    n_attachments = len(normalized.attachments)
    inline_body = (intent.cleaned_text or "").strip()

    # The lemma-fallback branch returns the candidate as-is (no trigger
    # stripped), so its `cleaned_text` is the operator's META-request like
    # "хочу добавить материалы в knowledge base" — NOT real knowledge.
    # Treat it as a session-open signal, not as content to ingest. Only
    # the literal-match path produces a `cleaned_text` we trust to contain
    # real KB content (the trigger has been stripped out of the middle).
    inline_is_real_content = (
        intent.match_kind == "literal" and bool(inline_body)
    )

    kb_session_repository.upsert(
        chat_id=normalized.chat_id,
        username=normalized.username,
        is_confidential=intent.confidential,
        ttl_seconds=settings.operator_kb_session_ttl_seconds,
    )

    if n_attachments == 0 and not inline_is_real_content:
        await _send_dm(normalized.chat_id, _KB_SESSION_WAIT_ACK)
        return {
            "status": "accepted",
            "kb_mode": "session_opened",
            "attachment_count": "0",
        }

    if n_attachments > 0 and normalized.media_group_id is not None:
        _buffer_attachments_for_media_group(
            normalized=normalized,
            is_confidential=intent.confidential,
            background_tasks=background_tasks,
        )
        return {
            "status": "accepted",
            "kb_mode": "media_group_buffered",
            "attachment_count": str(n_attachments),
            "media_group_id": normalized.media_group_id,
        }

    if n_attachments == 0:
        ack = "Принял текст, добавляю в базу…"
    else:
        ack = (
            f"Принял {n_attachments} {_kb_attachment_count_word(n_attachments)}, обрабатываю…"
        )
    await _send_dm(normalized.chat_id, ack)

    background_tasks.add_task(_process_operator_upload, normalized=normalized, intent=intent)
    return {
        "status": "accepted",
        "kb_mode": intent.mode,
        "attachment_count": str(n_attachments),
    }


async def _handle_kb_session_continuation(
    *,
    normalized: NormalizedTelegramMessage,
    background_tasks: BackgroundTasks,
) -> dict[str, str] | None:
    """Route an attachment-only operator message into the open KB session.

    Returns None when the message should fall through to the standard
    flow (no attachments, non-operator, no active session, or the session
    has expired). When a session is active we synthesize a KbIntent from
    the stored confidential flag, ack the operator, refresh the TTL so
    subsequent files in the same batch keep working, and schedule the
    upload as a background task.
    """
    if not normalized.attachments:
        return None
    if not normalized.username:
        return None
    if normalized.username != _effective_operator_username():
        return None
    session = kb_session_repository.get_active(
        chat_id=normalized.chat_id,
        username=normalized.username,
    )
    if session is None:
        return None

    synthetic_intent = KbIntent(
        confidential=session.is_confidential,
        mode="freetext",
        cleaned_text="",
        match_kind="literal",
    )
    n = len(normalized.attachments)
    kb_session_repository.upsert(
        chat_id=normalized.chat_id,
        username=normalized.username,
        is_confidential=session.is_confidential,
        ttl_seconds=settings.operator_kb_session_ttl_seconds,
    )

    if normalized.media_group_id is not None:
        _buffer_attachments_for_media_group(
            normalized=normalized,
            is_confidential=session.is_confidential,
            background_tasks=background_tasks,
        )
        return {
            "status": "accepted",
            "kb_mode": "media_group_buffered",
            "attachment_count": str(n),
            "media_group_id": normalized.media_group_id,
        }

    await _send_dm(
        normalized.chat_id,
        f"Принял {n} {_kb_attachment_count_word(n)}, обрабатываю…",
    )
    background_tasks.add_task(
        _process_operator_upload,
        normalized=normalized,
        intent=synthetic_intent,
    )
    return {
        "status": "accepted",
        "kb_mode": "session_continuation",
        "attachment_count": str(n),
    }


async def _handle_operator_media_group_orphan(
    *,
    normalized: NormalizedTelegramMessage,
    background_tasks: BackgroundTasks,
) -> dict[str, str] | None:
    """Speculatively buffer a caption-less operator media-group sibling.

    Telegram delivers each file in a media group as a separate webhook,
    and only one of them carries the caption with the KB-intent trigger.
    If the caption-less sibling is processed before the captioned one has
    upserted the kb_session, the standard dispatch would silently drop it
    as "attachment_only" — losing files. We buffer here regardless of
    session/intent state; `_flush_media_group_after_debounce` re-checks
    the session at drain time and either processes or refuses with a hint.
    """
    if not normalized.attachments:
        return None
    if normalized.media_group_id is None:
        return None
    if not normalized.username or normalized.username != _effective_operator_username():
        return None
    _buffer_attachments_for_media_group(
        normalized=normalized,
        is_confidential=False,
        background_tasks=background_tasks,
    )
    return {
        "status": "accepted",
        "kb_mode": "media_group_orphan_buffered",
        "attachment_count": str(len(normalized.attachments)),
        "media_group_id": normalized.media_group_id,
    }


def _buffer_attachments_for_media_group(
    *,
    normalized: NormalizedTelegramMessage,
    is_confidential: bool,
    background_tasks: BackgroundTasks,
) -> None:
    """Buffer every attachment under `media_group_id` and arm the flush.

    Each webhook schedules its own settling-window flusher. Whichever
    flusher's wait completes first wins the `drain()`; the others observe
    an empty buffer and return without side effects (covered by
    `test_kb_media_group_flush_empty_buffer_is_noop`). The
    `INSERT OR IGNORE` + (media_group_id, update_id) PK keeps `add()`
    idempotent under Telegram retries.
    """
    assert normalized.media_group_id is not None
    for attachment in normalized.attachments:
        media_group_buffer.add(
            media_group_id=normalized.media_group_id,
            chat_id=normalized.chat_id,
            username=normalized.username or "",
            update_id=normalized.update_id,
            source_message_id=normalized.source_message_id,
            attachment=attachment,
            is_confidential=is_confidential,
        )
    background_tasks.add_task(
        _flush_media_group_after_debounce,
        media_group_id=normalized.media_group_id,
        debounce_seconds=settings.operator_media_group_debounce_seconds,
    )


async def _flush_media_group_after_debounce(
    *,
    media_group_id: str,
    debounce_seconds: float,
) -> None:
    """Wait until the group has been quiet for `debounce_seconds`, then drain.

    Each new webhook scheduling this coroutine effectively extends the
    wait, because the loop re-reads `latest_received_at()` on every poll.
    A hard cap (`operator_media_group_settling_cap_seconds`) bounds total
    wait so a pathological stream of files cannot delay a flush forever.

    Multiple flushers may run for the same group; only one drains a
    non-empty buffer. The others observe `[]` and return — same idempotency
    contract as before.
    """
    cap = settings.operator_media_group_settling_cap_seconds
    poll = max(settings.operator_media_group_poll_interval_seconds, 0.05)
    started = asyncio.get_event_loop().time()

    try:
        while True:
            latest = media_group_buffer.latest_received_at(
                media_group_id=media_group_id,
            )
            if latest is None:
                # Buffer is empty — either never had data, or another
                # flusher already drained. Nothing to do.
                return
            quiet_for = (datetime.now(UTC) - latest).total_seconds()
            elapsed = asyncio.get_event_loop().time() - started
            if quiet_for >= debounce_seconds or elapsed >= cap:
                if elapsed >= cap and quiet_for < debounce_seconds:
                    logger.warning(
                        "media_group_settling_cap_hit",
                        extra={
                            "media_group_id": media_group_id,
                            "quiet_for_seconds": quiet_for,
                            "elapsed_seconds": elapsed,
                            "cap_seconds": cap,
                        },
                    )
                break
            await asyncio.sleep(poll)

        items = media_group_buffer.drain(media_group_id=media_group_id)
        if not items:
            return
        first = items[0]
        session = kb_session_repository.get_active(
            chat_id=first.chat_id,
            username=first.username,
        )
        if session is None:
            logger.warning(
                "media_group_orphan_dropped",
                extra={
                    "media_group_id": media_group_id,
                    "chat_id": first.chat_id,
                    "username": first.username,
                    "attachment_count": len(items),
                },
            )
            await _send_dm(
                first.chat_id,
                "Получил файлы без активной сессии добавления в базу. "
                "Если хотите загрузить — отправьте сначала фразу "
                "«добавь в базу знаний», затем файлы.",
            )
            return
        attachments = tuple(item.attachment for item in items)
        synthesized = NormalizedTelegramMessage(
            update_id=first.update_id,
            source_message_id=first.source_message_id,
            chat_id=first.chat_id,
            user_id=0,
            username=first.username,
            text="",
            reply_to_text=None,
            caption=None,
            media_group_id=media_group_id,
            attachments=attachments,
        )
        intent = KbIntent(
            confidential=session.is_confidential or any(
                item.is_confidential for item in items
            ),
            mode="freetext",
            cleaned_text="",
            match_kind="literal",
        )
        n = len(attachments)
        logger.info(
            "media_group_flush_draining",
            extra={
                "media_group_id": media_group_id,
                "attachment_count": n,
            },
        )
        await _send_dm(
            first.chat_id,
            f"Принял {n} {_kb_attachment_count_word(n)}, обрабатываю…",
        )
        await _process_operator_upload(normalized=synthesized, intent=intent)
    except Exception as exc:  # broad: a flush failure must never crash the loop
        logger.exception(
            "media_group_flush_failed",
            extra={
                "media_group_id": media_group_id,
                "error": _redact_token(str(exc)),
            },
        )


_FILES_TRIGGER_RE = re.compile(r"^\s*/files(\b|$)", re.IGNORECASE)
_SEND_TRIGGER_RE = re.compile(r"^\s*/send(\b|$)", re.IGNORECASE)
_FILE_INSPECT_TRIGGER_RE = re.compile(r"^\s*/file(\b|$)", re.IGNORECASE)
_FILES_FIND_TRIGGER_RE = re.compile(r"^\s*/files_find(\b|$)", re.IGNORECASE)
_FILE_DELETE_TRIGGER_RE = re.compile(r"^\s*/file_delete\b", re.IGNORECASE)
_FILES_DELETE_ALL_TRIGGER_RE = re.compile(
    r"^\s*/files_delete_all\b", re.IGNORECASE
)
_FILE_INSPECT_HEAD_CHARS = 3072
_FILES_FIND_MIN_QUERY = 2
_FILE_INSPECT_USAGE = "Использование: /file <short_id>"
_FILES_FIND_USAGE = "Использование: /files_find <запрос>"
_FILE_DELETE_USAGE = "Использование: /file_delete <short_id> [confirm]"
_FILES_DELETE_ALL_BULK_LIMIT = 10_000


async def _handle_file_inspect_command(
    *,
    normalized: NormalizedTelegramMessage,
) -> dict[str, str] | None:
    """Dispatch `/file <short_id>` and `/files_find <query>` for operator/admin.

    Returns None for non-matching commands or unauthorised senders so the
    normal routing continues.
    """
    if not normalized.username:
        return None
    is_operator = normalized.username == _effective_operator_username()
    is_admin = normalized.username == settings.hitl_config_admin_username
    if not (is_operator or is_admin):
        return None
    text = normalized.text or ""
    # files_find checked first since both /file and /files_find start with /file
    # (the regex \b boundaries already make them mutually exclusive, but this
    # keeps the dispatch order explicit).
    if _FILES_FIND_TRIGGER_RE.match(text):
        return await _handle_files_find_command(normalized=normalized, text=text)
    if _FILE_INSPECT_TRIGGER_RE.match(text):
        return await _handle_file_inspect_subcommand(
            normalized=normalized, text=text
        )
    return None


async def _handle_file_delete_command(
    *,
    normalized: NormalizedTelegramMessage,
) -> dict[str, str] | None:
    """Dispatch `/file_delete` and `/files_delete_all` for operator/admin.

    Both commands use a stateless two-step confirm: the first message replies
    with a warning and a hint; the second message ending in literal ``confirm``
    performs the destructive call.

    Story 09.07: ``/file_delete`` lets an operator delete an own file or an
    admin delete any file. ``/files_delete_all`` always scopes to the caller's
    own username (admin uses the per-file path to wipe someone else's data).
    """
    if not normalized.username:
        return None
    is_operator = normalized.username == _effective_operator_username()
    is_admin = normalized.username == settings.hitl_config_admin_username
    if not (is_operator or is_admin):
        return None
    text = normalized.text or ""
    # Order matters: /files_delete_all starts with /files_delete which would
    # otherwise be intercepted if we matched /file_delete first via a less
    # specific regex. The current regexes are mutually exclusive (one starts
    # with /file_delete, the other with /files_delete_all), but keep this
    # ordering explicit.
    if _FILES_DELETE_ALL_TRIGGER_RE.match(text):
        return await _handle_files_delete_all_subcommand(
            normalized=normalized, text=text
        )
    if _FILE_DELETE_TRIGGER_RE.match(text):
        return await _handle_file_delete_subcommand(
            normalized=normalized, text=text
        )
    return None


def _has_confirm_token(parts: list[str]) -> bool:
    return bool(parts) and parts[-1].lower() == "confirm"


def _format_delete_summary(summary: dict, *, scope_label: str) -> str:
    files = int(summary.get("deleted_files", 0) or 0)
    chunks = int(summary.get("deleted_chunks", 0) or 0)
    candidates = int(summary.get("deleted_candidates", 0) or 0)
    binaries = int(summary.get("deleted_binaries", 0) or 0)
    failed = summary.get("failed_binary_paths") or []
    lines = [
        f"🗑 Удалено ({scope_label}):",
        f"• файлов: {files}",
        f"• чанков RAG: {chunks}",
        f"• кандидатов знаний: {candidates}",
        f"• бинарных файлов: {binaries}",
    ]
    if failed:
        lines.append(f"⚠️ Не удалось удалить файлы с диска: {len(failed)}")
    return "\n".join(lines)


async def _handle_file_delete_subcommand(
    *, normalized: NormalizedTelegramMessage, text: str
) -> dict[str, str]:
    parts = text.split()
    # parts[0] is '/file_delete' itself.
    if len(parts) < 2:
        await _send_dm(normalized.chat_id, _FILE_DELETE_USAGE)
        return {"status": "accepted", "route": "file_delete", "decision": "usage"}
    short_id = parts[1].lstrip("#").upper()
    token = settings.internal_service_token or ""
    has_confirm = _has_confirm_token(parts[2:])
    if not has_confirm:
        detail = await api_client.fetch_file_inspect(
            short_id=short_id,
            requester_username=normalized.username or "",
            internal_token=token,
        )
        if detail is None:
            await _send_dm(normalized.chat_id, f"Файл #{short_id} не найден.")
            return {
                "status": "accepted",
                "route": "file_delete",
                "decision": "not_found",
            }
        name = detail.get("source_file_name") or short_id
        await _send_dm(
            normalized.chat_id,
            (
                f"⚠️ Будет удалён без возможности восстановить: {name}.\n"
                f"Подтвердите: /file_delete {short_id} confirm"
            ),
        )
        return {
            "status": "accepted",
            "route": "file_delete",
            "decision": "warn",
        }
    summary = await api_client.delete_operator_file(
        short_id=short_id,
        requester_username=normalized.username or "",
        internal_token=token,
    )
    if summary is None:
        await _send_dm(normalized.chat_id, f"Файл #{short_id} не найден.")
        return {
            "status": "accepted",
            "route": "file_delete",
            "decision": "not_found",
        }
    await _send_dm(
        normalized.chat_id,
        _format_delete_summary(summary, scope_label=f"#{short_id}"),
    )
    return {
        "status": "accepted",
        "route": "file_delete",
        "decision": "deleted",
    }


async def _handle_files_delete_all_subcommand(
    *, normalized: NormalizedTelegramMessage, text: str
) -> dict[str, str]:
    parts = text.split()
    # parts[0] is '/files_delete_all'.
    has_confirm = _has_confirm_token(parts[1:])
    token = settings.internal_service_token or ""
    if not has_confirm:
        records = operator_file_repository.list_recent(
            username=normalized.username or "",
            limit=_FILES_DELETE_ALL_BULK_LIMIT,
        )
        if not records:
            await _send_dm(normalized.chat_id, "У вас нет сохранённых файлов.")
            return {
                "status": "accepted",
                "route": "files_delete_all",
                "decision": "empty",
            }
        await _send_dm(
            normalized.chat_id,
            (
                f"⚠️ Будет удалено навсегда {len(records)} файлов.\n"
                "Подтвердите: /files_delete_all confirm"
            ),
        )
        return {
            "status": "accepted",
            "route": "files_delete_all",
            "decision": "warn",
            "count": str(len(records)),
        }
    summary = await api_client.delete_all_operator_files(
        requester_username=normalized.username or "",
        internal_token=token,
    )
    await _send_dm(
        normalized.chat_id,
        _format_delete_summary(summary, scope_label="все файлы"),
    )
    return {
        "status": "accepted",
        "route": "files_delete_all",
        "decision": "deleted",
        "count": str(int(summary.get("deleted_files", 0) or 0)),
    }


def _format_file_inspect_dm(detail: dict) -> str:
    short_id = detail.get("short_id") or "?"
    name = detail.get("source_file_name") or short_id
    uploaded_by = detail.get("uploaded_by") or "?"
    uploaded_at = detail.get("uploaded_at") or "?"
    size = detail.get("file_size_bytes")
    size_str = (
        f"{size // 1024} KB" if isinstance(size, int) and size >= 1024 else f"{size or 0} B"
    )
    file_type = detail.get("source_file_type") or "?"
    confidential = "🔒 конфиденциально" if detail.get("is_confidential") else ""
    kb_status = detail.get("kb_ingest_status") or "?"
    chunks = detail.get("kb_inserted_chunks")
    kb_line = (
        f"🧩 KB: {kb_status} · {chunks} фрагментов"
        if isinstance(chunks, int)
        else f"🧩 KB: {kb_status}"
    )
    badge_line = " · ".join(
        part for part in [size_str, file_type, confidential] if part
    )
    candidate_text = detail.get("candidate_text") or ""
    head = candidate_text[:_FILE_INSPECT_HEAD_CHARS]
    lines = [
        f"📄 #{short_id} · {name}",
        f"👤 Загрузил: {uploaded_by}",
        f"🕐 {uploaded_at}",
        f"📦 {badge_line}",
        kb_line,
    ]
    if head.strip():
        lines.append("")
        lines.append(
            f"Извлечённый текст (первые {_FILE_INSPECT_HEAD_CHARS} символов):"
        )
        lines.append("─────────")
        lines.append(head)
        lines.append("─────────")
    else:
        lines.append("")
        lines.append(
            f"Извлечение текста недоступно (kb_ingest_status: {kb_status})."
        )
    return "\n".join(lines)


def _format_files_find_dm(*, query: str, payload: dict) -> str:
    total = int(payload.get("total", 0) or 0)
    items = payload.get("items") or []
    if total == 0 or not items:
        return "Ничего не найдено."
    lines = [f"🔎 По запросу «{query}» найдено ({total}):", ""]
    for item in items:
        short_id = item.get("short_id") or "?"
        name = item.get("source_file_name") or short_id
        uploaded_by = item.get("uploaded_by") or "?"
        uploaded_at = item.get("uploaded_at") or "?"
        snippet = item.get("snippet") or ""
        lines.append(f"📄 #{short_id} · {name} · {uploaded_by} · {uploaded_at}")
        if snippet:
            lines.append(f"   {snippet}")
    return "\n".join(lines)


async def _handle_file_inspect_subcommand(
    *, normalized: NormalizedTelegramMessage, text: str
) -> dict[str, str]:
    parts = text.split(maxsplit=1)
    if len(parts) < 2 or not parts[1].strip():
        await _send_dm(normalized.chat_id, _FILE_INSPECT_USAGE)
        return {"status": "accepted", "route": "file_inspect", "decision": "usage"}
    short_id = parts[1].strip().lstrip("#").upper()
    token = settings.internal_service_token or ""
    detail = await api_client.fetch_file_inspect(
        short_id=short_id,
        requester_username=normalized.username or "",
        internal_token=token,
    )
    if detail is None:
        await _send_dm(normalized.chat_id, f"Файл #{short_id} не найден.")
        return {
            "status": "accepted",
            "route": "file_inspect",
            "decision": "not_found",
        }
    await _send_dm(normalized.chat_id, _format_file_inspect_dm(detail))
    return {
        "status": "accepted",
        "route": "file_inspect",
        "decision": "shown",
    }


async def _handle_files_find_command(
    *, normalized: NormalizedTelegramMessage, text: str
) -> dict[str, str]:
    parts = text.split(maxsplit=1)
    if len(parts) < 2 or len(parts[1].strip()) < _FILES_FIND_MIN_QUERY:
        await _send_dm(normalized.chat_id, _FILES_FIND_USAGE)
        return {"status": "accepted", "route": "files_find", "decision": "usage"}
    query = parts[1].strip()
    token = settings.internal_service_token or ""
    payload = await api_client.search_files(
        query=query,
        requester_username=normalized.username or "",
        internal_token=token,
    )
    await _send_dm(
        normalized.chat_id, _format_files_find_dm(query=query, payload=payload)
    )
    return {
        "status": "accepted",
        "route": "files_find",
        "decision": "shown",
        "count": str(int(payload.get("total", 0) or 0)),
    }


async def _handle_operator_file_library_command(
    *,
    normalized: NormalizedTelegramMessage,
) -> dict[str, str] | None:
    """Dispatch `/files` and `/send` for the operator only.

    Returns None when the message is not one of these commands or the sender
    is not the configured operator (the normal routing continues).
    """
    if not normalized.username:
        return None
    if normalized.username != _effective_operator_username():
        return None
    text = normalized.text or ""
    if _FILES_TRIGGER_RE.match(text):
        return await _handle_files_command(normalized=normalized, text=text)
    if _SEND_TRIGGER_RE.match(text):
        return await _handle_send_command(normalized=normalized, text=text)
    return None


def _kb_kb_status_glyph(ingest_status: str) -> str:
    if ingest_status == "ok":
        return "✅"
    if ingest_status == "skipped":
        return "⏭️"
    if ingest_status.startswith("failed"):
        return "❌"
    return "…"


async def _handle_files_command(
    *,
    normalized: NormalizedTelegramMessage,
    text: str,
) -> dict[str, str]:
    parts = text.split()
    limit = settings.operator_files_list_default_limit
    if len(parts) >= 2:
        try:
            limit = max(1, int(parts[1]))
        except ValueError:
            limit = settings.operator_files_list_default_limit
    limit = min(limit, settings.operator_files_list_max_limit)
    records = operator_file_repository.list_recent(
        username=normalized.username or "", limit=limit
    )
    if not records:
        await _send_dm(normalized.chat_id, "Пока нет сохранённых файлов.")
        return {
            "status": "accepted",
            "route": "files_list",
            "decision": "empty",
        }
    lines: list[str] = [
        f"📂 Сохранённые файлы (последние {len(records)}):",
    ]
    for record in records:
        lines.append(_format_record_line(record))
    await _send_dm(normalized.chat_id, "\n".join(lines))
    return {
        "status": "accepted",
        "route": "files_list",
        "decision": "listed",
        "count": str(len(records)),
    }


def _format_record_line(record: OperatorFileRecord) -> str:
    name = record.source_file_name or record.telegram_file_id
    size = (
        f"{(record.file_size_bytes or 0) // 1024} KB"
        if record.file_size_bytes is not None
        else "—"
    )
    confidential = "🔒 " if record.is_confidential else ""
    glyph = _kb_kb_status_glyph(record.kb_ingest_status)
    when = record.created_at[:16].replace("T", " ")
    return (
        f"{confidential}{glyph} #{record.short_id} · {name} · {size} · "
        f"{record.download_status}/{record.kb_ingest_status} · {when}"
    )


def _parse_send_target(token: str) -> int | str | None:
    if token.startswith("@"):
        return token
    try:
        return int(token)
    except ValueError:
        return None


_SEND_USAGE_TEXT = "Использование: /send <id> <@username|chat_id>"


async def _handle_send_command(
    *,
    normalized: NormalizedTelegramMessage,
    text: str,
) -> dict[str, str]:
    parts = text.split()
    if len(parts) < 3:
        await _send_dm(normalized.chat_id, _SEND_USAGE_TEXT)
        return {
            "status": "accepted",
            "route": "file_send",
            "decision": "bad_format",
        }
    short_id = parts[1].lstrip("#")
    target = _parse_send_target(parts[2])
    if target is None:
        await _send_dm(normalized.chat_id, _SEND_USAGE_TEXT)
        return {
            "status": "accepted",
            "route": "file_send",
            "decision": "bad_target",
        }
    record = operator_file_repository.get(short_id=short_id)
    if record is None:
        await _send_dm(
            normalized.chat_id,
            f"Файл #{short_id} не найден.",
        )
        return {
            "status": "accepted",
            "route": "file_send",
            "decision": "short_id_unknown",
        }
    try:
        await telegram_file_sender.send_document_by_file_id(
            chat_id=target, file_id=record.telegram_file_id
        )
    except TelegramFileSendError as exc:
        if record.stored_binary_path:
            local_path = Path(record.stored_binary_path)
            try:
                await telegram_file_sender.send_document_local(
                    chat_id=target,
                    path=local_path,
                    file_name=record.source_file_name,
                )
            except TelegramFileSendError as fallback_exc:
                await _send_dm(
                    normalized.chat_id,
                    _format_send_failure_dm(record, fallback_exc),
                )
                return {
                    "status": "accepted",
                    "route": "file_send",
                    "decision": "send_failed",
                }
            await _send_dm(
                normalized.chat_id,
                f"Файл отправлен (локальная копия): #{record.short_id}",
            )
            return {
                "status": "accepted",
                "route": "file_send",
                "decision": "sent_local",
            }
        await _send_dm(
            normalized.chat_id, _format_send_failure_dm(record, exc)
        )
        return {
            "status": "accepted",
            "route": "file_send",
            "decision": "send_failed",
        }
    await _send_dm(
        normalized.chat_id, f"Файл отправлен: #{record.short_id}"
    )
    return {
        "status": "accepted",
        "route": "file_send",
        "decision": "sent_by_id",
    }


def _format_send_failure_dm(
    record: OperatorFileRecord, exc: TelegramFileSendError
) -> str:
    name = record.source_file_name or record.short_id
    description = exc.description or "ошибка"
    return f"Не удалось отправить #{record.short_id} ({name}): {description}"


async def _handle_operator_reply(normalized: NormalizedTelegramMessage) -> dict[str, str]:
    ticket_id = _extract_ticket_id(normalized.reply_to_text)
    if ticket_id is None:
        ticket_id = _fallback_open_ticket_for_operator(normalized.username or "")
    if ticket_id is None:
        # Disambiguation path: with 2+ assigned tickets the operator must
        # tell us which one this reply belongs to. Silently dropping the
        # reply (the historical behaviour) hides operator work from the
        # customer, which is the worst possible failure mode.
        open_tickets = hitl_ticket_repository.list_active_for_operator(
            normalized.username or ""
        )
        if open_tickets:
            await _safe_send_text(
                chat_id=normalized.chat_id,
                text=_format_disambiguation_dm(open_tickets),
            )
            logger.info(
                "operator_reply_disambiguation_requested",
                extra={
                    "operator_username": normalized.username,
                    "open_ticket_ids": [t.id for t in open_tickets],
                },
            )
            return {
                "status": "operator_reply_disambiguation_requested",
                "open_ticket_count": str(len(open_tickets)),
            }
        logger.warning(
            "operator_reply_unmatched_no_open_tickets",
            extra={"operator_username": normalized.username},
        )
        return {
            "status": "ignored",
            "reason": "operator_reply_unmatched",
        }
    try:
        await api_client.deliver_operator_reply(
            ticket_id=ticket_id,
            operator_username=normalized.username or "",
            reply_text=normalized.text,
        )
    except Exception as exc:  # broad: best-effort; api emits incidents internally
        logger.warning(
            "operator_reply_delivery_failed",
            extra={"ticket_id": ticket_id, "error": str(exc)},
        )
        return {"status": "failed", "reason": "operator_reply_delivery_failed"}
    return {"status": "operator_reply_delivered", "ticket_id": str(ticket_id)}


async def _forward_inbound_safe(
    *,
    text: str,
    chat_id: int,
    customer_username: str | None,
    trace_id: str,
) -> None:
    """Forward a customer message to the api, swallowing+logging failures.

    Runs as a FastAPI BackgroundTask after the webhook has already returned
    200 OK to Telegram. The api side emits its own incidents on failure;
    this wrapper exists so an exception in the background task does not
    propagate uncaught.
    """
    try:
        await api_client.forward_inbound(
            text=text,
            chat_id=chat_id,
            customer_username=customer_username,
            trace_id=trace_id,
        )
    except Exception as exc:  # broad: best-effort; api emits incidents on its side
        logger.warning(
            "inbound_forward_failed",
            extra={"trace_id": trace_id, "error": str(exc)},
        )


def _log_routed(*, trace_id: str, result: dict, fallback: str) -> None:
    route = result.get("route") or fallback
    logger.info(
        "telegram_update_routed",
        extra={
            "trace_id": trace_id,
            "route": route,
            "decision": (
                result.get("decision")
                or result.get("kb_mode")
                or result.get("status")
            ),
            "media_group_id": result.get("media_group_id"),
        },
    )


def _derive_trace_id(*, header_trace: str | None, update_id: object) -> str:
    """Deterministic trace_id for a Telegram update.

    Keying on the update_id ensures Telegram retries (which reuse the same
    update_id) collide on the same trace_id, so api-side idempotency on
    /conversations/inbound can short-circuit duplicate calls.
    """
    if header_trace:
        return header_trace
    if isinstance(update_id, int):
        return f"tg-update-{update_id}"
    return str(uuid.uuid4())


@app.post("/telegram/webhook")
async def telegram_webhook(
    request: Request,
    background_tasks: BackgroundTasks,
) -> dict[str, str]:
    try:
        payload = await request.json()
    except Exception as exc:  # pragma: no cover - defensive guard
        raise HTTPException(status_code=400, detail="invalid_json") from exc

    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="invalid_payload_type")

    trace_id = _derive_trace_id(
        header_trace=request.headers.get("X-Trace-Id"),
        update_id=payload.get("update_id"),
    )

    # Top-level guard: any unhandled exception below this point becomes a
    # 200 + structured failure marker so Telegram does not retry and amplify
    # the failure into duplicate user-visible messages. HTTPException is
    # re-raised since it represents an intentional 4xx (bad payload).
    try:
        return await _process_telegram_update(payload, trace_id, background_tasks)
    except HTTPException:
        raise
    except Exception as exc:
        logger.exception(
            "telegram_webhook_unhandled_exception",
            extra={"trace_id": trace_id, "error": str(exc)},
        )
        return {"status": "accepted", "handler": "failed", "trace_id": trace_id}


async def _process_telegram_update(
    payload: dict,
    trace_id: str,
    background_tasks: BackgroundTasks,
) -> dict[str, str]:
    try:
        normalized = normalize_update(payload)
    except TelegramUpdateValidationError as exc:
        logger.warning(
            "telegram_update_rejected",
            extra={"trace_id": trace_id, "reason": str(exc)},
        )
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    if normalized is None:
        logger.info(
            "telegram_update_ignored",
            extra={"trace_id": trace_id, "update_id": payload.get("update_id")},
        )
        return {"status": "ignored", "trace_id": trace_id}

    logger.info(
        "telegram_update_received",
        extra={
            "trace_id": trace_id,
            "update_id": normalized.update_id,
            "chat_id": normalized.chat_id,
            "username": normalized.username,
            "has_text": bool(normalized.text),
            "caption_present": normalized.caption is not None,
            "media_group_id": normalized.media_group_id,
            "attachment_count": len(normalized.attachments),
            "attachment_kinds": [a.kind for a in normalized.attachments],
            "attachment_sizes": [a.file_size for a in normalized.attachments],
        },
    )

    delete_result = await _handle_file_delete_command(normalized=normalized)
    if delete_result is not None:
        response = {"trace_id": trace_id}
        response.update(delete_result)
        _log_routed(trace_id=trace_id, result=delete_result, fallback="file_delete")
        return response

    inspect_result = await _handle_file_inspect_command(normalized=normalized)
    if inspect_result is not None:
        response = {"trace_id": trace_id}
        response.update(inspect_result)
        _log_routed(trace_id=trace_id, result=inspect_result, fallback="file_inspect")
        return response

    file_lib_result = await _handle_operator_file_library_command(
        normalized=normalized
    )
    if file_lib_result is not None:
        response = {"trace_id": trace_id}
        response.update(file_lib_result)
        _log_routed(trace_id=trace_id, result=file_lib_result, fallback="file_library")
        return response

    kb_result = await _handle_kb_command(normalized, background_tasks)
    if kb_result is not None:
        response = {"trace_id": trace_id}
        response.update(kb_result)
        _log_routed(trace_id=trace_id, result=kb_result, fallback="kb_command")
        return response

    session_result = await _handle_kb_session_continuation(
        normalized=normalized,
        background_tasks=background_tasks,
    )
    if session_result is not None:
        response = {"trace_id": trace_id}
        response.update(session_result)
        _log_routed(
            trace_id=trace_id, result=session_result, fallback="kb_continuation"
        )
        return response

    orphan_result = await _handle_operator_media_group_orphan(
        normalized=normalized,
        background_tasks=background_tasks,
    )
    if orphan_result is not None:
        response = {"trace_id": trace_id}
        response.update(orphan_result)
        _log_routed(
            trace_id=trace_id,
            result=orphan_result,
            fallback="media_group_orphan",
        )
        return response

    if normalized.text == "":
        logger.info(
            "telegram_attachment_only_message_ignored",
            extra={"trace_id": trace_id, "update_id": normalized.update_id},
        )
        return {"status": "ignored", "reason": "attachment_only", "trace_id": trace_id}

    admin_command_result = _handle_admin_hitl_command(
        username=normalized.username,
        text=normalized.text,
    )
    if admin_command_result is not None:
        response = {"trace_id": trace_id}
        response.update(admin_command_result)
        return response

    calendar_command_result = await handle_calendar_command(
        normalized=normalized,
        api_client=api_client,
        send_dm=_send_dm,
        primary_operator_username=_effective_operator_username(),
        internal_token=settings.internal_service_token or "",
        nl_ops_db_path=settings.nl_ops_db_path,
    )
    if calendar_command_result is not None:
        response = {"trace_id": trace_id}
        response.update(calendar_command_result)
        _log_routed(
            trace_id=trace_id,
            result=calendar_command_result,
            fallback="calendar_command",
        )
        return response

    prompt_command_result = await handle_prompt_command(
        normalized=normalized,
        api_client=api_client,
        send_dm=_send_dm,
        internal_token=settings.internal_service_token or "",
    )
    if prompt_command_result is not None:
        response = {"trace_id": trace_id}
        response.update(prompt_command_result)
        return response

    admin_project_result = await handle_admin_project_command(
        normalized=normalized,
        api_client=api_client,
        send_dm=_send_dm,
        admin_username=settings.admin_telegram_username,
        internal_token=settings.internal_service_token or "",
        operator_file_repository=operator_file_repository,
    )
    if admin_project_result is not None:
        response = {"trace_id": trace_id}
        response.update(admin_project_result)
        return response

    admin_nl_result = await handle_admin_nl_dialog(
        normalized=normalized,
        api_client=api_client,
        send_dm=_send_dm,
        admin_username=settings.admin_telegram_username,
    )
    if admin_nl_result is not None:
        response = {"trace_id": trace_id}
        response.update(admin_nl_result)
        return response

    sales_command_result = await handle_sales_command(
        normalized=normalized,
        api_client=api_client,
        send_dm=_send_dm,
        primary_operator_username=_effective_operator_username(),
        admin_username=settings.hitl_config_admin_username,
        internal_token=settings.internal_service_token or "",
    )
    if sales_command_result is not None:
        response = {"trace_id": trace_id}
        response.update(sales_command_result)
        _log_routed(
            trace_id=trace_id,
            result=sales_command_result,
            fallback="sales_command",
        )
        return response

    material_command_result = await handle_material_command(
        normalized=normalized,
        api_client=api_client,
        send_dm=_send_dm,
        primary_operator_username=_effective_operator_username(),
        admin_username=settings.hitl_config_admin_username,
        internal_token=settings.internal_service_token or "",
        downloader_factory=_material_downloader_factory,
        storage_root=Path(settings.sales_materials_storage_dir),
    )
    if material_command_result is not None:
        response = {"trace_id": trace_id}
        response.update(material_command_result)
        _log_routed(
            trace_id=trace_id,
            result=material_command_result,
            fallback="material_command",
        )
        return response

    operator_service_nl_result = await handle_operator_service_nl_message(
        normalized=normalized,
        api_client=api_client,
        send_dm=_send_dm,
        openrouter=operator_service_nl_openrouter,
        primary_operator_username=_effective_operator_username(),
        admin_username=settings.hitl_config_admin_username,
        internal_token=settings.internal_service_token or "",
    )
    if operator_service_nl_result is not None:
        response = {"trace_id": trace_id}
        response.update(operator_service_nl_result)
        _log_routed(
            trace_id=trace_id,
            result=operator_service_nl_result,
            fallback="operator_service_nl",
        )
        return response

    services_nl_result = await handle_services_nl_message(
        normalized=normalized,
        api_client=api_client,
        send_dm=_send_dm,
        primary_operator_username=_effective_operator_username(),
        internal_token=settings.internal_service_token or "",
    )
    if services_nl_result is not None:
        response = {"trace_id": trace_id}
        response.update(services_nl_result)
        return response

    whoami_result = await _handle_whoami_command(normalized=normalized)
    if whoami_result is not None:
        response = {"trace_id": trace_id}
        response.update(whoami_result)
        return response

    persona_result = await _handle_persona_command(normalized=normalized)
    if persona_result is not None:
        response = {"trace_id": trace_id}
        response.update(persona_result)
        return response

    help_result = await _handle_help_command(normalized=normalized)
    if help_result is not None:
        response = {"trace_id": trace_id}
        response.update(help_result)
        return response

    logger.info(
        "telegram_update_normalized",
        extra={
            "trace_id": trace_id,
            "update_id": normalized.update_id,
            "source_message_id": normalized.source_message_id,
            "chat_id": normalized.chat_id,
            "user_id": normalized.user_id,
        },
    )
    persisted = persist_normalized_message(
        telegram_user_id=normalized.user_id,
        source_message_id=normalized.source_message_id,
        text=normalized.text,
        trace_id=trace_id,
    )
    logger.info(
        "telegram_message_persisted",
        extra={
            "trace_id": trace_id,
            "source_message_id": normalized.source_message_id,
            "persisted": persisted,
        },
    )
    if not persisted:
        # Telegram retried the same update_id (or a script replayed the
        # webhook). The original message has already been forwarded to the
        # api; replaying would produce a second ack + ticket + operator DM.
        logger.info(
            "telegram_duplicate_update_ignored",
            extra={
                "trace_id": trace_id,
                "source_message_id": normalized.source_message_id,
                "update_id": normalized.update_id,
            },
        )
        return {
            "status": "ignored",
            "reason": "duplicate_source_message",
            "trace_id": trace_id,
        }

    pending_prompt_result = await dispatch_pending_prompt_edit(
        normalized=normalized,
        api_client=api_client,
        send_dm=_send_dm,
        internal_token=settings.internal_service_token or "",
    )
    if pending_prompt_result is not None:
        response = {"trace_id": trace_id}
        response.update(pending_prompt_result)
        return response

    operator_username = _effective_operator_username()
    if normalized.username and normalized.username == operator_username:
        operator_result = await _handle_operator_reply(normalized)
        response = {"trace_id": trace_id}
        response.update(operator_result)
        return response

    # Customer forward runs as a background task so the webhook returns 200
    # OK within a few milliseconds. The AnswerPipeline (LLM + RAG + verifier
    # + guardrails) frequently exceeds Telegram's ~5s webhook deadline; when
    # that happened in production Telegram retried and produced 3x acks.
    background_tasks.add_task(
        _forward_inbound_safe,
        text=normalized.text,
        chat_id=normalized.chat_id,
        customer_username=normalized.username,
        trace_id=trace_id,
    )
    return {"status": "accepted", "trace_id": trace_id}

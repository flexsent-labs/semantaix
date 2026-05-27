"""Slash-command dispatcher for ``/material*`` (Story 12.05).

Mirrors the Story 12.02 ``sales_command_dispatch`` shape:

* ``/material [caption]`` — must be a reply to a message with a
  ``video`` / ``photo`` / ``document`` attachment. Downloads the binary
  via the existing :class:`TelegramFileDownloader` (the same helper
  ``/kb_add`` uses — no duplicated fetch/store) and posts the metadata
  + the original ``telegram_file_id`` to ``POST /sales/materials``.
* ``/material_list`` — calls ``GET /sales/materials`` and renders one
  short line per active row.
* ``/material_remove <id>`` — calls ``DELETE /sales/materials/{id}``.

Gating mirrors the other sales slash-commands: the sender must be a
registered active operator OR the configured admin. Unauthorized senders
are dropped silently with a structured ``unauthorized_material_command``
log line.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any, Protocol

import httpx

from services.bot_gateway.app.api_client import ApiClient, ApiError
from services.bot_gateway.app.operator_resolver import (
    ResolvedOperator,
    resolve_operator_for_sender,
)
from services.bot_gateway.app.telegram_update import (
    NormalizedTelegramMessage,
    TelegramAttachment,
)

logger = logging.getLogger(__name__)

SendDmFn = Callable[[int, str], Awaitable[Any]]


class _Downloader(Protocol):
    async def download(
        self,
        *,
        file_id: str,
        suggested_extension: str,
        mime_type: str | None = None,
    ) -> Any: ...


DownloaderFactory = Callable[[Path], _Downloader]

_MATERIAL_TRIGGER_RE = re.compile(r"^\s*/material(\b|$)", re.IGNORECASE)
_MATERIAL_LIST_TRIGGER_RE = re.compile(
    r"^\s*/material_list\b", re.IGNORECASE
)
_MATERIAL_REMOVE_TRIGGER_RE = re.compile(
    r"^\s*/material_remove\b", re.IGNORECASE
)

_USAGE_REPLY = (
    "Использование: ответьте на видео/фото/документ командой "
    "/material [подпись]."
)
_USAGE_REMOVE = "Использование: /material_remove <id>"
_LIST_EMPTY_HINT = "Материалов пока нет."
_API_UNAVAILABLE = "Сервис временно недоступен, попробуйте позже."
_CAPTION_MAX_CHARS = 200

_ALLOWED_KINDS: dict[str, str] = {
    "video": "video",
    "photo": "photo",
    "document": "document",
}


async def handle_material_command(
    *,
    normalized: NormalizedTelegramMessage,
    api_client: ApiClient,
    send_dm: SendDmFn,
    primary_operator_username: str,
    admin_username: str,
    internal_token: str,
    downloader_factory: DownloaderFactory,
    storage_root: Path,
) -> dict[str, str] | None:
    """Top-level dispatcher; returns ``None`` for non-``/material*`` messages."""
    text = normalized.text or ""
    if _MATERIAL_LIST_TRIGGER_RE.match(text):
        return await _dispatch(
            normalized=normalized,
            api_client=api_client,
            send_dm=send_dm,
            primary_operator_username=primary_operator_username,
            admin_username=admin_username,
            internal_token=internal_token,
            route="material_list",
            handler=lambda **kw: _handle_material_list(**kw),
            extras={},
        )
    if _MATERIAL_REMOVE_TRIGGER_RE.match(text):
        return await _dispatch(
            normalized=normalized,
            api_client=api_client,
            send_dm=send_dm,
            primary_operator_username=primary_operator_username,
            admin_username=admin_username,
            internal_token=internal_token,
            route="material_remove",
            handler=lambda **kw: _handle_material_remove(**kw),
            extras={},
        )
    if _MATERIAL_TRIGGER_RE.match(text):
        return await _dispatch(
            normalized=normalized,
            api_client=api_client,
            send_dm=send_dm,
            primary_operator_username=primary_operator_username,
            admin_username=admin_username,
            internal_token=internal_token,
            route="material",
            handler=lambda **kw: _handle_material_register(
                downloader_factory=downloader_factory,
                storage_root=storage_root,
                **kw,
            ),
            extras={},
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
    extras: dict[str, Any],
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
            "unauthorized_material_command",
            extra={
                "trace_id": trace_id,
                "from_username": normalized.username,
                "route": route,
            },
        )
        return {
            "status": "ignored",
            "reason": "unauthorized_material_command",
        }
    if resolved is None or resolved.project_id is None:
        logger.warning(
            "unauthorized_material_command",
            extra={
                "trace_id": trace_id,
                "from_username": normalized.username,
                "route": route,
                "note": "admin_has_no_project_mapping",
            },
        )
        return {
            "status": "ignored",
            "reason": "unauthorized_material_command",
        }
    return await handler(
        normalized=normalized,
        resolved=resolved,
        api_client=api_client,
        send_dm=send_dm,
        internal_token=internal_token,
        trace_id=trace_id,
        **extras,
    )


# --- /material --------------------------------------------------------------


def _kind_for_attachment(attachment: TelegramAttachment) -> str | None:
    if attachment.kind == "video":
        return "video"
    if attachment.kind == "photo":
        return "photo"
    if attachment.kind == "document":
        # Treat PDF documents as ``pdf`` so the dispatch endpoint routes via
        # ``send_document`` with the correct caption rules.
        mime = (attachment.mime_type or "").lower()
        name = (attachment.file_name or "").lower()
        if mime == "application/pdf" or name.endswith(".pdf"):
            return "pdf"
        return "document"
    return None


def _extension_for(attachment: TelegramAttachment, kind: str) -> str:
    if attachment.file_name and "." in attachment.file_name:
        return attachment.file_name.rsplit(".", 1)[-1]
    fallback = {"video": "mp4", "photo": "jpg", "pdf": "pdf", "document": "bin"}
    return fallback.get(kind, "bin")


async def _handle_material_register(
    *,
    normalized: NormalizedTelegramMessage,
    resolved: ResolvedOperator,
    api_client: ApiClient,
    send_dm: SendDmFn,
    internal_token: str,
    trace_id: str,
    downloader_factory: DownloaderFactory,
    storage_root: Path,
) -> dict[str, str]:
    text = normalized.text or ""
    body = text.split(maxsplit=1)
    caption_arg = body[1].strip() if len(body) >= 2 else ""
    attachment = normalized.reply_to_attachment
    if attachment is None:
        await send_dm(normalized.chat_id, _USAGE_REPLY)
        return {
            "status": "error",
            "route": "material",
            "decision": (
                "no_media" if normalized.reply_to_text else "no_reply"
            ),
        }
    kind = _kind_for_attachment(attachment)
    if kind is None:
        await send_dm(normalized.chat_id, _USAGE_REPLY)
        return {
            "status": "error",
            "route": "material",
            "decision": "unsupported_attachment",
        }
    caption = caption_arg or (normalized.reply_to_caption or "").strip()
    if len(caption) > _CAPTION_MAX_CHARS:
        await send_dm(
            normalized.chat_id,
            f"Подпись длиннее {_CAPTION_MAX_CHARS} символов.",
        )
        return {
            "status": "error",
            "route": "material",
            "decision": "caption_too_long",
        }

    extension = _extension_for(attachment, kind)
    storage_dir = storage_root / str(int(resolved.project_id or 0))
    storage_dir.mkdir(parents=True, exist_ok=True)
    downloader = downloader_factory(storage_dir)
    try:
        downloaded = await downloader.download(
            file_id=attachment.file_id,
            suggested_extension=extension,
            mime_type=attachment.mime_type,
        )
    except Exception as exc:
        logger.warning(
            "material_command_download_failed",
            extra={
                "trace_id": trace_id,
                "file_id_present": True,
                "error": str(exc),
            },
        )
        await send_dm(
            normalized.chat_id,
            "Не удалось скачать файл, попробуйте ещё раз.",
        )
        return {
            "status": "error",
            "route": "material",
            "decision": "download_failed",
        }
    try:
        result = await api_client.add_sales_material(
            project_id=int(resolved.project_id or 0),
            kind=kind,
            local_path=str(downloaded.path),
            byte_size=int(downloaded.byte_size),
            caption=caption or None,
            tags=None,
            telegram_file_id=attachment.file_id,
            internal_token=internal_token,
        )
    except (ApiError, httpx.HTTPStatusError, httpx.RequestError, OSError) as exc:
        await _log_api_error_and_dm(
            send_dm=send_dm,
            normalized=normalized,
            trace_id=trace_id,
            route="material",
            exc=exc,
        )
        return {"status": "error", "route": "material"}
    material_id = int(result.get("id", 0))
    await send_dm(
        normalized.chat_id,
        f'Добавлено: {kind} id={material_id} (caption="{caption}")',
    )
    return {
        "status": "ok",
        "route": "material",
        "material_id": str(material_id),
    }


# --- /material_list ---------------------------------------------------------


async def _handle_material_list(
    *,
    normalized: NormalizedTelegramMessage,
    resolved: ResolvedOperator,
    api_client: ApiClient,
    send_dm: SendDmFn,
    internal_token: str,
    trace_id: str,
) -> dict[str, str]:
    try:
        body = await api_client.list_sales_materials(
            project_id=int(resolved.project_id or 0),
            internal_token=internal_token,
        )
    except (ApiError, httpx.HTTPStatusError, httpx.RequestError, OSError) as exc:
        await _log_api_error_and_dm(
            send_dm=send_dm,
            normalized=normalized,
            trace_id=trace_id,
            route="material_list",
            exc=exc,
        )
        return {"status": "error", "route": "material_list"}
    materials = body.get("materials") or []
    if not materials:
        await send_dm(normalized.chat_id, _LIST_EMPTY_HINT)
        return {
            "status": "ok",
            "route": "material_list",
            "decision": "empty",
        }
    lines = [_render_material_row(row) for row in materials]
    await send_dm(normalized.chat_id, "\n".join(lines))
    return {
        "status": "ok",
        "route": "material_list",
        "count": str(len(materials)),
    }


def _render_material_row(row: dict) -> str:
    rid = row.get("id", "?")
    kind = row.get("kind", "?")
    caption = (row.get("caption") or "").strip()
    tags = row.get("tags") or []
    tail_parts: list[str] = []
    if caption:
        tail_parts.append(f'"{caption}"')
    if tags:
        tail_parts.append("[" + ",".join(str(t) for t in tags) + "]")
    tail = " · ".join(tail_parts)
    if tail:
        return f"{rid}. {kind} — {tail}"
    return f"{rid}. {kind}"


# --- /material_remove -------------------------------------------------------


async def _handle_material_remove(
    *,
    normalized: NormalizedTelegramMessage,
    resolved: ResolvedOperator,
    api_client: ApiClient,
    send_dm: SendDmFn,
    internal_token: str,
    trace_id: str,
) -> dict[str, str]:
    text = normalized.text or ""
    parts = text.split()
    if len(parts) != 2 or not parts[1].isdigit():
        await send_dm(normalized.chat_id, _USAGE_REMOVE)
        return {
            "status": "error",
            "route": "material_remove",
            "decision": "usage",
        }
    material_id = int(parts[1])
    if material_id <= 0:
        await send_dm(normalized.chat_id, _USAGE_REMOVE)
        return {
            "status": "error",
            "route": "material_remove",
            "decision": "usage",
        }
    try:
        await api_client.delete_sales_material(
            material_id=material_id, internal_token=internal_token
        )
    except ApiError as exc:
        if exc.detail == "material_not_found":
            await send_dm(
                normalized.chat_id, f"Не найдено: id={material_id}"
            )
            return {
                "status": "error",
                "route": "material_remove",
                "decision": "not_found",
            }
        await _log_api_error_and_dm(
            send_dm=send_dm,
            normalized=normalized,
            trace_id=trace_id,
            route="material_remove",
            exc=exc,
        )
        return {
            "status": "error",
            "route": "material_remove",
            "detail": exc.detail or "",
        }
    except (httpx.HTTPStatusError, httpx.RequestError, OSError) as exc:
        await _log_api_error_and_dm(
            send_dm=send_dm,
            normalized=normalized,
            trace_id=trace_id,
            route="material_remove",
            exc=exc,
        )
        return {"status": "error", "route": "material_remove"}
    await send_dm(normalized.chat_id, f"Удалено: id={material_id}")
    return {
        "status": "ok",
        "route": "material_remove",
        "material_id": str(material_id),
    }


# --- shared error helper ----------------------------------------------------


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
        "material_command_api_error",
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

"""Operator natural-language services dialog (Epic 13, story 13.05).

Bot-side dispatcher that turns operator DMs like ``"добавь услугу маникюр …"``
into propose/confirm/cancel calls against the api's project-scoped services
NL-ops state machine (story 13.04 endpoints under
``/api/projects/{project_id}/services/nl-ops``).

Mirrors :mod:`services.bot_gateway.app.admin_nl_dialog` shape — same
propose/confirm/cancel routing, ``да`` / ``нет`` / ``/confirm`` / ``/cancel``
recognition. Differences (call-outs per the story scope):

1. **Project-scoped** rather than global; the sender must be a registered
   operator on the project (Epic 10 ``operators`` registry, resolved via
   :func:`services.bot_gateway.app.operator_resolver.resolve_operator_for_sender`).
2. **Operator-gated** rather than admin-username-gated. Non-registered →
   ignored with logged reason ``unauthorized_services`` and **no DM**
   (avoids customer-thread leakage if a non-operator types the trigger
   phrase in a customer conversation).
3. **Plain-text preview** — every ``send_dm`` call uses ``parse_mode=None``.
   Operator-supplied content in the api preview is already length-capped at
   200 chars by ``parse_service_intent``; the bot does not re-escape
   because it never asks Telegram to interpret Markdown / HTML.

The api response for ``propose`` echoes ``prior_cancelled_session_id`` when
the single-pending invariant flipped a prior session to ``cancelled``; the
dispatcher DMs an explicit cancellation notice BEFORE the new preview so
the operator knows the older draft is gone.

The api's ``latest-pending`` endpoint does NOT echo confirm tokens (security
H4 — tokens stay one-way after propose). The dispatcher therefore caches the
plaintext token returned by the most-recent ``propose`` call in a small
in-memory dict keyed by ``(project_id, operator_username)`` so a ``да`` /
``нет`` reply can be matched to its session without a round trip. The cache
is cleared on confirm / cancel and trimmed by session expiry-driven misses.
"""

from __future__ import annotations

import asyncio
import logging
import re
from collections.abc import Awaitable, Callable
from typing import Any

import httpx

from services.bot_gateway.app.api_client import ApiClient, ApiError
from services.bot_gateway.app.operator_resolver import resolve_operator_for_sender
from services.bot_gateway.app.telegram_update import NormalizedTelegramMessage

logger = logging.getLogger(__name__)

SendDmFn = Callable[[int, str], Awaitable[Any]]

# Operator-content cap mirrors ``services_nl_ops._MAX_OPERATOR_TEXT_CHARS``;
# kept local to avoid a cross-service import for a single integer constant.
_MAX_OPERATOR_TEXT_CHARS = 200

# Start-of-message-anchored trigger regex. ё/е equivalence is normalized
# in :func:`_text_matches_trigger` before matching so the regex itself can
# stay tight to the canonical ``е`` form. Anchoring prevents quote-reply
# triggers (``> добавь услугу …``).
_TRIGGER_RE = re.compile(
    r"^\s*(?:добавь|добавьте|новая|создай|удали|удалите|измени|измените)\s+услуг[ауы]\b",
    re.IGNORECASE,
)

# Tight token regex matches ``secrets.token_urlsafe(16)`` output (22 url-safe
# base64 chars). A tight regex spares a network round trip for obvious typos
# / pasted noise; the api re-validates the token authoritatively.
_CONFIRM_SLASH_RE = re.compile(
    r"^\s*/confirm(?:\s+(?P<token>[A-Za-z0-9_-]{22}))?\s*$",
    re.IGNORECASE,
)
_CANCEL_SLASH_RE = re.compile(r"^\s*/cancel\s*$", re.IGNORECASE)

# Process-local cache of the most-recent propose's confirm_token per
# ``(project_id, operator_username)``. Cleared on confirm/cancel + on a
# 410 ``session_not_pending`` so a stale entry does not cause repeated
# 401s. Module-level state is acceptable here because:
# - the cache is read/written only from the asyncio event loop;
# - bot_gateway is a single-process service in the deployed stack;
# - a process restart safely falls back to "no cached token" → the api
#   simply rejects the bare ``да`` until the operator either re-proposes
#   or types ``/confirm <token>`` explicitly (the token was DM'd to them).
_TOKEN_CACHE: dict[tuple[int, str], tuple[int, str]] = {}


def _cache_token(*, project_id: int, operator: str, session_id: int, token: str) -> None:
    _TOKEN_CACHE[(project_id, operator)] = (session_id, token)


def _pop_token(*, project_id: int, operator: str) -> tuple[int, str] | None:
    return _TOKEN_CACHE.pop((project_id, operator), None)


def _peek_token(*, project_id: int, operator: str) -> tuple[int, str] | None:
    return _TOKEN_CACHE.get((project_id, operator))


def escape_and_cap(value: str | None, *, max_len: int = _MAX_OPERATOR_TEXT_CHARS) -> str:
    """Strip control chars + clip to the operator-content length cap.

    Returns an empty string for ``None`` input. Appends a trailing ``…`` on
    truncation so the operator sees that their text was cut. Control chars
    (``\\x00..\\x1f`` excl. tab/newline + ``\\x7f``) are stripped. This is
    only used on operator-echoed content in the OP_UNKNOWN clarification
    path — the api preview itself is already deterministic + capped.
    """
    if not value:
        return ""
    cleaned = "".join(
        ch for ch in value if ch == "\n" or ch == "\t" or (" " <= ch < "\x7f") or ch >= "\xa0"
    )
    if len(cleaned) > max_len:
        return cleaned[:max_len] + "…"
    return cleaned


def _text_matches_trigger(text: str) -> bool:
    """ё/е-insensitive start-of-message keyword match."""
    if not text:
        return False
    normalized = text.replace("ё", "е").replace("Ё", "Е")
    return _TRIGGER_RE.match(normalized) is not None


def _is_confirm_word(text: str) -> bool:
    return text.strip().casefold() == "да"


def _is_cancel_word(text: str) -> bool:
    return text.strip().casefold() == "нет"


def _format_preview_dm(preview: str, *, token: str) -> str:
    """Render the propose preview + confirm/cancel instructions (plain text)."""
    return (
        f"{preview}\n"
        f"Подтвердите ответом «да» или /confirm {token}. "
        "Отмена: «нет» или /cancel."
    )


def _map_confirm_error(detail: str | None) -> str:
    """Map api ``ApiError.detail`` to a Russian operator-facing DM string."""
    if detail == "invalid_confirm_token":
        return "Неверный токен."
    if detail == "not_session_owner":
        return "Сессия не принадлежит вам."
    if detail == "admin_cannot_remove_service":
        return "Удаление услуги доступно только оператору, не администратору."
    if detail == "session_expired":
        return "Сессия истекла, начните заново."
    if detail and detail.startswith("session_not_pending"):
        return "Сессия уже применена или отменена."
    return "Не удалось подтвердить."


def _map_cancel_error(detail: str | None) -> str:
    if detail == "not_session_owner":
        return "Сессия не принадлежит вам."
    if detail and detail.startswith("session_not_pending"):
        return "Сессия уже применена или отменена."
    return "Не удалось отменить."


async def handle_services_nl_message(
    *,
    normalized: NormalizedTelegramMessage,
    api_client: ApiClient,
    send_dm: SendDmFn,
    internal_token: str,
) -> dict[str, str] | None:
    """Top-level dispatcher for the operator services NL dialog.

    Returns ``None`` when the message is not a services-NL trigger and not a
    confirm/cancel reply with a cached pending session — so the caller in
    ``main.py`` falls through to other handlers / the customer path.

    Returns a dict with at least ``status`` + ``reason``/``route`` when the
    message was handled or deliberately ignored (e.g. trigger matched but
    sender is not a registered operator → ``unauthorized_services``).
    """
    text = normalized.text or ""
    is_trigger = _text_matches_trigger(text)
    is_confirm_word = _is_confirm_word(text)
    is_cancel_word = _is_cancel_word(text)
    confirm_slash = _CONFIRM_SLASH_RE.match(text)
    cancel_slash = _CANCEL_SLASH_RE.match(text)
    is_confirm_slash = confirm_slash is not None
    is_cancel_slash = cancel_slash is not None

    # Fast-path: messages that match no shape at all skip the operator
    # registry lookup entirely so this dispatcher costs nothing on the hot
    # customer-message path.
    if not (
        is_trigger
        or is_confirm_word
        or is_cancel_word
        or is_confirm_slash
        or is_cancel_slash
    ):
        return None

    # For confirm/cancel words (da/net) without a cached pending session,
    # fall through silently — they may be legitimate customer / operator
    # text destined for another handler.
    if (is_confirm_word or is_cancel_word) and not is_trigger and not (
        is_confirm_slash or is_cancel_slash
    ):
        # Peek-only — we still gate on operator before we commit to handling.
        pass

    resolved = await resolve_operator_for_sender(
        username=normalized.username,
        api_client=api_client,
    )

    if resolved is None or resolved.project_id is None:
        if is_trigger:
            logger.warning(
                "services_nl_unauthorized",
                extra={
                    "username": normalized.username,
                    "reason": "unauthorized_services",
                },
            )
            return {"status": "ignored", "reason": "unauthorized_services", "routed": "true"}
        # Confirm/cancel words from a non-operator are not services-NL —
        # let the rest of the pipeline handle them.
        return None

    project_id = int(resolved.project_id)
    operator = resolved.username

    if is_confirm_slash:
        return await _handle_confirm(
            chat_id=normalized.chat_id,
            project_id=project_id,
            operator=operator,
            explicit_token=confirm_slash.group("token"),
            api_client=api_client,
            send_dm=send_dm,
            internal_token=internal_token,
        )

    if is_cancel_slash or is_cancel_word:
        # Cancel words ignored when no cached pending — keep silent so a bare
        # "нет" anywhere else isn't blackholed by this dispatcher.
        cached = _peek_token(project_id=project_id, operator=operator)
        if is_cancel_word and cached is None:
            return None
        return await _handle_cancel(
            chat_id=normalized.chat_id,
            project_id=project_id,
            operator=operator,
            api_client=api_client,
            send_dm=send_dm,
            internal_token=internal_token,
        )

    if is_confirm_word:
        cached = _peek_token(project_id=project_id, operator=operator)
        if cached is None:
            # Same UX as cancel: silent fall-through — operator can still
            # confirm explicitly via /confirm <token>.
            return None
        return await _handle_confirm(
            chat_id=normalized.chat_id,
            project_id=project_id,
            operator=operator,
            explicit_token=None,
            api_client=api_client,
            send_dm=send_dm,
            internal_token=internal_token,
        )

    # Trigger keyword → propose.
    return await _handle_propose(
        chat_id=normalized.chat_id,
        project_id=project_id,
        operator=operator,
        utterance=text,
        api_client=api_client,
        send_dm=send_dm,
        internal_token=internal_token,
    )


async def _handle_propose(
    *,
    chat_id: int,
    project_id: int,
    operator: str,
    utterance: str,
    api_client: ApiClient,
    send_dm: SendDmFn,
    internal_token: str,
) -> dict[str, str]:
    try:
        body = await api_client.services_nl_propose(
            project_id=project_id,
            originating_operator=operator,
            text=utterance,
            internal_token=internal_token,
        )
    except (ApiError, httpx.HTTPStatusError) as exc:
        status = (
            exc.response.status_code
            if getattr(exc, "response", None) is not None
            else 0
        )
        await send_dm(chat_id, f"Ошибка ({status}). Попробуйте позже.")
        return {
            "status": "error",
            "route": "services_nl_propose",
            "http_status": str(status),
        }
    status = str(body.get("status", ""))
    preview = str(body.get("preview", ""))
    if status == "clarify":
        # OP_UNKNOWN → DM just the api-provided clarification; the api
        # preview is already deterministic. We additionally re-cap any
        # raw operator text that may have been echoed in clarification.
        await send_dm(chat_id, escape_and_cap(preview))
        return {
            "status": "ok",
            "route": "services_nl_propose",
            "decision": "clarify",
        }
    # Single-pending replacement notice → DM BEFORE the new preview.
    if body.get("prior_cancelled_session_id") is not None:
        await send_dm(
            chat_id,
            "Ваш предыдущий запрос отменён, заменён новым.",
        )
    token = str(body.get("confirm_token", ""))
    session_id_raw = body.get("session_id")
    if not token or session_id_raw is None:
        await send_dm(chat_id, "Сервис временно недоступен.")
        return {
            "status": "error",
            "route": "services_nl_propose",
            "reason": "missing_token",
        }
    session_id = int(session_id_raw)
    _cache_token(
        project_id=project_id,
        operator=operator,
        session_id=session_id,
        token=token,
    )
    await send_dm(chat_id, _format_preview_dm(preview, token=token))
    return {
        "status": "ok",
        "route": "services_nl_propose",
        "session_id": str(session_id),
    }


async def _handle_confirm(
    *,
    chat_id: int,
    project_id: int,
    operator: str,
    explicit_token: str | None,
    api_client: ApiClient,
    send_dm: SendDmFn,
    internal_token: str,
) -> dict[str, str]:
    cached = _peek_token(project_id=project_id, operator=operator)
    # Resolve which session to confirm:
    # - explicit token from /confirm <token> overrides the cached entry;
    # - bare ``да`` uses the cached entry if any.
    session_id: int | None = None
    confirm_token: str | None = None
    if explicit_token is not None:
        confirm_token = explicit_token
        # Look up the latest-pending row to learn its session_id; the api
        # scopes by operator so it cannot return another operator's session.
        try:
            latest = await api_client.services_nl_latest_pending(
                project_id=project_id,
                operator=operator,
                internal_token=internal_token,
            )
        except (ApiError, httpx.HTTPStatusError) as exc:
            status = (
                exc.response.status_code
                if getattr(exc, "response", None) is not None
                else 0
            )
            await send_dm(chat_id, f"Ошибка поиска сессии ({status}).")
            return {
                "status": "error",
                "route": "services_nl_confirm",
                "http_status": str(status),
            }
        if latest is None:
            await send_dm(chat_id, "Нет ожидающих подтверждения операций.")
            return {
                "status": "ok",
                "route": "services_nl_confirm",
                "decision": "no_pending",
            }
        session_id = int(latest["session_id"])
    elif cached is not None:
        session_id, confirm_token = cached
    else:
        await send_dm(chat_id, "Нет ожидающих подтверждения операций.")
        return {
            "status": "ok",
            "route": "services_nl_confirm",
            "decision": "no_pending",
        }

    assert session_id is not None
    assert confirm_token is not None
    try:
        body = await api_client.services_nl_confirm(
            project_id=project_id,
            session_id=session_id,
            presenter_operator=operator,
            confirm_token=confirm_token,
            internal_token=internal_token,
        )
    except ApiError as exc:
        detail = exc.detail
        await send_dm(chat_id, _map_confirm_error(detail))
        # 410 session_not_pending → cached token is stale; clear it so the
        # next ``да`` doesn't repeat the same dead-end.
        if detail and (
            detail.startswith("session_not_pending")
            or detail == "session_expired"
            or detail == "invalid_confirm_token"
        ):
            _pop_token(project_id=project_id, operator=operator)
        return {
            "status": "error",
            "route": "services_nl_confirm",
            "detail": detail or "",
        }
    except httpx.HTTPStatusError as exc:
        status = exc.response.status_code if exc.response is not None else 0
        await send_dm(chat_id, f"Не удалось подтвердить ({status}).")
        return {
            "status": "error",
            "route": "services_nl_confirm",
            "http_status": str(status),
        }
    op_type = str(body.get("applied_op_type") or body.get("op_type") or "")
    _pop_token(project_id=project_id, operator=operator)
    await send_dm(chat_id, f"Операция применена: {op_type}.")
    return {
        "status": "ok",
        "route": "services_nl_confirm",
        "op_type": op_type,
    }


async def _handle_cancel(
    *,
    chat_id: int,
    project_id: int,
    operator: str,
    api_client: ApiClient,
    send_dm: SendDmFn,
    internal_token: str,
) -> dict[str, str]:
    cached = _peek_token(project_id=project_id, operator=operator)
    if cached is None:
        # /cancel with no cached entry → look up latest-pending so the
        # operator can cancel a session even after a bot restart.
        try:
            latest = await api_client.services_nl_latest_pending(
                project_id=project_id,
                operator=operator,
                internal_token=internal_token,
            )
        except (ApiError, httpx.HTTPStatusError) as exc:
            status = (
                exc.response.status_code
                if getattr(exc, "response", None) is not None
                else 0
            )
            await send_dm(chat_id, f"Ошибка поиска сессии ({status}).")
            return {
                "status": "error",
                "route": "services_nl_cancel",
                "http_status": str(status),
            }
        if latest is None:
            await send_dm(chat_id, "Нет ожидающих подтверждения операций.")
            return {
                "status": "ok",
                "route": "services_nl_cancel",
                "decision": "no_pending",
            }
        session_id = int(latest["session_id"])
    else:
        session_id = cached[0]

    try:
        await api_client.services_nl_cancel(
            project_id=project_id,
            session_id=session_id,
            presenter_operator=operator,
            internal_token=internal_token,
        )
    except ApiError as exc:
        await send_dm(chat_id, _map_cancel_error(exc.detail))
        _pop_token(project_id=project_id, operator=operator)
        return {
            "status": "error",
            "route": "services_nl_cancel",
            "detail": exc.detail or "",
        }
    except httpx.HTTPStatusError as exc:
        status = exc.response.status_code if exc.response is not None else 0
        await send_dm(chat_id, f"Не удалось отменить ({status}).")
        return {
            "status": "error",
            "route": "services_nl_cancel",
            "http_status": str(status),
        }
    _pop_token(project_id=project_id, operator=operator)
    await send_dm(chat_id, "Запрос отменён.")
    return {"status": "ok", "route": "services_nl_cancel"}


# A tiny re-export so unit tests can clear cache between runs without
# reaching into the private name.
def _reset_token_cache_for_tests() -> None:
    _TOKEN_CACHE.clear()


# Silence unused-import lint when the module is loaded standalone.
_ = asyncio

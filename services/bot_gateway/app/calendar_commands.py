"""Operator `/connect_calendar` + `/disconnect_calendar` commands (Epic 11, story 11.03).

Give the project's designated calendar operator a Telegram entry point:

- `/connect_calendar` asks the api to mint a Google consent URL and DMs it with a
  short Russian instruction. **Connect IS enable**: a successful OAuth callback
  also flips the project to enabled and records the connecting operator as the
  designated calendar operator atomically with the token upsert. There is no
  separate `/calendar_on` command — re-enable after `/calendar_off` means the
  operator re-runs `/connect_calendar`.
- `/disconnect_calendar` asks the api to revoke + delete the stored token and DMs
  a Russian confirmation.
- `/calendar_off` pauses the feature for the operator's project without losing
  the stored token; `/calendar_service add|remove …` manages per-service rules.

Gating mirrors the existing operator-command dispatch (`kb_intent._SLASH_RE`,
`/hitl_config`): the sender is resolved against the Epic-10 operator registry, and
only a registered operator bound to a project may run these. The api's
``/calendar/connect/initiate`` does NOT gate on project-enabled or
designated-operator (so first-time bootstrap and operator handover work);
the OAuth callback is the authoritative gate and records the connecting
operator as the designated calendar operator on success. Read-only availability
endpoints continue to enforce the designated-operator rule via
``services.api.app.calendar.authorization`` (403 ``not_calendar_operator``).

Never log the consent URL (it carries a single-use ``state``) or any token.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Any

import httpx

from services.api.app.calendar.calendar_service_alias_hint_repository import (
    should_send_calendar_service_alias_hint,
)
from services.bot_gateway.app.api_client import ApiClient, ApiError
from services.bot_gateway.app.operator_resolver import resolve_operator_for_sender
from services.bot_gateway.app.telegram_update import NormalizedTelegramMessage

logger = logging.getLogger(__name__)

SendDmFn = Callable[[int, str], Awaitable[Any]]

_CONNECT_RE = re.compile(r"^\s*/connect_calendar\b", re.IGNORECASE)
_DISCONNECT_RE = re.compile(r"^\s*/disconnect_calendar\b", re.IGNORECASE)
_CALENDAR_OFF_RE = re.compile(r"^\s*/calendar_off\b\s*$", re.IGNORECASE)
_CALENDAR_SERVICE_RE = re.compile(r"^\s*/calendar_service\b\s*(?P<rest>.*)$", re.IGNORECASE)
# Story 13.03 — start-of-message anchored `/service` slash command. Subcommand
# is required; trailing args are parsed by ``parse_service_kv``. Quoted-reply
# (`> /service add ...`) does NOT match because the ``>`` prefix breaks the
# ``^\s*/service`` anchor.
_SERVICE_RE = re.compile(
    r"^\s*/service\b\s*(?P<subcommand>\w+)?\s*(?P<rest>.*)$", re.IGNORECASE
)

_DAY_TOKENS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
_TIME_RE = re.compile(r"^(?P<start>\d{1,2}:\d{2})-(?P<end>\d{1,2}:\d{2})$")

# Russian-first operator-facing copy, kept with the command (consistent with the
# other bot_gateway slash-command copy).
_CONNECT_INSTRUCTION = (
    "🔗 Чтобы подключить календарь, откройте ссылку и разрешите доступ "
    "(только чтение занятости):\n{consent_url}\n\n"
    "После подтверждения вернитесь в Telegram — доступ заработает автоматически, "
    "и календарь включится для вашего проекта."
)
_CONNECT_FALLBACK = (
    "Не получилось начать подключение календаря — попробуйте чуть позже."
)
_DISCONNECT_CONFIRMATION = (
    "✅ Календарь отключён. Доступ к занятости отозван, сохранённый токен удалён."
)
_DISCONNECT_FALLBACK = (
    "Не получилось отключить календарь — попробуйте чуть позже."
)
_CALENDAR_OFF_CONFIRMATION = (
    "✅ Календарь выключен. Сохранённый токен не удалён — чтобы снова включить, "
    "запустите /connect_calendar."
)
_CALENDAR_FALLBACK = "Не получилось изменить настройки календаря — попробуйте чуть позже."
_SERVICE_USAGE = (
    "Использование:\n"
    "/calendar_service add <название> <минуты> <дни> <часы>\n"
    "например: /calendar_service add маникюр 60 mon-sat 10:00-19:00\n"
    "/calendar_service remove <id>"
)
_SERVICE_ADDED = "✅ Услуга #{rule_id} «{name}» сохранена."
_SERVICE_REMOVED = "✅ Услуга #{rule_id} удалена."
_SERVICE_FALLBACK = "Не получилось сохранить услугу — попробуйте чуть позже."

# --- Story 13.03 /service slash command (canonical FR-24 Path A) -----------
#
# Plain-text Russian copy: NO MarkdownV2 / HTML (matches the NL preview rule —
# prevents preview/echo injection if a service name or description contains
# Telegram-format reserved chars).
_SERVICE_NEW_USAGE = (
    "Использование:\n"
    "/service add <название> [duration=60] [days=mon-sat] [hours=10:00-19:00] "
    "[price=\"от 2000\"] [desc=\"...\"] [tags=...]\n"
    "/service edit <название> [...]\n"
    "/service remove <название>\n"
    "/service list"
)
_SERVICE_NEW_ADDED = "Услуга «{name}» сохранена."
_SERVICE_NEW_SCHEDULING_HINT = (
    " Чтобы сделать её бронируемой, добавьте расписание: "
    "`/service edit <название> duration=60 days=mon-sat hours=10:00-19:00`."
)
_SERVICE_NEW_REMOVED = "Услуга «{name}» удалена."
_SERVICE_NEW_NOT_FOUND = "услуга «{name}» не найдена."
_SERVICE_NEW_ADMIN_CANNOT_REMOVE = (
    "Удаление услуги доступно только оператору, не администратору."
)
_SERVICE_NEW_VALIDATION_FAILED = "услуга «{name}» не сохранена: {reason}."
_SERVICE_NEW_FALLBACK = "Не получилось сохранить услугу — попробуйте чуть позже."
_SERVICE_NEW_LIST_EMPTY = "В вашем проекте пока не настроены услуги."
_SERVICE_NEW_LIST_HEADER = "Услуги проекта:"
_CALENDAR_SERVICE_MIGRATION_HINT = (
    "Команда /calendar_service устарела — используйте /service "
    "или просто напишите «добавь услугу …»."
)

# Cyrillic day-codes accepted as aliases for the ASCII tokens. Mapping is
# applied during ``parse_service_kv`` so the rest of the pipeline stays
# ASCII-only.
_CYRILLIC_DAY_MAP: dict[str, str] = {
    "пн": "mon",
    "вт": "tue",
    "ср": "wed",
    "чт": "thu",
    "пт": "fri",
    "сб": "sat",
    "вс": "sun",
}

# All Cyrillic dash variants ought to normalize to a plain ASCII hyphen so
# ``пн–сб`` / ``пн—сб`` parse as ranges.
_DASH_VARIANTS = ("‐", "‑", "‒", "–", "—", "−")

_KV_ALLOWED_KEYS = {"duration", "days", "hours", "price", "desc", "tags"}
_KV_VALUE_MAX_CHARS = 200


class ServiceKvError(ValueError):
    """Raised by ``parse_service_kv`` for any malformed key=value input."""

    def __init__(self, reason: str, *, key: str | None = None) -> None:
        super().__init__(reason)
        self.reason = reason
        self.key = key


def _normalize_dashes(value: str) -> str:
    out = value
    for variant in _DASH_VARIANTS:
        out = out.replace(variant, "-")
    return out


def _expand_day_range(token: str) -> list[str]:
    """Normalize ``mon-sat``/``пн-сб``/``mon,wed,fri`` to a list of 3-letter codes."""
    token = _normalize_dashes(token).strip().lower()
    if "," in token:
        out: list[str] = []
        for piece in token.split(","):
            piece = piece.strip()
            if not piece:
                continue
            mapped = _CYRILLIC_DAY_MAP.get(piece, piece)
            if mapped not in _DAY_TOKENS:
                raise ServiceKvError("invalid_days", key="days")
            out.append(mapped)
        if not out:
            raise ServiceKvError("invalid_days", key="days")
        return out
    if "-" in token:
        start, _, end = token.partition("-")
        start = _CYRILLIC_DAY_MAP.get(start.strip(), start.strip())
        end = _CYRILLIC_DAY_MAP.get(end.strip(), end.strip())
        if start not in _DAY_TOKENS or end not in _DAY_TOKENS:
            raise ServiceKvError("invalid_days", key="days")
        start_i = _DAY_TOKENS.index(start)
        end_i = _DAY_TOKENS.index(end)
        if start_i > end_i:
            raise ServiceKvError("invalid_days", key="days")
        return list(_DAY_TOKENS[start_i : end_i + 1])
    mapped = _CYRILLIC_DAY_MAP.get(token, token)
    if mapped not in _DAY_TOKENS:
        raise ServiceKvError("invalid_days", key="days")
    return [mapped]


def _parse_hours(value: str) -> list[list[str]]:
    """Parse ``10:00-19:00`` (single) or ``10:00-13:00,14:00-19:00`` (multi)."""
    windows: list[list[str]] = []
    for piece in _normalize_dashes(value).split(","):
        piece = piece.strip()
        if not piece:
            continue
        match = _TIME_RE.match(piece)
        if match is None:
            raise ServiceKvError("invalid_hours", key="hours")
        windows.append([match.group("start"), match.group("end")])
    if not windows:
        raise ServiceKvError("invalid_hours", key="hours")
    return windows


def _tokenize_kv(args: str) -> list[str]:
    """Tokenize ``key=value`` args; supports ``"..."`` / ``'...'`` quoting.

    Quoted values preserve internal whitespace; the surrounding quotes are
    stripped. Unquoted tokens are whitespace-separated.
    """
    tokens: list[str] = []
    buf: list[str] = []
    i = 0
    n = len(args)
    while i < n:
        ch = args[i]
        if ch.isspace():
            if buf:
                tokens.append("".join(buf))
                buf = []
            i += 1
            continue
        if ch in ('"', "'"):
            quote = ch
            i += 1
            while i < n and args[i] != quote:
                buf.append(args[i])
                i += 1
            # Skip closing quote (or end-of-string if user forgot it).
            if i < n:
                i += 1
            continue
        buf.append(ch)
        i += 1
    if buf:
        tokens.append("".join(buf))
    return tokens


def parse_service_kv(args: str) -> tuple[str, dict[str, object]]:
    """Parse ``<name> [key=value ...]`` into ``(name, payload)``.

    Returns the service name (first positional token) and a payload dict
    matching the api's ``ProjectServiceUpsertRequest`` field names: ``duration``
    → ``duration_minutes`` (int), ``days`` → ``service_days`` (list[str]),
    ``hours`` → ``working_hours`` (dict[str, list]), ``price`` → ``price_text``
    (str), ``desc`` → ``description`` (str), ``tags`` → ``tags`` (list[str]).

    Raises ``ServiceKvError`` with a structured ``reason`` for any malformed
    input so the caller can DM a precise Russian message.
    """
    raw_tokens = _tokenize_kv(args)
    name_parts: list[str] = []
    kv_tokens: list[str] = []
    for token in raw_tokens:
        if "=" in token and not name_parts and not kv_tokens:
            # No name yet but already a kv pair — treat as missing name.
            kv_tokens.append(token)
        elif "=" in token:
            kv_tokens.append(token)
        else:
            if kv_tokens:
                # A positional after a kv — treat as part of the value-less
                # tail; reject for clarity.
                raise ServiceKvError("unexpected_positional")
            name_parts.append(token)
    name = " ".join(name_parts).strip()
    if not name:
        raise ServiceKvError("missing_name")
    parsed: dict[str, object] = {}
    for token in kv_tokens:
        key, _, value = token.partition("=")
        key = key.strip().lower()
        if not key or key not in _KV_ALLOWED_KEYS:
            raise ServiceKvError("unknown_key", key=key)
        if len(value) > _KV_VALUE_MAX_CHARS:
            value = value[:_KV_VALUE_MAX_CHARS] + "…"
        if key == "duration":
            if not value.isdigit():
                raise ServiceKvError("invalid_duration", key="duration")
            parsed["duration_minutes"] = int(value)
        elif key == "days":
            days = _expand_day_range(value)
            parsed["service_days"] = days
        elif key == "hours":
            windows = _parse_hours(value)
            parsed["working_hours"] = {}  # filled below once days known
            parsed["__hours_windows__"] = windows
        elif key == "price":
            parsed["price_text"] = value
        elif key == "desc":
            parsed["description"] = value
        elif key == "tags":
            parsed["tags"] = [
                tag.strip() for tag in value.split(",") if tag.strip()
            ]
    # If both days and hours were supplied, build working_hours per day.
    windows = parsed.pop("__hours_windows__", None)
    if windows is not None:
        days_list = parsed.get("service_days") or list(_DAY_TOKENS[:5])  # mon-fri
        # Single window collapses to ``[start, end]`` (matches Epic-11 schema);
        # multi-window stays nested ``[[a, b], [c, d]]``.
        per_day_value: object = windows[0] if len(windows) == 1 else windows
        parsed["working_hours"] = {day: per_day_value for day in days_list}
    return name, parsed


async def handle_calendar_command(
    *,
    normalized: NormalizedTelegramMessage,
    api_client: ApiClient,
    send_dm: SendDmFn,
    internal_token: str,
    nl_ops_db_path: str | None = None,
    now_fn: Callable[[], datetime] | None = None,
) -> dict[str, str] | None:
    """Dispatch `/connect_calendar`, `/disconnect_calendar`, `/calendar_off`,
    `/calendar_service` (alias with migration hint), and `/service` (canonical
    Epic-13 surface).

    Returns None for non-matching messages so the normal routing continues.
    A non-authorized sender (not a registered operator bound to a project) is
    ignored with the logged reason ``unauthorized_calendar`` (legacy commands)
    or ``unauthorized_services`` (the new `/service` command — see story 13.03
    operator-gating rule). ``nl_ops_db_path`` is required when an operator
    invokes `/calendar_service` so the migration-hint DM can be deduped per
    `(project_id, operator)`.
    """
    text = normalized.text or ""
    is_connect = bool(_CONNECT_RE.match(text))
    is_disconnect = bool(_DISCONNECT_RE.match(text))
    is_off = bool(_CALENDAR_OFF_RE.match(text))
    service_match = _CALENDAR_SERVICE_RE.match(text)
    is_service_alias = service_match is not None
    new_service_match = _SERVICE_RE.match(text)
    is_new_service = new_service_match is not None
    if not (
        is_connect
        or is_disconnect
        or is_off
        or is_service_alias
        or is_new_service
    ):
        return None

    if is_connect:
        command = "connect"
    elif is_disconnect:
        command = "disconnect"
    elif is_off:
        command = "calendar_off"
    elif is_new_service:
        command = "service"
    else:
        command = "calendar_service"

    resolved = await resolve_operator_for_sender(
        username=normalized.username,
        api_client=api_client,
    )
    if resolved is None or resolved.project_id is None:
        # The new `/service` command uses a different ignored-reason so log
        # analytics can distinguish it from the legacy commands; the user is
        # NOT DM'd in either case (story 13.03 + Epic-11 rule).
        reason = "unauthorized_services" if is_new_service else "unauthorized_calendar"
        logger.warning(
            "calendar_command_unauthorized",
            extra={
                "username": normalized.username,
                "reason": reason,
                "command": command,
            },
        )
        return {"status": "ignored", "reason": reason}

    if is_connect:
        return await _do_connect(
            normalized=normalized,
            api_client=api_client,
            send_dm=send_dm,
            project_id=resolved.project_id,
            operator=resolved.username,
            internal_token=internal_token,
        )
    if is_disconnect:
        return await _do_disconnect(
            normalized=normalized,
            api_client=api_client,
            send_dm=send_dm,
            project_id=resolved.project_id,
            operator=resolved.username,
            internal_token=internal_token,
        )
    if is_off:
        return await _do_disable(
            normalized=normalized,
            api_client=api_client,
            send_dm=send_dm,
            project_id=resolved.project_id,
            operator=resolved.username,
            internal_token=internal_token,
        )
    if is_new_service:
        assert new_service_match is not None  # narrowed by ``is_new_service``
        subcommand_raw = new_service_match.group("subcommand") or ""
        return await _do_new_service(
            normalized=normalized,
            api_client=api_client,
            send_dm=send_dm,
            project_id=resolved.project_id,
            operator=resolved.username,
            internal_token=internal_token,
            subcommand=subcommand_raw.lower(),
            rest=new_service_match.group("rest").strip(),
        )
    # Legacy `/calendar_service` alias: emit a deprecation log on EVERY call;
    # DM a one-time migration hint per ``(project_id, operator)``; then
    # delegate to the existing handler so the operator's intended action
    # still completes.
    assert service_match is not None
    logger.info(
        "deprecation_warning_calendar_service_command",
        extra={
            "operator": resolved.username,
            "project_id": resolved.project_id,
        },
    )
    if nl_ops_db_path is not None:
        clock = now_fn or (lambda: datetime.now(UTC))
        try:
            should_dm = should_send_calendar_service_alias_hint(
                db_path=nl_ops_db_path,
                project_id=resolved.project_id,
                operator=resolved.username,
                now=clock(),
            )
        except Exception:  # broad: dedup is best-effort; never block the action
            logger.warning(
                "calendar_service_alias_hint_dedup_failed",
                extra={
                    "operator": resolved.username,
                    "project_id": resolved.project_id,
                },
                exc_info=True,
            )
            should_dm = False
        if should_dm:
            await send_dm(normalized.chat_id, _CALENDAR_SERVICE_MIGRATION_HINT)
    return await _do_service(
        normalized=normalized,
        api_client=api_client,
        send_dm=send_dm,
        project_id=resolved.project_id,
        operator=resolved.username,
        internal_token=internal_token,
        rest=service_match.group("rest").strip(),
    )


async def _do_connect(
    *,
    normalized: NormalizedTelegramMessage,
    api_client: ApiClient,
    send_dm: SendDmFn,
    project_id: int,
    operator: str,
    internal_token: str,
) -> dict[str, str]:
    try:
        result = await api_client.initiate_calendar_connect(
            project_id=project_id,
            operator=operator,
            internal_token=internal_token,
        )
    except (httpx.HTTPStatusError, httpx.RequestError):
        logger.warning(
            "calendar_connect_initiate_failed",
            extra={"project_id": project_id, "operator": operator},
        )
        await send_dm(normalized.chat_id, _CONNECT_FALLBACK)
        return {"status": "accepted", "route": "calendar_connect", "decision": "api_error"}

    consent_url = str(result.get("consent_url") or "")
    if not consent_url:
        logger.warning(
            "calendar_connect_missing_url",
            extra={"project_id": project_id, "operator": operator},
        )
        await send_dm(normalized.chat_id, _CONNECT_FALLBACK)
        return {"status": "accepted", "route": "calendar_connect", "decision": "no_url"}

    # NB: log success WITHOUT the consent URL — it carries a single-use state.
    logger.info(
        "calendar_connect_url_sent",
        extra={"project_id": project_id, "operator": operator},
    )
    await send_dm(normalized.chat_id, _CONNECT_INSTRUCTION.format(consent_url=consent_url))
    return {"status": "accepted", "route": "calendar_connect", "decision": "url_sent"}


async def _do_disconnect(
    *,
    normalized: NormalizedTelegramMessage,
    api_client: ApiClient,
    send_dm: SendDmFn,
    project_id: int,
    operator: str,
    internal_token: str,
) -> dict[str, str]:
    try:
        await api_client.disconnect_calendar(
            project_id=project_id,
            operator=operator,
            internal_token=internal_token,
        )
    except (httpx.HTTPStatusError, httpx.RequestError):
        logger.warning(
            "calendar_disconnect_failed",
            extra={"project_id": project_id, "operator": operator},
        )
        await send_dm(normalized.chat_id, _DISCONNECT_FALLBACK)
        return {
            "status": "accepted",
            "route": "calendar_disconnect",
            "decision": "api_error",
        }

    logger.info(
        "calendar_disconnected",
        extra={"project_id": project_id, "operator": operator},
    )
    await send_dm(normalized.chat_id, _DISCONNECT_CONFIRMATION)
    return {
        "status": "accepted",
        "route": "calendar_disconnect",
        "decision": "disconnected",
    }


async def _do_disable(
    *,
    normalized: NormalizedTelegramMessage,
    api_client: ApiClient,
    send_dm: SendDmFn,
    project_id: int,
    operator: str,
    internal_token: str,
) -> dict[str, str]:
    try:
        await api_client.calendar_disable(
            project_id=project_id,
            actor=operator,
            actor_role="operator",
            internal_token=internal_token,
        )
    except (httpx.HTTPStatusError, httpx.RequestError):
        logger.warning(
            "calendar_disable_failed",
            extra={"project_id": project_id, "operator": operator},
        )
        await send_dm(normalized.chat_id, _CALENDAR_FALLBACK)
        return {"status": "accepted", "route": "calendar_off", "decision": "api_error"}
    logger.info(
        "calendar_disabled",
        extra={"project_id": project_id, "operator": operator},
    )
    await send_dm(normalized.chat_id, _CALENDAR_OFF_CONFIRMATION)
    return {"status": "accepted", "route": "calendar_off", "decision": "disabled"}


def _parse_day_range(token: str) -> list[str] | None:
    """Parse ``mon-sat`` or a single ``mon`` into a list of weekday tokens."""
    token = token.lower()
    if "-" in token:
        start, _, end = token.partition("-")
        if start not in _DAY_TOKENS or end not in _DAY_TOKENS:
            return None
        start_i = _DAY_TOKENS.index(start)
        end_i = _DAY_TOKENS.index(end)
        if start_i > end_i:
            return None
        return list(_DAY_TOKENS[start_i : end_i + 1])
    if token not in _DAY_TOKENS:
        return None
    return [token]


def parse_service_add(rest: str) -> dict[str, object] | None:
    """Parse ``add <name> [<minutes> [<days> [<hours>]]]`` into repository kwargs.

    Name is the only required positional after ``add``; missing trailing fields
    default to ``None`` so the service is created as a catalog-only entry.
    Returns None on any malformed input so the caller can show usage help.
    """
    parts = rest.split()
    if len(parts) < 2 or parts[0].lower() != "add":
        return None
    name = parts[1]
    duration: int | None = None
    days: list[str] | None = None
    working_hours: dict[str, list[str]] | None = None
    if len(parts) >= 3:
        if not parts[2].isdigit():
            return None
        duration = int(parts[2])
        if duration <= 0:
            return None
    if len(parts) >= 4:
        days = _parse_day_range(parts[3])
        if days is None:
            return None
    if len(parts) >= 5:
        time_match = _TIME_RE.match(parts[4])
        if time_match is None:
            return None
        start = time_match.group("start")
        end = time_match.group("end")
        assert days is not None  # narrowed by len(parts) >= 4 above
        working_hours = {day: [start, end] for day in days}
    return {
        "name": name,
        "duration_minutes": duration,
        "service_days": days,
        "working_hours": working_hours,
    }


async def _do_service(
    *,
    normalized: NormalizedTelegramMessage,
    api_client: ApiClient,
    send_dm: SendDmFn,
    project_id: int,
    operator: str,
    internal_token: str,
    rest: str,
) -> dict[str, str]:
    parts = rest.split()
    action = parts[0].lower() if parts else ""
    if action == "remove" and len(parts) == 2 and parts[1].isdigit():
        return await _do_service_remove(
            normalized=normalized,
            api_client=api_client,
            send_dm=send_dm,
            project_id=project_id,
            operator=operator,
            internal_token=internal_token,
            rule_id=int(parts[1]),
        )
    if action == "add":
        parsed = parse_service_add(rest)
        if parsed is not None:
            return await _do_service_add(
                normalized=normalized,
                api_client=api_client,
                send_dm=send_dm,
                project_id=project_id,
                operator=operator,
                internal_token=internal_token,
                parsed=parsed,
            )
    await send_dm(normalized.chat_id, _SERVICE_USAGE)
    return {"status": "ignored", "route": "calendar_service", "reason": "usage"}


async def _do_service_add(
    *,
    normalized: NormalizedTelegramMessage,
    api_client: ApiClient,
    send_dm: SendDmFn,
    project_id: int,
    operator: str,
    internal_token: str,
    parsed: dict[str, object],
) -> dict[str, str]:
    try:
        result = await api_client.calendar_upsert_service(
            project_id=project_id,
            actor=operator,
            actor_role="operator",
            internal_token=internal_token,
            name=parsed["name"],
            duration_minutes=parsed["duration_minutes"],
            working_hours=parsed["working_hours"],
            service_days=parsed["service_days"],
        )
    except (httpx.HTTPStatusError, httpx.RequestError):
        logger.warning(
            "calendar_service_add_failed",
            extra={"project_id": project_id, "operator": operator},
        )
        await send_dm(normalized.chat_id, _SERVICE_FALLBACK)
        return {"status": "accepted", "route": "calendar_service", "decision": "api_error"}
    rule_id = result.get("id")
    await send_dm(
        normalized.chat_id,
        _SERVICE_ADDED.format(rule_id=rule_id, name=parsed["name"]),
    )
    return {"status": "accepted", "route": "calendar_service", "decision": "added"}


async def _do_service_remove(
    *,
    normalized: NormalizedTelegramMessage,
    api_client: ApiClient,
    send_dm: SendDmFn,
    project_id: int,
    operator: str,
    internal_token: str,
    rule_id: int,
) -> dict[str, str]:
    try:
        await api_client.calendar_delete_service(
            project_id=project_id,
            rule_id=rule_id,
            actor=operator,
            actor_role="operator",
            internal_token=internal_token,
        )
    except (httpx.HTTPStatusError, httpx.RequestError):
        logger.warning(
            "calendar_service_remove_failed",
            extra={"project_id": project_id, "operator": operator},
        )
        await send_dm(normalized.chat_id, _SERVICE_FALLBACK)
        return {"status": "accepted", "route": "calendar_service", "decision": "api_error"}
    await send_dm(normalized.chat_id, _SERVICE_REMOVED.format(rule_id=rule_id))
    return {"status": "accepted", "route": "calendar_service", "decision": "removed"}


# --- Story 13.03 /service dispatch + helpers --------------------------------


async def _do_new_service(
    *,
    normalized: NormalizedTelegramMessage,
    api_client: ApiClient,
    send_dm: SendDmFn,
    project_id: int,
    operator: str,
    internal_token: str,
    subcommand: str,
    rest: str,
) -> dict[str, str]:
    """Dispatch a `/service` subcommand to the right handler."""
    if subcommand in ("add", "edit"):
        return await _do_new_service_upsert(
            normalized=normalized,
            api_client=api_client,
            send_dm=send_dm,
            project_id=project_id,
            operator=operator,
            internal_token=internal_token,
            rest=rest,
            edit_mode=(subcommand == "edit"),
        )
    if subcommand == "remove":
        return await _do_new_service_remove(
            normalized=normalized,
            api_client=api_client,
            send_dm=send_dm,
            project_id=project_id,
            operator=operator,
            internal_token=internal_token,
            rest=rest,
        )
    if subcommand == "list":
        return await _do_new_service_list(
            normalized=normalized,
            api_client=api_client,
            send_dm=send_dm,
            project_id=project_id,
            internal_token=internal_token,
        )
    await send_dm(normalized.chat_id, _SERVICE_NEW_USAGE)
    return {"status": "ignored", "route": "service", "reason": "usage"}


def _format_service_list_line(service: dict) -> str:
    """Render one row of the `/service list` DM (plain text, no MarkdownV2)."""
    name = service.get("name") or "?"
    parts: list[str] = []
    duration = service.get("duration_minutes")
    if isinstance(duration, int):
        parts.append(f"{duration} мин")
    days = service.get("service_days")
    if isinstance(days, list) and days:
        parts.append("-".join(str(d) for d in days))
    hours = service.get("working_hours")
    if isinstance(hours, dict) and hours:
        # Pick the first day's window as a representative summary; per-day
        # windows would bloat the DM.
        first_value = next(iter(hours.values()))
        if isinstance(first_value, list) and len(first_value) == 2 and all(
            isinstance(v, str) for v in first_value
        ):
            parts.append(f"{first_value[0]}-{first_value[1]}")
    price = service.get("price_text")
    if isinstance(price, str) and price:
        parts.append(f"{price} ₽")
    suffix = (" — " + ", ".join(parts)) if parts else ""
    return f"• {name}{suffix}"


async def _do_new_service_upsert(
    *,
    normalized: NormalizedTelegramMessage,
    api_client: ApiClient,
    send_dm: SendDmFn,
    project_id: int,
    operator: str,
    internal_token: str,
    rest: str,
    edit_mode: bool,
) -> dict[str, str]:
    try:
        name, payload = parse_service_kv(rest)
    except ServiceKvError as exc:
        await send_dm(
            normalized.chat_id,
            _SERVICE_NEW_VALIDATION_FAILED.format(name="", reason=exc.reason),
        )
        return {
            "status": "ignored",
            "route": "service",
            "reason": exc.reason,
            "decision": "kv_parse_failed",
        }
    payload["name"] = name
    try:
        result = await api_client.upsert_project_service(
            project_id=project_id,
            payload=payload,
            actor=operator,
            actor_role="operator",
            internal_token=internal_token,
        )
    except ApiError as exc:
        if exc.response.status_code == 400:
            reason = exc.detail or "validation_failed"
            await send_dm(
                normalized.chat_id,
                _SERVICE_NEW_VALIDATION_FAILED.format(name=name, reason=reason),
            )
            return {
                "status": "ignored",
                "route": "service",
                "reason": reason,
                "decision": "validation_failed",
            }
        logger.warning(
            "service_upsert_failed",
            extra={"project_id": project_id, "operator": operator},
        )
        await send_dm(normalized.chat_id, _SERVICE_NEW_FALLBACK)
        return {"status": "accepted", "route": "service", "decision": "api_error"}
    except (httpx.HTTPStatusError, httpx.RequestError):
        logger.warning(
            "service_upsert_failed",
            extra={"project_id": project_id, "operator": operator},
        )
        await send_dm(normalized.chat_id, _SERVICE_NEW_FALLBACK)
        return {"status": "accepted", "route": "service", "decision": "api_error"}
    saved_name = result.get("name") if isinstance(result, dict) else None
    display_name = str(saved_name) if saved_name else name
    saved_duration = (
        result.get("duration_minutes") if isinstance(result, dict) else None
    )
    success_text = _SERVICE_NEW_ADDED.format(name=display_name)
    if saved_duration is None:
        success_text += _SERVICE_NEW_SCHEDULING_HINT
    await send_dm(normalized.chat_id, success_text)
    decision = "edited" if edit_mode else "added"
    return {"status": "accepted", "route": "service", "decision": decision}


async def _do_new_service_remove(
    *,
    normalized: NormalizedTelegramMessage,
    api_client: ApiClient,
    send_dm: SendDmFn,
    project_id: int,
    operator: str,
    internal_token: str,
    rest: str,
) -> dict[str, str]:
    name = rest.strip()
    if not name:
        await send_dm(normalized.chat_id, _SERVICE_NEW_USAGE)
        return {"status": "ignored", "route": "service", "reason": "usage"}
    try:
        listing = await api_client.list_project_services(
            project_id=project_id, internal_token=internal_token
        )
    except (ApiError, httpx.HTTPStatusError, httpx.RequestError):
        logger.warning(
            "service_remove_list_failed",
            extra={"project_id": project_id, "operator": operator},
        )
        await send_dm(normalized.chat_id, _SERVICE_NEW_FALLBACK)
        return {"status": "accepted", "route": "service", "decision": "api_error"}
    services = listing.get("services") or [] if isinstance(listing, dict) else []
    target_id: int | None = None
    for service in services:
        if not isinstance(service, dict):
            continue
        candidate_name = service.get("name")
        if isinstance(candidate_name, str) and candidate_name.casefold() == name.casefold():
            raw_id = service.get("id")
            if isinstance(raw_id, int):
                target_id = raw_id
                break
    if target_id is None:
        await send_dm(
            normalized.chat_id, _SERVICE_NEW_NOT_FOUND.format(name=name)
        )
        return {
            "status": "ignored",
            "route": "service",
            "reason": "not_found",
            "decision": "not_found",
        }
    try:
        await api_client.delete_project_service(
            project_id=project_id,
            service_id=target_id,
            actor=operator,
            actor_role="operator",
            internal_token=internal_token,
        )
    except ApiError as exc:
        if exc.response.status_code == 403:
            await send_dm(
                normalized.chat_id, _SERVICE_NEW_ADMIN_CANNOT_REMOVE
            )
            return {
                "status": "ignored",
                "route": "service",
                "reason": "admin_cannot_remove_service",
                "decision": "forbidden",
            }
        if exc.response.status_code == 404:
            await send_dm(
                normalized.chat_id, _SERVICE_NEW_NOT_FOUND.format(name=name)
            )
            return {
                "status": "ignored",
                "route": "service",
                "reason": "not_found",
                "decision": "not_found",
            }
        logger.warning(
            "service_remove_failed",
            extra={"project_id": project_id, "operator": operator},
        )
        await send_dm(normalized.chat_id, _SERVICE_NEW_FALLBACK)
        return {"status": "accepted", "route": "service", "decision": "api_error"}
    except (httpx.HTTPStatusError, httpx.RequestError):
        logger.warning(
            "service_remove_failed",
            extra={"project_id": project_id, "operator": operator},
        )
        await send_dm(normalized.chat_id, _SERVICE_NEW_FALLBACK)
        return {"status": "accepted", "route": "service", "decision": "api_error"}
    await send_dm(
        normalized.chat_id, _SERVICE_NEW_REMOVED.format(name=name)
    )
    return {"status": "accepted", "route": "service", "decision": "removed"}


async def _do_new_service_list(
    *,
    normalized: NormalizedTelegramMessage,
    api_client: ApiClient,
    send_dm: SendDmFn,
    project_id: int,
    internal_token: str,
) -> dict[str, str]:
    try:
        listing = await api_client.list_project_services(
            project_id=project_id, internal_token=internal_token
        )
    except (ApiError, httpx.HTTPStatusError, httpx.RequestError):
        logger.warning(
            "service_list_failed",
            extra={"project_id": project_id},
        )
        await send_dm(normalized.chat_id, _SERVICE_NEW_FALLBACK)
        return {"status": "accepted", "route": "service", "decision": "api_error"}
    services = listing.get("services") or [] if isinstance(listing, dict) else []
    if not services:
        await send_dm(normalized.chat_id, _SERVICE_NEW_LIST_EMPTY)
        return {"status": "accepted", "route": "service", "decision": "list_empty"}
    lines = [_SERVICE_NEW_LIST_HEADER]
    for service in services:
        if isinstance(service, dict):
            lines.append(_format_service_list_line(service))
    await send_dm(normalized.chat_id, "\n".join(lines))
    return {"status": "accepted", "route": "service", "decision": "listed"}

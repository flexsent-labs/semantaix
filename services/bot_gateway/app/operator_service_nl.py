"""Operator natural-language service-management dialog (Story 12.02b).

Drop-in fallback for ``/service_add`` / ``/service_remove`` / ``/service_list``
that lets an authorized operator manage the services catalog from Telegram
without memorizing slash commands. Free-text DMs like
``"добавь услугу Медовеевка Лайт"`` or ``"какие у нас услуги?"`` are mapped
to one of four canonical actions by a single OpenRouter ``complete_json``
call, then routed through the SAME ``ApiClient.add_sales_service`` /
``delete_sales_service`` / ``list_sales_services`` calls + the SAME
``Добавлено: …`` / ``Удалено: id=…`` / list-view DM the slash dispatcher
emits.

Design seams:

- The LLM classifier (``classify_service_intent``) is module-level and
  takes the OpenRouter client by parameter so the unit tests can inject a
  ``FakeOpenRouter`` without touching network code.
- The dispatch entry (``handle_operator_service_nl_message``) gates on the
  operator registry BEFORE calling the LLM — an unauthorized sender must
  never burn LLM tokens (cost control).
- Slash-prefixed text short-circuits at the top of the dispatcher even if
  the caller wired this in front of the slash dispatcher; the regression
  guard test asserts the LLM client sees zero invocations for ``/service_*``.
- ``describe`` is sugar over (delete + add) since Story 12.02 explicitly
  disallows in-place edit; the reply text differs (``Обновлено: …``) so the
  operator gets the right cognitive feedback.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, Literal, Protocol

import httpx

from services.api.app.openrouter_client import OpenRouterJsonSchemaViolation
from services.bot_gateway.app.api_client import ApiClient, ApiError
from services.bot_gateway.app.operator_resolver import (
    ResolvedOperator,
    resolve_operator_for_sender,
)
from services.bot_gateway.app.telegram_update import NormalizedTelegramMessage

logger = logging.getLogger(__name__)


SendDmFn = Callable[[int, str], Awaitable[Any]]


# Mirror of the slash-command empty-list and unreachable-api hints so the NL
# path and the slash path render identical text — DRY across both surfaces.
_EMPTY_LIST_HINT = (
    "Услуг пока нет. Добавьте первую через /service_add <название>."
)
_API_UNAVAILABLE = "Сервис временно недоступен, попробуйте позже."
_DESCRIBE_USAGE = "Использование: опиши <услугу> как <описание>"

# Cost cap: a long pasted message would otherwise be sent verbatim to
# OpenRouter and bill the project for an open-ended number of tokens. 500
# chars covers every reasonable NL phrasing of the four supported intents.
_MAX_INPUT_CHARS = 500

ACTION_ADD: Literal["add"] = "add"
ACTION_REMOVE: Literal["remove"] = "remove"
ACTION_LIST: Literal["list"] = "list"
ACTION_DESCRIBE: Literal["describe"] = "describe"

_VALID_ACTIONS = (ACTION_ADD, ACTION_REMOVE, ACTION_LIST, ACTION_DESCRIBE)
_ACTIONS_REQUIRING_NAME = (ACTION_ADD, ACTION_REMOVE, ACTION_DESCRIBE)


# System prompt — Russian-first, utilitarian, NO persona voice. The prompt
# explicitly tells the LLM to set ``action: null`` when uncertain so the
# classifier can fall through to the rest of the inbound pipeline.
_SYSTEM_PROMPT = """\
Ты — классификатор операторских сообщений в Telegram-боте. Оператор управляет \
каталогом услуг компании. Твоя задача — определить, что хочет сделать оператор, \
и вернуть СТРОГО JSON с одним из четырёх действий:

- "add"      — добавить услугу. Поля: name (название услуги, строка), \
description (описание, строка или null).
- "remove"   — удалить услугу. Поля: name (название удаляемой услуги, строка), \
description: null.
- "list"     — показать список услуг. Поля: name: null, description: null.
- "describe" — переописать существующую услугу (sugar над удалить+добавить). \
Поля: name (название), description (новое описание, строка).

Если сообщение НЕ относится к управлению каталогом услуг (например, "привет", \
"послушай", обычный чат), верни {"action": null}. Лучше вернуть null, чем \
ошибиться: классификация некачественного сигнала пропускает сообщение дальше \
по обычному пайплайну.

Примеры:

Вход: "добавь услугу Медовеевка Лайт"
Выход: {"action": "add", "name": "Медовеевка Лайт", "description": null}

Вход: "добавь услугу каньонинг — спуск по верёвке"
Выход: {"action": "add", "name": "каньонинг", "description": "спуск по верёвке"}

Вход: "удали услугу каньонинг"
Выход: {"action": "remove", "name": "каньонинг", "description": null}

Вход: "какие у нас услуги?"
Выход: {"action": "list", "name": null, "description": null}

Вход: "список услуг"
Выход: {"action": "list", "name": null, "description": null}

Вход: "опиши каньонинг как спуск по верёвке"
Выход: {"action": "describe", "name": "каньонинг", "description": "спуск по верёвке"}

Вход: "послушай"
Выход: {"action": null}

Отвечай ТОЛЬКО валидным JSON-объектом, без markdown, без комментариев.
"""


class _OpenRouterClient(Protocol):
    async def complete_json(
        self, *, system: str, user: str, model: str | None = None
    ) -> dict[str, Any]: ...


@dataclass(frozen=True)
class ServiceIntent:
    action: Literal["add", "remove", "list", "describe"]
    name: str | None = None
    description: str | None = None


def _log_schema_violation(*, reason: str, **extra: Any) -> None:
    payload: dict[str, Any] = {"reason": reason}
    payload.update(extra)
    logger.warning("operator_service_nl_schema_violation", extra=payload)


async def classify_service_intent(
    text: str,
    *,
    openrouter: _OpenRouterClient,
) -> ServiceIntent | None:
    """Map an operator free-text DM to one of four service-management intents.

    Returns ``None`` (fall-through signal for the dispatcher) on:

    - LLM ``OpenRouterJsonSchemaViolation`` (non-JSON / non-object response),
    - explicit ``action: null`` from the LLM (uncertain classification),
    - unknown action string,
    - missing required ``name`` for an action that needs one,
    - wrong type on ``name`` / ``description``.

    Always logs a structured ``operator_service_nl_schema_violation`` event
    when a schema-shape problem (not ``action: null``, which is intentional)
    is detected, so on-call can tune the prompt without re-shipping code.
    """
    truncated = text[:_MAX_INPUT_CHARS]
    try:
        result = await openrouter.complete_json(
            system=_SYSTEM_PROMPT, user=truncated
        )
    except OpenRouterJsonSchemaViolation as exc:
        _log_schema_violation(reason="non_json", error=str(exc))
        return None
    except (httpx.HTTPError, RuntimeError) as exc:
        # Misconfigured key, transport error, or any other LLM-call failure
        # must NOT 5xx the Telegram webhook (Telegram would retry → duplicate
        # operator acks). Log and treat as "no classification" so the rest of
        # the inbound pipeline gets the message.
        logger.warning(
            "operator_service_nl_llm_error",
            extra={"error": str(exc), "error_type": type(exc).__name__},
        )
        return None
    action = result.get("action")
    if action is None:
        return None
    if action not in _VALID_ACTIONS:
        _log_schema_violation(reason="bad_action", action=str(action))
        return None
    name = result.get("name")
    description = result.get("description")
    if name is not None and not isinstance(name, str):
        _log_schema_violation(reason="bad_name_type", action=action)
        return None
    if description is not None and not isinstance(description, str):
        _log_schema_violation(reason="bad_description_type", action=action)
        return None
    if action in _ACTIONS_REQUIRING_NAME and not name:
        _log_schema_violation(reason="missing_name", action=action)
        return None
    return ServiceIntent(action=action, name=name, description=description)


def _is_slash_prefixed(text: str) -> bool:
    return text.lstrip().startswith("/")


# Cheap pre-filter: only invoke the LLM (and the upstream operator-registry
# auth check, which costs a network round-trip) when the message *could* be
# about services. Catches every Russian inflection of the noun (услуга /
# услуги / услугу / услуг / услуге / услугам / услугами / услугах) plus the
# imperative verbs used in the ``describe`` prompt examples (опиши / опишите).
# Operator chatter / HITL replies / customer-shaped messages skip this and
# fall through silently — both a cost control AND a fix for an interaction
# with FastAPI's TestClient where the otherwise-unconditional auth roundtrip
# de-syncs pytest-cov's tracer for the rest of the webhook function.
_SERVICE_HINT_RE = re.compile(
    r"услуг|опиши(те)?\b",
    re.IGNORECASE,
)


def _looks_like_service_intent(text: str) -> bool:
    return _SERVICE_HINT_RE.search(text) is not None


def _render_service_row(service: dict) -> str:
    """Mirror of ``sales_command_dispatch._render_service_row`` so the NL list
    view renders identically to the slash list view."""
    name = str(service.get("name", "")).strip() or "?"
    description = (service.get("description_md") or "").strip()
    sid = service.get("id", "?")
    if description:
        return f"{sid}. {name} — {description}"
    return f"{sid}. {name}"


async def _resolve_operator_or_ignore(
    *,
    normalized: NormalizedTelegramMessage,
    api_client: ApiClient,
    primary_operator_username: str,
    admin_username: str,
    trace_id: str,
) -> ResolvedOperator | None:
    """Return the resolved operator OR ``None`` after emitting the unauthorized
    log line. Mirrors the slash-command dispatcher's authorization gate."""
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
            "unauthorized_service_nl",
            extra={
                "trace_id": trace_id,
                "from_username": normalized.username,
            },
        )
        return None
    if resolved is None or resolved.project_id is None:
        logger.warning(
            "unauthorized_service_nl",
            extra={
                "trace_id": trace_id,
                "from_username": normalized.username,
                "note": "admin_has_no_project_mapping",
            },
        )
        return None
    return resolved


def _log_action_taken(
    *,
    trace_id: str,
    action: str,
    service_id: int | None,
    from_username: str | None,
) -> None:
    logger.info(
        "operator_service_nl_action_taken",
        extra={
            "trace_id": trace_id,
            "action": action,
            "service_id": service_id,
            "from_username": from_username,
        },
    )


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
        "operator_service_nl_api_error",
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


# --- Per-action handlers ----------------------------------------------------


async def _handle_add(
    *,
    normalized: NormalizedTelegramMessage,
    resolved: ResolvedOperator,
    intent: ServiceIntent,
    api_client: ApiClient,
    send_dm: SendDmFn,
    internal_token: str,
    trace_id: str,
) -> dict[str, str]:
    name = intent.name or ""
    try:
        body = await api_client.add_sales_service(
            project_id=int(resolved.project_id or 0),
            name=name,
            description_md=intent.description,
            tags=None,
            internal_token=internal_token,
        )
    except ApiError as exc:
        if exc.detail == "service_already_exists":
            await send_dm(
                normalized.chat_id, f"Такая услуга уже есть: {name}."
            )
            return {
                "status": "error",
                "route": "service_add",
                "decision": "duplicate",
            }
        await _log_api_error_and_dm(
            send_dm=send_dm,
            normalized=normalized,
            trace_id=trace_id,
            route="service_add",
            exc=exc,
        )
        return {
            "status": "error",
            "route": "service_add",
            "detail": exc.detail or "",
        }
    except (httpx.HTTPStatusError, httpx.RequestError, OSError) as exc:
        await _log_api_error_and_dm(
            send_dm=send_dm,
            normalized=normalized,
            trace_id=trace_id,
            route="service_add",
            exc=exc,
        )
        return {"status": "error", "route": "service_add"}
    service_id = int(body.get("id", 0))
    await send_dm(
        normalized.chat_id, f"Добавлено: {name} (id={service_id})"
    )
    _log_action_taken(
        trace_id=trace_id,
        action=ACTION_ADD,
        service_id=service_id,
        from_username=normalized.username,
    )
    return {
        "status": "ok",
        "route": "service_add",
        "service_id": str(service_id),
    }


async def _list_services_or_error(
    *,
    resolved: ResolvedOperator,
    api_client: ApiClient,
    internal_token: str,
) -> list[dict]:
    body = await api_client.list_sales_services(
        project_id=int(resolved.project_id or 0),
        internal_token=internal_token,
    )
    return list(body.get("services") or [])


async def _handle_list(
    *,
    normalized: NormalizedTelegramMessage,
    resolved: ResolvedOperator,
    api_client: ApiClient,
    send_dm: SendDmFn,
    internal_token: str,
    trace_id: str,
) -> dict[str, str]:
    try:
        services = await _list_services_or_error(
            resolved=resolved,
            api_client=api_client,
            internal_token=internal_token,
        )
    except (ApiError, httpx.HTTPStatusError, httpx.RequestError, OSError) as exc:
        await _log_api_error_and_dm(
            send_dm=send_dm,
            normalized=normalized,
            trace_id=trace_id,
            route="service_list",
            exc=exc,
        )
        return {"status": "error", "route": "service_list"}
    if not services:
        await send_dm(normalized.chat_id, _EMPTY_LIST_HINT)
        _log_action_taken(
            trace_id=trace_id,
            action=ACTION_LIST,
            service_id=None,
            from_username=normalized.username,
        )
        return {
            "status": "ok",
            "route": "service_list",
            "decision": "empty",
        }
    lines = [_render_service_row(s) for s in services]
    await send_dm(normalized.chat_id, "\n".join(lines))
    _log_action_taken(
        trace_id=trace_id,
        action=ACTION_LIST,
        service_id=None,
        from_username=normalized.username,
    )
    return {
        "status": "ok",
        "route": "service_list",
        "count": str(len(services)),
    }


def _find_service_by_name(services: list[dict], name: str) -> dict | None:
    """Case-insensitive name match against the project's active services."""
    target = name.strip().casefold()
    for s in services:
        candidate = str(s.get("name", "")).strip().casefold()
        if candidate == target:
            return s
    return None


async def _handle_remove(
    *,
    normalized: NormalizedTelegramMessage,
    resolved: ResolvedOperator,
    intent: ServiceIntent,
    api_client: ApiClient,
    send_dm: SendDmFn,
    internal_token: str,
    trace_id: str,
) -> dict[str, str]:
    name = intent.name or ""
    try:
        services = await _list_services_or_error(
            resolved=resolved,
            api_client=api_client,
            internal_token=internal_token,
        )
    except (ApiError, httpx.HTTPStatusError, httpx.RequestError, OSError) as exc:
        await _log_api_error_and_dm(
            send_dm=send_dm,
            normalized=normalized,
            trace_id=trace_id,
            route="service_remove",
            exc=exc,
        )
        return {"status": "error", "route": "service_remove"}
    match = _find_service_by_name(services, name)
    if match is None:
        await send_dm(normalized.chat_id, f"Не найдено: {name}")
        return {
            "status": "error",
            "route": "service_remove",
            "decision": "not_found",
        }
    service_id = int(match.get("id", 0))
    try:
        await api_client.delete_sales_service(
            service_id=service_id, internal_token=internal_token
        )
    except ApiError as exc:
        if exc.detail == "service_not_found":
            await send_dm(normalized.chat_id, f"Не найдено: id={service_id}")
            return {
                "status": "error",
                "route": "service_remove",
                "decision": "not_found",
            }
        await _log_api_error_and_dm(
            send_dm=send_dm,
            normalized=normalized,
            trace_id=trace_id,
            route="service_remove",
            exc=exc,
        )
        return {
            "status": "error",
            "route": "service_remove",
            "detail": exc.detail or "",
        }
    except (httpx.HTTPStatusError, httpx.RequestError, OSError) as exc:
        await _log_api_error_and_dm(
            send_dm=send_dm,
            normalized=normalized,
            trace_id=trace_id,
            route="service_remove",
            exc=exc,
        )
        return {"status": "error", "route": "service_remove"}
    await send_dm(normalized.chat_id, f"Удалено: id={service_id}")
    _log_action_taken(
        trace_id=trace_id,
        action=ACTION_REMOVE,
        service_id=service_id,
        from_username=normalized.username,
    )
    return {
        "status": "ok",
        "route": "service_remove",
        "service_id": str(service_id),
    }


async def _handle_describe(
    *,
    normalized: NormalizedTelegramMessage,
    resolved: ResolvedOperator,
    intent: ServiceIntent,
    api_client: ApiClient,
    send_dm: SendDmFn,
    internal_token: str,
    trace_id: str,
) -> dict[str, str]:
    name = intent.name or ""
    description = intent.description
    if not description:
        await send_dm(normalized.chat_id, _DESCRIBE_USAGE)
        return {
            "status": "error",
            "route": "service_describe",
            "decision": "missing_description",
        }
    try:
        services = await _list_services_or_error(
            resolved=resolved,
            api_client=api_client,
            internal_token=internal_token,
        )
    except (ApiError, httpx.HTTPStatusError, httpx.RequestError, OSError) as exc:
        await _log_api_error_and_dm(
            send_dm=send_dm,
            normalized=normalized,
            trace_id=trace_id,
            route="service_describe",
            exc=exc,
        )
        return {"status": "error", "route": "service_describe"}
    match = _find_service_by_name(services, name)
    if match is None:
        await send_dm(normalized.chat_id, f"Не найдено: {name}")
        return {
            "status": "error",
            "route": "service_describe",
            "decision": "not_found",
        }
    old_id = int(match.get("id", 0))
    try:
        await api_client.delete_sales_service(
            service_id=old_id, internal_token=internal_token
        )
    except (ApiError, httpx.HTTPStatusError, httpx.RequestError, OSError) as exc:
        await _log_api_error_and_dm(
            send_dm=send_dm,
            normalized=normalized,
            trace_id=trace_id,
            route="service_describe",
            exc=exc,
        )
        return {"status": "error", "route": "service_describe"}
    try:
        body = await api_client.add_sales_service(
            project_id=int(resolved.project_id or 0),
            name=name,
            description_md=description,
            tags=None,
            internal_token=internal_token,
        )
    except (ApiError, httpx.HTTPStatusError, httpx.RequestError, OSError) as exc:
        await _log_api_error_and_dm(
            send_dm=send_dm,
            normalized=normalized,
            trace_id=trace_id,
            route="service_describe",
            exc=exc,
        )
        return {"status": "error", "route": "service_describe"}
    new_id = int(body.get("id", 0))
    await send_dm(normalized.chat_id, f"Обновлено: {name} (id={new_id})")
    _log_action_taken(
        trace_id=trace_id,
        action=ACTION_DESCRIBE,
        service_id=new_id,
        from_username=normalized.username,
    )
    return {
        "status": "ok",
        "route": "service_describe",
        "service_id": str(new_id),
    }


# --- Top-level dispatch ----------------------------------------------------


async def handle_operator_service_nl_message(
    *,
    normalized: NormalizedTelegramMessage,
    api_client: ApiClient,
    send_dm: SendDmFn,
    openrouter: _OpenRouterClient,
    primary_operator_username: str,
    admin_username: str,
    internal_token: str,
) -> dict[str, str] | None:
    """Top-level NL dispatcher. Returns ``None`` to signal fall-through.

    Order of checks:

    1. Empty / slash-prefixed text → return ``None`` (no LLM call).
    2. Resolve operator via the registry; non-operator → log + ignore.
    3. Classify via OpenRouter; ``None`` → fall-through (no DM, no api call).
    4. Route to the per-action handler.
    """
    text = normalized.text or ""
    if not text.strip():
        return None
    if _is_slash_prefixed(text):
        return None
    if not _looks_like_service_intent(text):
        # Not a plausible service-management phrasing — skip the auth round
        # trip and the LLM call entirely, let the rest of the inbound
        # pipeline (HITL operator-reply, customer fall-through, etc.) own it.
        return None

    trace_id = f"tg-update-{normalized.update_id}"
    resolved = await _resolve_operator_or_ignore(
        normalized=normalized,
        api_client=api_client,
        primary_operator_username=primary_operator_username,
        admin_username=admin_username,
        trace_id=trace_id,
    )
    if resolved is None:
        # Unauthorized: log + return None so the rest of the inbound pipeline
        # (existing keyword-based services NL dialog, customer fall-through,
        # ...) keeps its own behavior for non-operator senders. The LLM was
        # NOT called — that's the cost-control invariant the spy test asserts.
        return None

    intent = await classify_service_intent(text, openrouter=openrouter)
    if intent is None:
        return None

    if intent.action == ACTION_ADD:
        return await _handle_add(
            normalized=normalized,
            resolved=resolved,
            intent=intent,
            api_client=api_client,
            send_dm=send_dm,
            internal_token=internal_token,
            trace_id=trace_id,
        )
    if intent.action == ACTION_LIST:
        return await _handle_list(
            normalized=normalized,
            resolved=resolved,
            api_client=api_client,
            send_dm=send_dm,
            internal_token=internal_token,
            trace_id=trace_id,
        )
    if intent.action == ACTION_REMOVE:
        return await _handle_remove(
            normalized=normalized,
            resolved=resolved,
            intent=intent,
            api_client=api_client,
            send_dm=send_dm,
            internal_token=internal_token,
            trace_id=trace_id,
        )
    # action == ACTION_DESCRIBE — exhaustive by the validation in
    # ``classify_service_intent``.
    return await _handle_describe(
        normalized=normalized,
        resolved=resolved,
        intent=intent,
        api_client=api_client,
        send_dm=send_dm,
        internal_token=internal_token,
        trace_id=trace_id,
    )

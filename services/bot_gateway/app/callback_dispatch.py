from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from typing import Any

from services.bot_gateway.app.telegram_callback import NormalizedCallbackQuery

logger = logging.getLogger(__name__)

CallbackHandler = Callable[
    [NormalizedCallbackQuery, str, str],
    Awaitable[dict[str, str] | None],
]


async def dispatch_callback_query(
    normalized: NormalizedCallbackQuery,
    *,
    handlers: dict[str, CallbackHandler],
    telegram_bot_sender: Any,
) -> dict[str, str]:
    """Dispatch callback query by namespace and always answer it."""
    data = normalized.data or ""
    namespace = ""
    action = ""
    argument = ""
    parts = data.split(":", 2)
    if len(parts) == 3:
        namespace, action, argument = parts

    result: dict[str, str] = {
        "status": "processed",
        "route": "callback_query",
        "namespace": namespace or "unknown",
    }
    answer_text: str | None = ""
    try:
        handler = handlers.get(namespace)
        if handler is None:
            logger.info(
                "callback_unhandled",
                extra={"namespace": namespace, "data": data},
            )
            result["decision"] = "unhandled_namespace"
            return result
        handled = await handler(normalized, action, argument)
        if handled:
            result.update(handled)
            answer_text = handled.get("answer_text")
        else:
            result["decision"] = "handler_noop"
    finally:
        try:
            await telegram_bot_sender.answer_callback_query(
                callback_query_id=normalized.callback_query_id,
                text=answer_text,
            )
        except Exception:
            logger.warning(
                "callback_answer_failed",
                extra={"callback_query_id": normalized.callback_query_id},
                exc_info=True,
            )
    return result

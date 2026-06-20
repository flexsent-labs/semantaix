"""Route private customer DMs to the api inbound seam."""

from __future__ import annotations

import asyncio
import logging
import uuid
from datetime import UTC, datetime
from typing import Any, Protocol

from services.user_gateway.app.api_client import ApiClient

logger = logging.getLogger(__name__)


class RateLimiter(Protocol):
    def check_and_record(
        self, *, chat_id: int, now: datetime, max_messages: int, window_seconds: int
    ) -> bool: ...


def _normalize_username(username: str | None) -> str:
    if not username:
        return ""
    return username.lstrip("@").lower()


class MessageRouter:
    def __init__(
        self,
        *,
        api_client: ApiClient,
        queue: asyncio.Queue[Any],
        operator_id: int | None,
        linked_username: str | None,
        rate_limiter: RateLimiter | None = None,
        rate_limit_messages: int = 10,
        rate_limit_window_seconds: int = 300,
    ) -> None:
        self._api_client = api_client
        self._queue = queue
        self._operator_id = operator_id
        self._linked_username = _normalize_username(linked_username)
        self._rate_limiter = rate_limiter
        self._rate_limit_messages = rate_limit_messages
        self._rate_limit_window_seconds = rate_limit_window_seconds

    async def handle_new_message(self, event: Any) -> None:
        if not getattr(event, "is_private", False):
            return
        sender = getattr(event, "sender", None)
        sender_username = _normalize_username(
            getattr(sender, "username", None) if sender is not None else None
        )
        if self._linked_username and sender_username == self._linked_username:
            return
        sender_id = int(getattr(sender, "id", 0) or 0) if sender is not None else 0
        # Channel parity with the Bot-API path (story 12.103): a flood of customer
        # DMs would otherwise hit the LLM pipeline unbounded. Rate-limit per sender
        # BEFORE enqueueing; over-budget messages are dropped silently (no reply on
        # the operator's personal account). Sync repo dispatched off the event loop.
        if self._rate_limiter is not None:
            allowed = await asyncio.to_thread(
                self._rate_limiter.check_and_record,
                chat_id=sender_id,
                now=datetime.now(UTC),
                max_messages=self._rate_limit_messages,
                window_seconds=self._rate_limit_window_seconds,
            )
            if not allowed:
                logger.warning("user_gateway_rate_limited sender_id=%s", sender_id)
                return
        message = getattr(event, "message", event)
        try:
            self._queue.put_nowait(message)
        except asyncio.QueueFull:
            logger.warning("user_gateway_queue_full sender_id=%s", sender_id)

    async def drain_queue(self) -> None:
        while True:
            message = await self._queue.get()
            try:
                await self._forward(message)
            finally:
                self._queue.task_done()

    def _derive_trace_id(self, *, chat_id: int, message: Any) -> str:
        """Deterministic trace_id for a Telethon message.

        The api dedups ``/conversations/inbound`` on ``trace_id`` alone, so a
        re-delivered MTProto message (Telethon re-fires on reconnect / gap
        recovery) MUST reuse the same trace_id to be deduplicated — mirroring the
        Bot-API ``_derive_trace_id`` (``tg-update-{update_id}``). Keying on the
        Telethon message id makes re-delivery collide on one trace_id; a missing
        id falls back to a random uuid (no idempotency, but never a crash).
        """
        message_id = getattr(message, "id", None)
        if isinstance(message_id, int):
            return f"tg-user-{self._operator_id}-{chat_id}-{message_id}"
        return str(uuid.uuid4())

    async def _forward(self, message: Any) -> None:
        text = str(getattr(message, "message", "") or getattr(message, "text", "") or "")
        chat_id = int(getattr(message, "chat_id", 0) or 0)
        sender = getattr(message, "sender", None)
        customer_username = getattr(sender, "username", None) if sender else None
        trace_id = self._derive_trace_id(chat_id=chat_id, message=message)
        try:
            await self._api_client.forward_inbound(
                chat_id=chat_id,
                text=text,
                customer_username=customer_username,
                trace_id=trace_id,
                delivery_channel="operator_user",
                operator_id=self._operator_id,
            )
        except Exception:
            logger.warning(
                "user_gateway_forward_failed chat_id=%s operator_id=%s",
                chat_id,
                self._operator_id,
                exc_info=True,
            )

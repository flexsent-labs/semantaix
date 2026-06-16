"""Route private customer DMs to the api inbound seam."""

from __future__ import annotations

import asyncio
import logging
import uuid
from typing import Any

from services.user_gateway.app.api_client import ApiClient

logger = logging.getLogger(__name__)


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
    ) -> None:
        self._api_client = api_client
        self._queue = queue
        self._operator_id = operator_id
        self._linked_username = _normalize_username(linked_username)

    async def handle_new_message(self, event: Any) -> None:
        if not getattr(event, "is_private", False):
            return
        sender = getattr(event, "sender", None)
        sender_username = _normalize_username(
            getattr(sender, "username", None) if sender is not None else None
        )
        if self._linked_username and sender_username == self._linked_username:
            return
        message = getattr(event, "message", event)
        try:
            self._queue.put_nowait(message)
        except asyncio.QueueFull:
            sender_id = getattr(sender, "id", None)
            logger.warning("user_gateway_queue_full sender_id=%s", sender_id)

    async def drain_queue(self) -> None:
        while True:
            message = await self._queue.get()
            try:
                await self._forward(message)
            finally:
                self._queue.task_done()

    async def _forward(self, message: Any) -> None:
        text = str(getattr(message, "message", "") or getattr(message, "text", "") or "")
        chat_id = int(getattr(message, "chat_id", 0) or 0)
        sender = getattr(message, "sender", None)
        customer_username = getattr(sender, "username", None) if sender else None
        trace_id = str(uuid.uuid4())
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

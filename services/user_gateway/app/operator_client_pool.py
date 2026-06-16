"""Per-operator Telethon client lifecycle and message handlers."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from services.user_gateway.app.message_router import MessageRouter
from services.user_gateway.app.operator_auth_repo import OperatorTelegramAuthRepository

logger = logging.getLogger(__name__)

ClientFactory = Callable[[str], Any]
HandlerRegistrar = Callable[[Any, Callable[..., Awaitable[None]]], Any]


class OperatorClientPool:
    def __init__(
        self,
        *,
        operator_auth_repo: OperatorTelegramAuthRepository,
        client_factory: ClientFactory,
        handler_registrar: HandlerRegistrar,
        router_factory: Callable[[int, str | None], MessageRouter],
    ) -> None:
        self._operator_auth_repo = operator_auth_repo
        self._client_factory = client_factory
        self._handler_registrar = handler_registrar
        self._router_factory = router_factory
        self._clients: dict[int, Any] = {}
        self._routers: dict[int, MessageRouter] = {}
        self._tasks: dict[int, list[asyncio.Task[None]]] = {}

    def get_client(self, operator_id: int) -> Any | None:
        return self._clients.get(operator_id)

    def is_connected(self, operator_id: int) -> bool:
        client = self._clients.get(operator_id)
        if client is None:
            return False
        return bool(getattr(client, "is_connected", lambda: False)())

    async def start(
        self,
        operator_id: int,
        *,
        session_path: str,
        linked_username: str | None = None,
    ) -> None:
        if operator_id in self._clients:
            return
        Path(session_path).parent.mkdir(parents=True, exist_ok=True)
        client = self._client_factory(session_path)
        client.flood_sleep_threshold = 60
        connect = getattr(client, "connect", None)
        if connect is not None:
            await connect()
        router = self._router_factory(operator_id, linked_username)
        self._handler_registrar(client, router.handle_new_message)
        self._clients[operator_id] = client
        self._routers[operator_id] = router
        drain_task = asyncio.create_task(
            router.drain_queue(), name=f"drain_{operator_id}"
        )
        self._tasks[operator_id] = [drain_task]
        self._operator_auth_repo.set_customer_channel_active(operator_id, True)
        logger.info("operator_client_started operator_id=%s", operator_id)

    async def stop(self, operator_id: int) -> None:
        tasks = self._tasks.pop(operator_id, [])
        for task in tasks:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        client = self._clients.pop(operator_id, None)
        self._routers.pop(operator_id, None)
        if client is not None:
            disconnect = getattr(client, "disconnect", None)
            if disconnect is not None:
                await disconnect()
        if self._operator_auth_repo.get(operator_id) is not None:
            self._operator_auth_repo.set_customer_channel_active(operator_id, False)
        logger.info("operator_client_stopped operator_id=%s", operator_id)

    async def stop_all(self) -> None:
        for operator_id in list(self._clients):
            await self.stop(operator_id)

    async def send_message(self, operator_id: int, *, chat_id: int, text: str) -> None:
        client = self._clients.get(operator_id)
        if client is None:
            raise KeyError(f"operator {operator_id} not connected")
        await client.send_message(chat_id, text)

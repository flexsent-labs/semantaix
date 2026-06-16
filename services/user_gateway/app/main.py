"""User gateway service — Telegram MTProto customer channel."""

from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI

from platform_common.app_factory import create_service_app
from platform_common.settings import get_settings
from services.user_gateway.app.api_client import ApiClient
from services.user_gateway.app.auth_session_repo import AuthSessionRepository
from services.user_gateway.app.message_router import MessageRouter
from services.user_gateway.app.operator_auth_repo import OperatorTelegramAuthRepository
from services.user_gateway.app.operator_client_pool import OperatorClientPool
from services.user_gateway.app.routers import auth as auth_router
from services.user_gateway.app.routers import messages as messages_router
from services.user_gateway.app.telegram_auth import TelethonAuthService

logger = logging.getLogger(__name__)
settings = get_settings()

auth_session_repo = AuthSessionRepository(settings.user_gateway_db_path)
operator_auth_repo = OperatorTelegramAuthRepository(settings.user_gateway_db_path)
api_client = ApiClient(
    base_url=settings.api_internal_base_url,
    internal_token=settings.internal_service_token or "",
)


def _default_client_factory(session_path: str) -> Any:
    from telethon import TelegramClient
    from telethon.sessions import SQLiteSession

    return TelegramClient(
        SQLiteSession(session_path),
        int(settings.telegram_api_id or 0),
        settings.telegram_api_hash or "",
    )


def _handler_registrar(client: Any, handler) -> Any:
    from telethon import events

    return client.on(events.NewMessage)(handler)


def _router_factory(operator_id: int, linked_username: str | None) -> MessageRouter:
    return MessageRouter(
        api_client=api_client,
        queue=asyncio.Queue(maxsize=100),
        operator_id=operator_id,
        linked_username=linked_username,
    )


async def _on_authenticated(operator_id: int | None, linked_username: str | None) -> None:
    if operator_id is None:
        return
    record = operator_auth_repo.get(operator_id)
    if record is None:
        return
    await operator_client_pool.start(
        operator_id,
        session_path=record.session_path,
        linked_username=linked_username or record.linked_username,
    )


operator_client_pool = OperatorClientPool(
    operator_auth_repo=operator_auth_repo,
    client_factory=_default_client_factory,
    handler_registrar=_handler_registrar,
    router_factory=_router_factory,
)

auth_service = TelethonAuthService(
    settings=settings,
    auth_session_repo=auth_session_repo,
    operator_auth_repo=operator_auth_repo,
    api_client=api_client,
    client_factory=_default_client_factory,
    on_authenticated=_on_authenticated,
)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    auth_service.clear_stale_on_startup()
    yield
    await operator_client_pool.stop_all()


app = create_service_app("user_gateway", lifespan=lifespan)
app.include_router(auth_router.router)
app.include_router(messages_router.router)

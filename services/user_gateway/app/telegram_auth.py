"""Telethon QR login orchestration for legacy and per-operator sessions."""

from __future__ import annotations

import asyncio
import base64
import io
import logging
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

import qrcode
from telethon.errors import PasswordHashInvalidError, SessionPasswordNeededError
from telethon.sessions import SQLiteSession

from platform_common.settings import AppSettings, get_settings
from services.user_gateway.app.api_client import ApiClient
from services.user_gateway.app.auth_session_repo import AuthSessionRepository
from services.user_gateway.app.auth_state import get_state, reset_all_states, reset_state
from services.user_gateway.app.operator_auth_repo import OperatorTelegramAuthRepository

logger = logging.getLogger(__name__)

ClientFactory = Callable[[str], Any]
OnAuthenticated = Callable[[int | None, str | None], Awaitable[None]]


class TelethonAuthService:
    def __init__(
        self,
        *,
        settings: AppSettings | None = None,
        auth_session_repo: AuthSessionRepository | None = None,
        operator_auth_repo: OperatorTelegramAuthRepository | None = None,
        api_client: ApiClient | None = None,
        client_factory: ClientFactory | None = None,
        on_authenticated: OnAuthenticated | None = None,
    ) -> None:
        self._settings = settings or get_settings()
        self._auth_session_repo = auth_session_repo or AuthSessionRepository(
            self._settings.user_gateway_db_path
        )
        self._operator_auth_repo = operator_auth_repo or OperatorTelegramAuthRepository(
            self._settings.user_gateway_db_path
        )
        self._api_client = api_client or ApiClient(
            base_url=self._settings.api_internal_base_url,
            internal_token=self._settings.internal_service_token or "",
        )
        self._client_factory = client_factory or self._default_client_factory
        self._on_authenticated = on_authenticated
        self._background_tasks: set[asyncio.Task[None]] = set()

    def _default_client_factory(self, session_path: str) -> Any:
        from telethon import TelegramClient

        api_id = int(self._settings.telegram_api_id or 0)
        api_hash = self._settings.telegram_api_hash or ""
        return TelegramClient(SQLiteSession(session_path), api_id, api_hash)

    def _session_path(self, operator_id: int | None) -> str:
        if operator_id is None:
            return self._settings.tg_user_session_path
        return str(
            Path(self._settings.operator_sessions_dir) / f"{operator_id}.session"
        )

    async def _validate_operator(self, operator_id: int) -> None:
        operator = await self._api_client.get_operator(operator_id)
        if operator is None:
            from fastapi import HTTPException

            raise HTTPException(status_code=404, detail="operator_not_found")

    def get_status(self, operator_id: int | None = None) -> dict[str, object]:
        state = get_state(operator_id)
        return {
            "phase": state.phase,
            "authenticated": state.phase == "authenticated",
        }

    async def qr_start(self, operator_id: int | None = None) -> dict[str, object]:
        if operator_id is not None:
            await self._validate_operator(operator_id)
        state = get_state(operator_id)
        if state.phase not in ("idle", "qr_pending"):
            from fastapi import HTTPException

            raise HTTPException(status_code=409, detail="already_authenticated")

        session_path = self._session_path(operator_id)
        Path(session_path).parent.mkdir(parents=True, exist_ok=True)
        if operator_id is not None:
            self._operator_auth_repo.upsert(
                operator_id=operator_id,
                phase="qr_pending",
                session_path=session_path,
            )
        else:
            self._auth_session_repo.set_phase("qr_pending")

        client = self._client_factory(session_path)
        client.flood_sleep_threshold = 60
        await client.connect()
        qr_login = await client.qr_login()
        qr_image_b64 = _render_qr_b64(qr_login.url)

        state.phase = "qr_pending"
        state.client = client
        state.qr_login = qr_login

        task = asyncio.create_task(
            self._await_qr_scan(operator_id=operator_id),
            name=f"qr_scan_{operator_id}",
        )
        self._background_tasks.add(task)
        task.add_done_callback(self._background_tasks.discard)

        return {"qr_image_b64": qr_image_b64, "expires_in": 30}

    async def verify_2fa(
        self, password: str, operator_id: int | None = None
    ) -> dict[str, str]:
        state = get_state(operator_id)
        async with state._lock:
            if state.phase != "2fa_pending" or state.client is None:
                from fastapi import HTTPException

                raise HTTPException(
                    status_code=409,
                    detail=(
                        "no_pending_auth: phase is not 2fa_pending; "
                        "call /user_login to restart"
                    ),
                )
            try:
                await state.client.sign_in(password=password)
            except PasswordHashInvalidError:
                from fastapi import HTTPException

                raise HTTPException(status_code=401, detail="invalid_password") from None
            await self._mark_authenticated(operator_id, state)
        return {"status": "authenticated"}

    async def _await_qr_scan(self, operator_id: int | None) -> None:
        state = get_state(operator_id)
        qr_login = state.qr_login
        if qr_login is None or state.client is None:
            return
        try:
            await qr_login.wait(timeout=30)
            await self._mark_authenticated(operator_id, state)
        except asyncio.TimeoutError:
            await qr_login.recreate()
            state.qr_login = qr_login
        except SessionPasswordNeededError:
            state.phase = "2fa_pending"
            if operator_id is not None:
                self._operator_auth_repo.set_phase(operator_id, "2fa_pending")
            else:
                self._auth_session_repo.set_phase("2fa_pending")
        except Exception:
            logger.exception("qr_scan_failed operator_id=%s", operator_id)
            reset_state(operator_id)

    async def _mark_authenticated(
        self, operator_id: int | None, state: Any
    ) -> None:
        state.phase = "authenticated"
        linked_username: str | None = None
        if state.client is not None:
            me = await state.client.get_me()
            linked_username = getattr(me, "username", None)
        if operator_id is not None:
            record = self._operator_auth_repo.get(operator_id)
            session_path = record.session_path if record else self._session_path(operator_id)
            self._operator_auth_repo.upsert(
                operator_id=operator_id,
                phase="authenticated",
                session_path=session_path,
                linked_username=linked_username,
            )
        else:
            self._auth_session_repo.set_phase("authenticated")
        if self._on_authenticated is not None:
            await self._on_authenticated(operator_id, linked_username)

    def clear_stale_on_startup(self) -> None:
        stale = self._auth_session_repo.clear_stale_on_startup()
        if stale in ("qr_pending", "2fa_pending"):
            logger.warning(
                "auth_restart: cleared stale phase=%s; operator must /user_login again",
                stale,
            )
        for operator_id in self._operator_auth_repo.clear_stale_on_startup():
            logger.warning(
                "auth_restart: cleared stale operator_id=%s; operator must relink",
                operator_id,
            )
        reset_all_states()


def _render_qr_b64(url: str) -> str:
    image = qrcode.make(url)
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return base64.b64encode(buffer.getvalue()).decode("ascii")

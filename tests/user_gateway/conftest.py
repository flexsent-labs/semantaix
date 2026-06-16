"""Shared fakes for user_gateway Telethon tests."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any


class FakeQRLogin:
    def __init__(self, url: str = "tg://login?token=test") -> None:
        self.url = url
        self.recreate_calls = 0

    async def wait(self, timeout: float = 30) -> None:
        return None

    async def recreate(self) -> None:
        self.recreate_calls += 1


class FakeClient:
    def __init__(self, *, qr_login: FakeQRLogin | None = None) -> None:
        self.flood_sleep_threshold = 0
        self._qr_login = qr_login or FakeQRLogin()
        self.connected = False
        self.sent: list[tuple[int, str]] = []

    async def connect(self) -> None:
        self.connected = True

    async def disconnect(self) -> None:
        self.connected = False

    def is_connected(self) -> bool:
        return self.connected

    async def qr_login(self) -> FakeQRLogin:
        return self._qr_login

    async def sign_in(self, password: str) -> None:
        from telethon.errors import PasswordHashInvalidError

        if password == "bad":
            raise PasswordHashInvalidError(request=None)

    async def get_me(self) -> Any:
        return SimpleNamespace(username="linkeduser")

    async def send_message(self, chat_id: int, text: str) -> None:
        self.sent.append((chat_id, text))

    def on(self, _event):
        def decorator(handler):
            return handler

        return decorator


class TimeoutQRLogin(FakeQRLogin):
    async def wait(self, timeout: float = 30) -> None:
        raise asyncio.TimeoutError


class TwoFaQRLogin(FakeQRLogin):
    async def wait(self, timeout: float = 30) -> None:
        from telethon.errors import SessionPasswordNeededError

        raise SessionPasswordNeededError(request=None)

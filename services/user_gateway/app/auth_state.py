"""In-memory Telethon auth state — not serializable, keyed per operator."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any

_LEGACY_KEY: int | None = None


@dataclass
class _AuthState:
    phase: str = "idle"
    client: Any | None = None
    qr_login: Any | None = None
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock)


_states: dict[int | None, _AuthState] = {_LEGACY_KEY: _AuthState()}


def get_state(operator_id: int | None = None) -> _AuthState:
    if operator_id not in _states:
        _states[operator_id] = _AuthState()
    return _states[operator_id]


def reset_state(operator_id: int | None = None) -> None:
    state = get_state(operator_id)
    state.phase = "idle"
    state.client = None
    state.qr_login = None


def reset_all_states() -> None:
    for key in list(_states):
        reset_state(key)

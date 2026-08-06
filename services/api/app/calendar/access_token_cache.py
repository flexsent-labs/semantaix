"""Access-token minting/caching with single-flight refresh (story 11.04).

The cache holds a per-``(project_id, operator)`` short-lived access token and
refreshes it only when it is missing or within ``skew`` of expiry — never per
request (performance rule). Refresh is **single-flight**: a per-key
``asyncio.Lock`` (from an injected factory) serialises concurrent inbound
messages so they neither double-mint nor race the SQLite write.

When the refresh token itself is dead (``TokenRefreshFailed``) the cache marks
the token row ``reconnect_needed``, emits an incident, notifies the operator
over Telegram to re-run ``/connect_calendar``, and raises
``CalendarReconnectNeeded`` to the caller (11.07 translates it to an
escalation). Keeping the row preserves reconnect deduplication across API
restarts; tokens never reach a log line or an incident summary.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta
from typing import Protocol

from services.api.app.calendar.oauth import AccessToken, TokenRefreshFailed
from services.api.app.calendar.token_repository import STATUS_RECONNECT_NEEDED

logger = logging.getLogger(__name__)

_DEFAULT_SKEW = timedelta(seconds=60)
_RECONNECT_MESSAGE = (
    "Доступ к календарю больше недействителен. "
    "Пожалуйста, переподключите его командой /connect_calendar."
)


class CalendarReconnectNeeded(Exception):
    """Raised when the operator's refresh token is dead and re-consent is required."""


class _OAuthClient(Protocol):
    def refresh(self, *, refresh_token: str) -> AccessToken: ...


class _TokenRepo(Protocol):
    def get_refresh_token(self, project_id: int, operator: str) -> str: ...
    def get_status(self, project_id: int, operator: str) -> str | None: ...
    def set_status(self, project_id: int, operator: str, status: str) -> None: ...
    def delete(self, project_id: int, operator: str) -> None: ...


class _Clock(Protocol):
    def now(self) -> datetime: ...


class _IncidentSink(Protocol):
    def ingest(self, *, fingerprint: str, severity: str, summary: str) -> object: ...


class _Notifier(Protocol):
    async def send_message(self, *, chat_id: int, text: str) -> int: ...


class AccessTokenProvider:
    def __init__(
        self,
        *,
        oauth_client: _OAuthClient,
        token_repo: _TokenRepo,
        clock: _Clock,
        lock_factory,
        incident_sink: _IncidentSink,
        notifier: _Notifier,
        skew: timedelta = _DEFAULT_SKEW,
    ) -> None:
        self._oauth_client = oauth_client
        self._token_repo = token_repo
        self._clock = clock
        self._lock_factory = lock_factory
        self._incident_sink = incident_sink
        self._notifier = notifier
        self._skew = skew
        self._cache: dict[tuple[int, str], AccessToken] = {}
        self._locks: dict[tuple[int, str], asyncio.Lock] = {}

    def _lock_for(self, key: tuple[int, str]) -> asyncio.Lock:
        lock = self._locks.get(key)
        if lock is None:
            lock = self._lock_factory()
            self._locks[key] = lock
        return lock

    def _is_fresh(self, token: AccessToken) -> bool:
        return self._clock.now() < token.expiry - self._skew

    async def get_access_token(
        self,
        project_id: int,
        operator: str,
        *,
        operator_chat_id: int,
        trace_id: str,
    ) -> str:
        """Return a valid access token, refreshing under a single-flight lock.

        ``operator_chat_id``/``trace_id`` ride along for the reconnect DM and the
        incident — they are never logged as secrets.
        """
        key = (project_id, operator)
        cached = self._cache.get(key)
        if cached is not None and self._is_fresh(cached):
            return cached.access_token

        async with self._lock_for(key):
            # Re-check inside the lock: a sibling caller may have just refreshed.
            cached = self._cache.get(key)
            if cached is not None and self._is_fresh(cached):
                return cached.access_token
            return await self._refresh(
                key, operator_chat_id=operator_chat_id, trace_id=trace_id
            )

    async def _refresh(
        self,
        key: tuple[int, str],
        *,
        operator_chat_id: int,
        trace_id: str,
    ) -> str:
        project_id, operator = key
        # Short-circuit: if the token row is already marked dead, do NOT call
        # Google again and do NOT re-DM the operator. The user's first DM
        # already told them to /connect_calendar; spamming the same message
        # on every restart / every inbound message is the bug we are fixing.
        existing_status = await asyncio.to_thread(
            self._token_repo.get_status, project_id, operator
        )
        if existing_status == STATUS_RECONNECT_NEEDED:
            logger.info(
                "calendar_token_reconnect_pending_short_circuit",
                extra={"trace_id": trace_id},
            )
            raise CalendarReconnectNeeded("reconnect_needed_already_pending")
        refresh_token = await asyncio.to_thread(
            self._token_repo.get_refresh_token, project_id, operator
        )
        try:
            token = await asyncio.to_thread(
                self._oauth_client.refresh, refresh_token=refresh_token
            )
        except TokenRefreshFailed as exc:
            await self._handle_dead_token(
                key, operator_chat_id=operator_chat_id, trace_id=trace_id
            )
            raise CalendarReconnectNeeded("reconnect_needed") from exc
        self._cache[key] = token
        logger.info("calendar_access_token_refreshed", extra={"trace_id": trace_id})
        return token.access_token

    async def _handle_dead_token(
        self,
        key: tuple[int, str],
        *,
        operator_chat_id: int,
        trace_id: str,
    ) -> None:
        project_id, operator = key
        self._cache.pop(key, None)
        # Persistent dedup: if the token is ALREADY flagged reconnect_needed,
        # the operator was already DM'd and an incident already exists for
        # this dead-token cycle. Do not duplicate. The status is reset to
        # 'connected' (and a future DM is re-armed) only when the operator
        # successfully re-runs /connect_calendar, which calls
        # CalendarTokenRepository.upsert — that overwrites status to
        # 'connected'.
        current_status = await asyncio.to_thread(
            self._token_repo.get_status, project_id, operator
        )
        if current_status == STATUS_RECONNECT_NEEDED:
            logger.info(
                "calendar_token_reconnect_dm_dedup",
                extra={"trace_id": trace_id},
            )
            return
        await asyncio.to_thread(
            self._token_repo.set_status, project_id, operator, STATUS_RECONNECT_NEEDED
        )
        # Note: we deliberately do NOT delete the token row. Keeping the row
        # with status='reconnect_needed' (a) lets the dedup check above work
        # persistently across api restarts, (b) lets the R2 connect-callback
        # DM guard correctly recognize this as a re-consent (token row still
        # present) and suppress its own "Календарь подключён" message on the
        # recovery callback, and (c) preserves audit history of the dead
        # event. The row is overwritten with fresh credentials + status
        # 'connected' when the operator successfully runs /connect_calendar.
        await asyncio.to_thread(
            self._incident_sink.ingest,
            fingerprint=f"calendar_reconnect_needed:{project_id}:{operator}",
            severity="warning",
            summary=(
                f"Calendar refresh token revoked/expired for operator {operator} "
                f"(project {project_id}); reconnect required."
            ),
        )
        logger.warning(
            "calendar_token_reconnect_needed", extra={"trace_id": trace_id}
        )
        await self._notifier.send_message(
            chat_id=operator_chat_id, text=_RECONNECT_MESSAGE
        )

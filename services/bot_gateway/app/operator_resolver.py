"""Flat operator resolver for bot_gateway (Epic 10.5 story 10.5-02).

Replaces the primary-operator fallback with a fail-closed lookup against the
api's ``operators`` registry.  An unreachable api is treated as no-match so
operator commands are silently skipped; the bot never acts on an unverified
sender identity.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

import httpx

from platform_common.settings import get_settings
from services.bot_gateway.app.api_client import ApiClient

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ResolvedOperator:
    username: str
    chat_id: int | None
    project_id: int | None
    is_active: bool
    source: str  # always "registry" after Epic 10.5


async def resolve_operator_for_sender(
    *,
    username: str | None,
    api_client: ApiClient,
) -> ResolvedOperator | None:
    """Resolve a Telegram username to a registered operator, if any.

    Returns:
        - ResolvedOperator(source="registry") for a registered active operator.
        - None when the sender is not a registered operator, is inactive, or
          when the api is unreachable (fail-closed — no fallback).
    """
    if not username:
        return None
    if get_settings().is_platform_admin_username(username):
        return None
    try:
        record = await api_client.find_operator_by_username(username=username)
    except httpx.HTTPStatusError as exc:
        logger.warning(
            "operator_resolution_unavailable",
            extra={"username": username, "status": exc.response.status_code},
        )
        return None
    except (httpx.RequestError, httpx.TransportError, OSError) as exc:
        logger.warning(
            "operator_resolution_unavailable",
            extra={"username": username, "error": str(exc)},
        )
        return None

    if record is not None and bool(record.get("is_active", True)):
        return ResolvedOperator(
            username=str(record["username"]),
            chat_id=(
                int(record["chat_id"])
                if record.get("chat_id") is not None
                else None
            ),
            project_id=int(record["project_id"]),
            is_active=True,
            source="registry",
        )
    return None

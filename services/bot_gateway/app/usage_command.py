"""/usage bot command — role-aware, three-tracker output, deep link (Story 14.08)."""
from __future__ import annotations

import json
import logging
import re
from collections.abc import Awaitable, Callable
from datetime import datetime
from functools import lru_cache
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from services.bot_gateway.app.api_client import ApiClient
from services.bot_gateway.app.operator_resolver import resolve_operator_for_sender
from services.bot_gateway.app.telegram_update import NormalizedTelegramMessage
from services.bot_gateway.app.usage_formatter import format_degraded, format_usage

logger = logging.getLogger(__name__)

SendDmFn = Callable[[int, str], Awaitable[Any]]

_USAGE_RE = re.compile(r"^/usage(?:\s+(.+))?\s*$", re.IGNORECASE)
_MAX_PROJECTS_LIST = 10


@lru_cache(maxsize=1)
def _strings() -> dict:
    path = Path(__file__).resolve().parents[3] / "data" / "russian_usage_strings.json"
    return json.loads(path.read_text(encoding="utf-8"))


def _today_in_tz(timezone: str) -> str:
    """Return today's YYYY-MM-DD date string in the given IANA timezone."""
    tz = ZoneInfo(timezone)
    return datetime.now(tz).date().isoformat()


def _deep_link(web_ui_base_url: str, project_id: int) -> str:
    base = web_ui_base_url.rstrip("/")
    return f"{base}/admin/usage?project_id={project_id}&window=1d"


async def handle_usage_command(
    *,
    normalized: NormalizedTelegramMessage,
    api_client: ApiClient,
    send_dm: SendDmFn,
    admin_username: str,
    internal_token: str,
    web_ui_base_url: str,
    default_timezone: str,
) -> dict | None:
    """Handle /usage [project_name] command.

    Returns a routing dict on match, None if the message isn't a /usage command.
    Non-registered senders are silently skipped (no DM); logged as
    ``unauthorized_usage``.
    """
    m = _USAGE_RE.match(normalized.text or "")
    if m is None:
        return None

    username = normalized.username or ""
    is_admin = bool(username) and username == admin_username
    project_arg: str | None = m.group(1)

    # Operator-gating: every sender must be registered unless they're the admin.
    operator = await resolve_operator_for_sender(
        username=username, api_client=api_client
    )
    if not is_admin and operator is None:
        logger.info("unauthorized_usage", extra={"username": username})
        return {"status": "ignored", "reason": "unauthorized_usage"}

    scope = "admin" if is_admin else "operator"

    # --- Resolve project ---
    if is_admin:
        if project_arg:
            project_id, project_name = await _resolve_project_by_name(
                project_arg, api_client
            )
            if project_id is None:
                s = _strings()
                await send_dm(
                    normalized.chat_id,
                    s["unknown_project"].format(name=project_arg),
                )
                return {"status": "usage_unknown_project"}
        else:
            # No argument and no context — ask admin to specify
            projects = (await api_client.list_projects()).get("items", [])
            top = projects[:_MAX_PROJECTS_LIST]
            projects_list = "\n".join(
                f"  • {p['name']} ({p['slug']})" for p in top
            )
            await send_dm(
                normalized.chat_id,
                _strings()["admin_specify_project"].format(
                    projects_list=projects_list or "(нет проектов)"
                ),
            )
            return {"status": "usage_admin_specify_project"}
    else:
        if operator is None or operator.project_id is None:
            await send_dm(
                normalized.chat_id, _strings()["no_project_assigned_operator"]
            )
            return {"status": "usage_no_project"}
        project_id = operator.project_id
        project_name = await _get_project_name(project_id, api_client)

    # --- Fetch usage ---
    today = _today_in_tz(default_timezone)
    result = await api_client.fetch_usage_today(
        project_id=project_id,
        scope=scope,
        as_user=username,
        today_utc=today,
        internal_token=internal_token,
    )

    if result is None:
        await send_dm(normalized.chat_id, format_degraded())
        return {"status": "usage_degraded"}

    text = format_usage(
        summary_rows=result["summary_rows"],
        wasted_rows=result["wasted_rows"],
        scope=scope,
        project_name=project_name or str(project_id),
        deep_link=_deep_link(web_ui_base_url, project_id),
    )
    await send_dm(normalized.chat_id, text)
    return {"status": "usage_sent", "scope": scope}


async def _resolve_project_by_name(
    name: str, api_client: ApiClient
) -> tuple[int | None, str | None]:
    """Look up a project by name (case-insensitive) or slug.

    Returns (project_id, project_name) or (None, None) if not found.
    """
    try:
        projects = (await api_client.list_projects()).get("items", [])
    except Exception:
        return None, None
    name_lower = name.strip().lower()
    for p in projects:
        if (
            str(p.get("name", "")).lower() == name_lower
            or str(p.get("slug", "")).lower() == name_lower
        ):
            return int(p["id"]), str(p["name"])
    return None, None


async def _get_project_name(project_id: int, api_client: ApiClient) -> str | None:
    """Return the project name for a given project_id, or None on error."""
    try:
        projects = (await api_client.list_projects()).get("items", [])
    except Exception:
        return None
    for p in projects:
        if int(p.get("id", -1)) == project_id:
            return str(p["name"])
    return None

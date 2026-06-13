"""Shared auth helpers for web_ui routes."""
from __future__ import annotations

import httpx
from fastapi import Request

from platform_common.settings import get_settings

_settings = get_settings()


async def _api_get(
    request: Request, path: str, *, params: dict | None = None
) -> tuple[int, dict]:
    cookie = request.cookies.get(_settings.web_session_cookie_name)
    headers: dict[str, str] = {}
    if cookie:
        headers["Cookie"] = f"{_settings.web_session_cookie_name}={cookie}"
    url = f"{_settings.api_internal_base_url.rstrip('/')}{path}"
    async with httpx.AsyncClient(timeout=10) as client:
        response = await client.get(url, params=params or {}, headers=headers)
    try:
        body = response.json()
    except ValueError:
        body = {"detail": response.text or "api_returned_non_json"}
    return response.status_code, body


async def _resolve_principal(request: Request) -> dict | None:
    status, body = await _api_get(request, "/admin/auth/me")
    if status == 200:
        return body
    return None

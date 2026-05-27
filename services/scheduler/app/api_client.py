"""Thin httpx wrapper around the api's ``/sales/followups/*`` endpoints.

The scheduler is single-process and only calls the api over HTTP — no
direct DB access. The client owns the service-token header injection.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

import httpx

logger = logging.getLogger(__name__)


class ApiClient:
    def __init__(
        self,
        *,
        base_url: str,
        service_token: str,
        timeout_seconds: float = 10.0,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._service_token = service_token
        self._timeout = timeout_seconds

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._service_token}"}

    async def list_due_followups(
        self, *, now: datetime
    ) -> list[dict[str, Any]]:
        url = f"{self._base_url}/sales/followups/due"
        async with httpx.AsyncClient(timeout=self._timeout) as client:
            response = await client.get(
                url,
                params={"now": now.isoformat()},
                headers=self._headers(),
            )
            response.raise_for_status()
            payload = response.json()
        rows = payload.get("rows") or []
        return [row for row in rows if isinstance(row, dict)]

    async def skip_stale(self, followup_id: int) -> None:
        url = f"{self._base_url}/sales/followups/{followup_id}/skip-stale"
        async with httpx.AsyncClient(timeout=self._timeout) as client:
            response = await client.post(url, headers=self._headers())
            response.raise_for_status()

    async def reschedule(
        self, followup_id: int, *, new_fire_at: datetime
    ) -> None:
        url = f"{self._base_url}/sales/followups/{followup_id}/reschedule"
        async with httpx.AsyncClient(timeout=self._timeout) as client:
            response = await client.post(
                url,
                json={"new_fire_at": new_fire_at.isoformat()},
                headers=self._headers(),
            )
            response.raise_for_status()

    async def fire(self, followup_id: int) -> dict[str, Any]:
        url = f"{self._base_url}/sales/followups/{followup_id}/fire"
        async with httpx.AsyncClient(timeout=self._timeout) as client:
            response = await client.post(url, headers=self._headers())
            response.raise_for_status()
            data = response.json()
        return data if isinstance(data, dict) else {}


__all__ = ["ApiClient"]

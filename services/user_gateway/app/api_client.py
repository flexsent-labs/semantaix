"""Thin httpx client for api /conversations/inbound forwarding."""

from __future__ import annotations

import httpx


class ApiClient:
    def __init__(
        self,
        *,
        base_url: str,
        internal_token: str = "",
        timeout_seconds: float = 10.0,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._internal_token = internal_token
        self._timeout = timeout_seconds

    def _headers(self) -> dict[str, str]:
        if not self._internal_token:
            return {}
        return {"Authorization": f"Bearer {self._internal_token}"}

    async def forward_inbound(
        self,
        *,
        chat_id: int,
        text: str,
        customer_username: str | None,
        trace_id: str,
        delivery_channel: str = "operator_user",
        operator_id: int | None = None,
    ) -> dict:
        payload: dict[str, object] = {
            "text": text,
            "chat_id": chat_id,
            "customer_username": customer_username,
            "trace_id": trace_id,
            "delivery_channel": delivery_channel,
        }
        if operator_id is not None:
            payload["operator_id"] = operator_id
        async with httpx.AsyncClient(timeout=self._timeout) as client:
            response = await client.post(
                f"{self._base_url}/conversations/inbound",
                json=payload,
                headers=self._headers(),
            )
            response.raise_for_status()
            return response.json()

    async def get_operator(self, operator_id: int) -> dict | None:
        async with httpx.AsyncClient(timeout=self._timeout) as client:
            response = await client.get(
                f"{self._base_url}/operators/{operator_id}",
                headers=self._headers(),
            )
        if response.status_code == 404:
            return None
        response.raise_for_status()
        return response.json()

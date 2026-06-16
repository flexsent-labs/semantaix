from __future__ import annotations

import httpx


class UserGatewayClient:
    def __init__(
        self,
        *,
        base_url: str,
        timeout_seconds: float = 15.0,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._timeout_seconds = timeout_seconds

    async def send_message(self, *, operator_id: int, chat_id: int, text: str) -> dict:
        payload = {
            "operator_id": operator_id,
            "chat_id": chat_id,
            "text": text,
        }
        async with httpx.AsyncClient(timeout=self._timeout_seconds) as client:
            response = await client.post(
                f"{self._base_url}/messages/send",
                json=payload,
            )
            response.raise_for_status()
            return response.json()

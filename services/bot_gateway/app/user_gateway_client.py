from __future__ import annotations

import httpx


class UserGatewayError(httpx.HTTPStatusError):
    def __init__(
        self,
        message: str,
        *,
        request: httpx.Request,
        response: httpx.Response,
        detail: str | None,
    ) -> None:
        super().__init__(message, request=request, response=response)
        self.detail = detail


def _extract_detail(response: httpx.Response) -> str | None:
    try:
        body = response.json()
    except ValueError:
        return None
    if isinstance(body, dict):
        detail = body.get("detail")
        if isinstance(detail, str):
            return detail
        if detail is not None:
            return str(detail)
    return None


def _raise_for_status(response: httpx.Response) -> None:
    try:
        response.raise_for_status()
    except httpx.HTTPStatusError as exc:
        raise UserGatewayError(
            str(exc),
            request=exc.request,
            response=exc.response,
            detail=_extract_detail(exc.response),
        ) from exc


class UserGatewayClient:
    def __init__(
        self,
        *,
        base_url: str,
        timeout_seconds: float = 10.0,
        internal_token: str = "",
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._timeout = timeout_seconds
        self._internal_token = internal_token

    def _headers(self) -> dict[str, str]:
        if not self._internal_token:
            return {}
        return {"Authorization": f"Bearer {self._internal_token}"}

    async def qr_start(self, *, operator_id: int) -> dict[str, object]:
        async with httpx.AsyncClient(timeout=self._timeout) as client:
            response = await client.post(
                f"{self._base_url}/auth/qr_start",
                params={"operator_id": operator_id},
                headers=self._headers(),
            )
        _raise_for_status(response)
        return response.json()

    async def status(self, *, operator_id: int) -> dict[str, object]:
        async with httpx.AsyncClient(timeout=self._timeout) as client:
            response = await client.get(
                f"{self._base_url}/auth/status",
                params={"operator_id": operator_id},
                headers=self._headers(),
            )
        _raise_for_status(response)
        return response.json()

    async def verify_2fa(self, *, operator_id: int, password: str) -> dict[str, object]:
        async with httpx.AsyncClient(timeout=self._timeout) as client:
            response = await client.post(
                f"{self._base_url}/auth/verify_2fa",
                params={"operator_id": operator_id},
                json={"password": password},
                headers=self._headers(),
            )
        _raise_for_status(response)
        return response.json()

    async def send_message(self, *, operator_id: int, chat_id: int, text: str) -> dict[str, object]:
        async with httpx.AsyncClient(timeout=self._timeout) as client:
            response = await client.post(
                f"{self._base_url}/messages/send",
                json={"operator_id": operator_id, "chat_id": chat_id, "text": text},
                headers=self._headers(),
            )
        _raise_for_status(response)
        return response.json()

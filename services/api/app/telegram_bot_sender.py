from __future__ import annotations

from pathlib import Path
from typing import Any, Literal

import httpx

_MediaField = Literal["video", "photo", "document"]


class TelegramMediaSendError(Exception):
    """Telegram media-send failure with a categorised reason.

    ``reason`` is one of ``telegram_send_failed`` (Telegram returned an error
    payload), ``telegram_network_error`` (httpx transport failure),
    ``local_file_missing`` (the local_path does not exist), or
    ``missing_bot_token`` (the sender is configured with the placeholder
    token). ``description`` carries the Telegram-supplied detail when
    available so the caller can surface it without re-parsing.
    """

    def __init__(
        self, reason: str, *, description: str | None = None
    ) -> None:
        super().__init__(
            reason if description is None else f"{reason}:{description}"
        )
        self.reason = reason
        self.description = description


class TelegramBotSender:
    def __init__(
        self,
        *,
        bot_token: str,
        base_url: str = "https://api.telegram.org",
        http_transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.bot_token = bot_token
        self._base_url = base_url.rstrip("/")
        self._http_transport = http_transport

    def _client(self, *, timeout: int) -> httpx.AsyncClient:
        if self._http_transport is not None:
            return httpx.AsyncClient(
                timeout=timeout, transport=self._http_transport
            )
        return httpx.AsyncClient(timeout=timeout)

    def _require_token(self) -> None:
        if self.bot_token == "replace-me" or not self.bot_token:
            raise RuntimeError("missing_bot_token")

    def _is_token_configured(self) -> bool:
        return bool(self.bot_token) and self.bot_token != "replace-me"

    async def send_message(self, *, chat_id: int, text: str) -> int:
        self._require_token()

        # sendMessage is NOT idempotent and Telegram offers no dedup key, so a
        # retry can only be safe when we KNOW the first attempt never reached
        # Telegram. That is true only for connection-establishment failures
        # (ConnectError / ConnectTimeout): the request body was never sent.
        # Everything else — read timeouts, mid-stream transport errors, and any
        # HTTP status (4xx or 5xx) — may have been raised *after* Telegram
        # already delivered the message, so retrying would double-post the same
        # text to the customer. We surface those immediately instead.
        url = f"{self._base_url}/bot{self.bot_token}/sendMessage"
        body = {"chat_id": chat_id, "text": text}
        async with self._client(timeout=15) as client:
            try:
                response = await client.post(url, json=body)
                response.raise_for_status()
            except (httpx.ConnectError, httpx.ConnectTimeout):
                response = await client.post(url, json=body)
                response.raise_for_status()
            payload = response.json()
            return int(payload["result"]["message_id"])

    async def send_video(
        self,
        *,
        chat_id: int,
        file_id: str | None = None,
        local_path: Path | None = None,
        caption: str | None = None,
    ) -> dict[str, Any]:
        return await self._send_media(
            method="sendVideo",
            field="video",
            chat_id=chat_id,
            file_id=file_id,
            local_path=local_path,
            caption=caption,
        )

    async def send_photo(
        self,
        *,
        chat_id: int,
        file_id: str | None = None,
        local_path: Path | None = None,
        caption: str | None = None,
    ) -> dict[str, Any]:
        return await self._send_media(
            method="sendPhoto",
            field="photo",
            chat_id=chat_id,
            file_id=file_id,
            local_path=local_path,
            caption=caption,
        )

    async def send_document(
        self,
        *,
        chat_id: int,
        file_id: str | None = None,
        local_path: Path | None = None,
        caption: str | None = None,
    ) -> dict[str, Any]:
        return await self._send_media(
            method="sendDocument",
            field="document",
            chat_id=chat_id,
            file_id=file_id,
            local_path=local_path,
            caption=caption,
        )

    async def _send_media(
        self,
        *,
        method: str,
        field: _MediaField,
        chat_id: int,
        file_id: str | None,
        local_path: Path | None,
        caption: str | None,
    ) -> dict[str, Any]:
        if (file_id is None) == (local_path is None):
            raise ValueError(
                "send_media requires exactly one of file_id / local_path"
            )
        if not self._is_token_configured():
            raise TelegramMediaSendError("missing_bot_token")

        url = f"{self._base_url}/bot{self.bot_token}/{method}"
        if file_id is not None:
            body: dict[str, Any] = {"chat_id": chat_id, field: file_id}
            if caption is not None:
                body["caption"] = caption
            return await self._post_json(url=url, body=body, field=field)

        assert local_path is not None
        if not local_path.exists() or not local_path.is_file():
            raise TelegramMediaSendError("local_file_missing")
        data: dict[str, Any] = {"chat_id": str(chat_id)}
        if caption is not None:
            data["caption"] = caption
        with local_path.open("rb") as fp:
            files = {field: (local_path.name, fp.read())}
        return await self._post_multipart(
            url=url, data=data, files=files, field=field
        )

    async def _post_json(
        self, *, url: str, body: dict[str, Any], field: _MediaField
    ) -> dict[str, Any]:
        try:
            async with self._client(timeout=60) as client:
                response = await client.post(url, json=body)
        except httpx.HTTPError:
            raise TelegramMediaSendError(
                "telegram_network_error"
            ) from None
        return self._interpret(response, field=field)

    async def _post_multipart(
        self,
        *,
        url: str,
        data: dict[str, Any],
        files: dict[str, Any],
        field: _MediaField,
    ) -> dict[str, Any]:
        try:
            async with self._client(timeout=60) as client:
                response = await client.post(url, data=data, files=files)
        except httpx.HTTPError:
            raise TelegramMediaSendError(
                "telegram_network_error"
            ) from None
        return self._interpret(response, field=field)

    @staticmethod
    def _interpret(
        response: httpx.Response, *, field: _MediaField
    ) -> dict[str, Any]:
        try:
            payload = response.json()
        except ValueError:
            payload = None
        if not isinstance(payload, dict):
            raise TelegramMediaSendError("telegram_send_failed")
        if response.status_code >= 400 or not payload.get("ok"):
            description = payload.get("description")
            raise TelegramMediaSendError(
                "telegram_send_failed",
                description=(
                    str(description) if description is not None else None
                ),
            )
        file_id = _extract_file_id(payload, field=field)
        return {"ok": True, "telegram_file_id": file_id}

    async def _call_identity_method(
        self, *, method: str, json_body: dict[str, str]
    ) -> dict:
        """POST a setMyX identity update.

        Returns the parsed Bot API envelope (``{"ok": bool, ...}``). Does not
        raise on ``ok=false`` — callers (operator UI, startup sync) need to
        report Telegram's verdict back to the operator rather than crash.
        """
        if not self._is_token_configured():
            return {"ok": False, "skipped": "missing_bot_token"}
        async with self._client(timeout=15) as client:
            response = await client.post(
                f"{self._base_url}/bot{self.bot_token}/{method}",
                json=json_body,
            )
            response.raise_for_status()
            return response.json()

    async def set_my_name(self, *, name: str) -> dict:
        return await self._call_identity_method(
            method="setMyName", json_body={"name": name}
        )

    async def set_my_description(self, *, description: str) -> dict:
        return await self._call_identity_method(
            method="setMyDescription", json_body={"description": description}
        )

    async def set_my_short_description(self, *, short_description: str) -> dict:
        return await self._call_identity_method(
            method="setMyShortDescription",
            json_body={"short_description": short_description},
        )


def _extract_file_id(
    payload: dict[str, Any], *, field: _MediaField
) -> str | None:
    """Pull the freshly assigned file_id out of a Bot API media response.

    ``sendPhoto`` returns a list (``result.photo[]``); the largest size's
    ``file_id`` is the one we want to cache. ``sendVideo`` / ``sendDocument``
    return a single object under ``result.<field>``.
    """
    result = payload.get("result")
    if not isinstance(result, dict):
        return None
    media = result.get(field)
    if field == "photo":
        if not isinstance(media, list) or not media:
            return None
        largest = max(
            (item for item in media if isinstance(item, dict)),
            key=lambda item: int(item.get("file_size") or 0),
            default=None,
        )
        if largest is None:
            return None
        candidate = largest.get("file_id")
        return str(candidate) if isinstance(candidate, str) else None
    if not isinstance(media, dict):
        return None
    candidate = media.get("file_id")
    return str(candidate) if isinstance(candidate, str) else None

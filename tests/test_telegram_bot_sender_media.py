"""Unit tests for ``TelegramBotSender`` media-send methods (Story 12.05).

Three new methods (``send_video`` / ``send_photo`` / ``send_document``) accept
either a cached ``file_id`` (cheap JSON path) or a ``local_path`` (multipart
upload). All raise ``TelegramMediaSendError`` with a categorised reason on a
Telegram-side failure so the api caller can decide whether to fall back.
On success the helper returns ``{ok: True, telegram_file_id: <id>}`` so the
caller can persist the freshly assigned file_id.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx
import pytest

from services.api.app.telegram_bot_sender import (
    TelegramBotSender,
    TelegramMediaSendError,
)


class _StubTransport(httpx.AsyncBaseTransport):
    def __init__(
        self,
        *,
        responses: list[httpx.Response],
    ) -> None:
        self.responses = list(responses)
        self.calls: list[dict[str, Any]] = []

    async def handle_async_request(
        self, request: httpx.Request
    ) -> httpx.Response:
        await request.aread()
        self.calls.append(
            {
                "url": str(request.url),
                "method": request.method,
                "content": request.content,
                "content_type": request.headers.get("content-type", ""),
            }
        )
        return self.responses.pop(0)


def _make_sender(transport: httpx.AsyncBaseTransport) -> TelegramBotSender:
    return TelegramBotSender(
        bot_token="t",
        base_url="https://api.telegram.org",
        http_transport=transport,
    )


def _ok_response(file_id: str, *, method_path: str) -> httpx.Response:
    media_payload: dict[str, Any] | list[dict[str, Any]]
    if method_path == "photo":
        media_payload = [
            {"file_id": "thumb", "file_size": 10},
            {"file_id": file_id, "file_size": 99999},
        ]
    else:
        media_payload = {"file_id": file_id}
    payload = {
        "ok": True,
        "result": {
            "message_id": 555,
            method_path: media_payload,
        },
    }
    return httpx.Response(200, content=json.dumps(payload).encode("utf-8"))


@pytest.mark.asyncio
async def test_send_video_by_file_id_uses_json_path() -> None:
    transport = _StubTransport(
        responses=[_ok_response("VID-123", method_path="video")]
    )
    sender = _make_sender(transport)
    result = await sender.send_video(
        chat_id=42, file_id="OLD-VID", caption="caption"
    )
    assert result["ok"] is True
    assert result["telegram_file_id"] == "VID-123"
    assert transport.calls[0]["url"].endswith("/sendVideo")
    assert "application/json" in transport.calls[0]["content_type"]
    body = json.loads(transport.calls[0]["content"])
    assert body == {"chat_id": 42, "video": "OLD-VID", "caption": "caption"}


@pytest.mark.asyncio
async def test_send_photo_by_file_id_caption_optional() -> None:
    transport = _StubTransport(
        responses=[_ok_response("PHOTO-1", method_path="photo")]
    )
    sender = _make_sender(transport)
    result = await sender.send_photo(chat_id=7, file_id="OLDPH")
    assert result["telegram_file_id"] == "PHOTO-1"
    body = json.loads(transport.calls[0]["content"])
    assert body == {"chat_id": 7, "photo": "OLDPH"}


@pytest.mark.asyncio
async def test_send_document_by_file_id_round_trip() -> None:
    transport = _StubTransport(
        responses=[_ok_response("DOC-1", method_path="document")]
    )
    sender = _make_sender(transport)
    result = await sender.send_document(
        chat_id=7, file_id="OLDDOC", caption="cap"
    )
    assert result["telegram_file_id"] == "DOC-1"


@pytest.mark.asyncio
async def test_send_video_by_local_path_uploads_multipart(
    tmp_path: Path,
) -> None:
    video = tmp_path / "x.mp4"
    video.write_bytes(b"mp4-bytes")
    transport = _StubTransport(
        responses=[_ok_response("FRESH-VID", method_path="video")]
    )
    sender = _make_sender(transport)
    result = await sender.send_video(
        chat_id=10, local_path=video, caption="cap"
    )
    assert result["telegram_file_id"] == "FRESH-VID"
    assert transport.calls[0]["url"].endswith("/sendVideo")
    assert "multipart/form-data" in transport.calls[0]["content_type"]
    body = transport.calls[0]["content"]
    assert b"FRESH-VID" not in body
    assert b"mp4-bytes" in body  # actual file bytes were uploaded
    assert b"caption" in body


@pytest.mark.asyncio
async def test_send_photo_by_local_path_uploads_multipart(
    tmp_path: Path,
) -> None:
    photo = tmp_path / "x.jpg"
    photo.write_bytes(b"jpg-bytes")
    transport = _StubTransport(
        responses=[_ok_response("FRESH-PH", method_path="photo")]
    )
    sender = _make_sender(transport)
    result = await sender.send_photo(chat_id=10, local_path=photo)
    assert result["telegram_file_id"] == "FRESH-PH"
    assert "multipart/form-data" in transport.calls[0]["content_type"]


@pytest.mark.asyncio
async def test_send_document_local_uploads_multipart(
    tmp_path: Path,
) -> None:
    pdf = tmp_path / "x.pdf"
    pdf.write_bytes(b"pdf-bytes")
    transport = _StubTransport(
        responses=[_ok_response("FRESH-DOC", method_path="document")]
    )
    sender = _make_sender(transport)
    result = await sender.send_document(
        chat_id=10, local_path=pdf, caption="hello"
    )
    assert result["telegram_file_id"] == "FRESH-DOC"
    assert "multipart/form-data" in transport.calls[0]["content_type"]


@pytest.mark.asyncio
async def test_send_video_requires_file_id_or_local_path() -> None:
    sender = _make_sender(_StubTransport(responses=[]))
    with pytest.raises(ValueError):
        await sender.send_video(chat_id=1)


@pytest.mark.asyncio
async def test_send_video_telegram_error_raises_media_send_error() -> None:
    transport = _StubTransport(
        responses=[
            httpx.Response(
                400,
                content=json.dumps(
                    {"ok": False, "description": "Bad Request: chat not found"}
                ).encode("utf-8"),
            )
        ]
    )
    sender = _make_sender(transport)
    with pytest.raises(TelegramMediaSendError) as exc_info:
        await sender.send_video(chat_id=1, file_id="X")
    assert exc_info.value.reason == "telegram_send_failed"
    assert "chat not found" in (exc_info.value.description or "")


@pytest.mark.asyncio
async def test_send_video_network_error_raises_media_send_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _ExplodingTransport(httpx.AsyncBaseTransport):
        async def handle_async_request(
            self, request: httpx.Request
        ) -> httpx.Response:
            raise httpx.ConnectError("nope")

    sender = _make_sender(_ExplodingTransport())
    with pytest.raises(TelegramMediaSendError) as exc_info:
        await sender.send_video(chat_id=1, file_id="X")
    assert exc_info.value.reason == "telegram_network_error"


@pytest.mark.asyncio
async def test_send_video_local_missing_file_raises_categorised(
    tmp_path: Path,
) -> None:
    sender = _make_sender(_StubTransport(responses=[]))
    with pytest.raises(TelegramMediaSendError) as exc_info:
        await sender.send_video(
            chat_id=1, local_path=tmp_path / "missing.mp4"
        )
    assert exc_info.value.reason == "local_file_missing"


@pytest.mark.asyncio
async def test_send_video_missing_token_raises_clean() -> None:
    transport = _StubTransport(responses=[])
    sender = TelegramBotSender(
        bot_token="replace-me",
        base_url="https://api.telegram.org",
        http_transport=transport,
    )
    with pytest.raises(TelegramMediaSendError) as exc_info:
        await sender.send_video(chat_id=1, file_id="X")
    assert exc_info.value.reason == "missing_bot_token"


@pytest.mark.asyncio
async def test_send_video_non_json_response_categorised() -> None:
    transport = _StubTransport(
        responses=[httpx.Response(200, content=b"not-json")]
    )
    sender = _make_sender(transport)
    with pytest.raises(TelegramMediaSendError) as exc_info:
        await sender.send_video(chat_id=1, file_id="X")
    assert exc_info.value.reason == "telegram_send_failed"


@pytest.mark.asyncio
async def test_send_local_network_error_categorised(tmp_path: Path) -> None:
    """Multipart upload that drops at the transport level surfaces a
    ``telegram_network_error`` exception."""

    class _ExplodingTransport(httpx.AsyncBaseTransport):
        async def handle_async_request(
            self, request: httpx.Request
        ) -> httpx.Response:
            raise httpx.ConnectError("nope")

    video = tmp_path / "x.mp4"
    video.write_bytes(b"x")
    sender = _make_sender(_ExplodingTransport())
    with pytest.raises(TelegramMediaSendError) as exc_info:
        await sender.send_video(chat_id=1, local_path=video)
    assert exc_info.value.reason == "telegram_network_error"


@pytest.mark.asyncio
async def test_send_video_response_result_not_dict_returns_none_file_id() -> None:
    """Defensive: a malformed ``result`` (string, list) is not parseable."""
    transport = _StubTransport(
        responses=[
            httpx.Response(
                200,
                content=json.dumps(
                    {"ok": True, "result": "not-a-dict"}
                ).encode("utf-8"),
            )
        ]
    )
    sender = _make_sender(transport)
    result = await sender.send_video(chat_id=1, file_id="X")
    assert result == {"ok": True, "telegram_file_id": None}


@pytest.mark.asyncio
async def test_send_photo_empty_sizes_returns_none_file_id() -> None:
    transport = _StubTransport(
        responses=[
            httpx.Response(
                200,
                content=json.dumps(
                    {"ok": True, "result": {"message_id": 1, "photo": []}}
                ).encode("utf-8"),
            )
        ]
    )
    sender = _make_sender(transport)
    result = await sender.send_photo(chat_id=1, file_id="X")
    assert result["telegram_file_id"] is None


@pytest.mark.asyncio
async def test_send_photo_all_non_dict_sizes_returns_none_file_id() -> None:
    transport = _StubTransport(
        responses=[
            httpx.Response(
                200,
                content=json.dumps(
                    {
                        "ok": True,
                        "result": {
                            "message_id": 1,
                            "photo": ["nope", 42, None],
                        },
                    }
                ).encode("utf-8"),
            )
        ]
    )
    sender = _make_sender(transport)
    result = await sender.send_photo(chat_id=1, file_id="X")
    assert result["telegram_file_id"] is None


@pytest.mark.asyncio
async def test_send_video_ok_payload_without_file_id_still_succeeds() -> None:
    """Defensive: an OK response without a parseable file_id still resolves
    to ok=True but ``telegram_file_id`` is ``None`` so the caller doesn't try
    to cache an empty string."""
    transport = _StubTransport(
        responses=[
            httpx.Response(
                200,
                content=json.dumps(
                    {"ok": True, "result": {"message_id": 1}}
                ).encode("utf-8"),
            )
        ]
    )
    sender = _make_sender(transport)
    result = await sender.send_video(chat_id=1, file_id="X")
    assert result["ok"] is True
    assert result["telegram_file_id"] is None

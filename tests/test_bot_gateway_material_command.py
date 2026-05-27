"""Bot-side slash-command tests for ``/material`` (Story 12.05).

Three commands gated identically to ``/service_*``:

* ``/material [caption]`` — must be a reply to a message carrying a
  ``video`` / ``photo`` / ``document`` attachment. The bot downloads the
  binary into ``.data/sales_materials/<project_id>/`` and posts metadata
  + the original ``telegram_file_id`` to ``POST /sales/materials``.
* ``/material_list`` — calls ``GET /sales/materials``; renders one line per
  active row.
* ``/material_remove <id>`` — calls ``DELETE /sales/materials/{id}``.

Validation errors return one Russian line; unauthorized senders are
silently dropped with a structured ``unauthorized_material_command``
log line.
"""

from __future__ import annotations

import json as _json
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import httpx
import pytest

from services.bot_gateway.app.api_client import ApiError
from services.bot_gateway.app.material_command_dispatch import (
    handle_material_command,
)
from services.bot_gateway.app.telegram_update import (
    NormalizedTelegramMessage,
    TelegramAttachment,
)


def _msg(
    text: str,
    *,
    username: str = "@op",
    reply_to_text: str | None = None,
    reply_to_attachment: TelegramAttachment | None = None,
    reply_to_caption: str | None = None,
) -> NormalizedTelegramMessage:
    return NormalizedTelegramMessage(
        update_id=1,
        source_message_id=2,
        chat_id=42,
        user_id=99,
        username=username,
        text=text,
        reply_to_text=reply_to_text,
        reply_to_attachment=reply_to_attachment,
        reply_to_caption=reply_to_caption,
    )


class FakeApi:
    def __init__(self) -> None:
        self.find_operator_by_username = AsyncMock(
            return_value={
                "username": "@op",
                "chat_id": 42,
                "project_id": 7,
                "is_active": True,
            }
        )
        self.add_sales_material = AsyncMock(return_value={"id": 11})
        self.list_sales_materials = AsyncMock(
            return_value={"materials": []}
        )
        self.delete_sales_material = AsyncMock(return_value={"ok": True})


class _Sent:
    def __init__(self) -> None:
        self.calls: list[tuple[int, str]] = []

    async def __call__(self, chat_id: int, text: str) -> None:
        self.calls.append((chat_id, text))


class _FakeDownloader:
    def __init__(self, *, path: Path, byte_size: int = 100) -> None:
        self._path = path
        self._size = byte_size
        self.calls: list[dict[str, Any]] = []

    async def download(
        self,
        *,
        file_id: str,
        suggested_extension: str,
        mime_type: str | None = None,
    ) -> Any:
        self.calls.append(
            {
                "file_id": file_id,
                "suggested_extension": suggested_extension,
                "mime_type": mime_type,
            }
        )

        class _D:
            def __init__(self, *, path: Path, byte_size: int) -> None:
                self.path = path
                self.byte_size = byte_size
                self.mime_type = mime_type

        return _D(path=self._path, byte_size=self._size)


@pytest.mark.asyncio
async def test_material_command_requires_reply(tmp_path: Path) -> None:
    api = FakeApi()
    sent = _Sent()
    downloader = _FakeDownloader(path=tmp_path / "x.mp4")
    result = await handle_material_command(
        normalized=_msg("/material"),
        api_client=api,
        send_dm=sent,
        primary_operator_username="@op",
        admin_username="@admin",
        internal_token="bot-tok",
        downloader_factory=lambda _: downloader,
        storage_root=tmp_path,
    )
    assert result is not None
    assert result["route"] == "material"
    assert result["decision"] == "no_reply"
    assert sent.calls == [
        (
            42,
            (
                "Использование: ответьте на видео/фото/документ командой "
                "/material [подпись]."
            ),
        )
    ]
    api.add_sales_material.assert_not_awaited()


@pytest.mark.asyncio
async def test_material_command_reply_target_has_no_media(
    tmp_path: Path,
) -> None:
    api = FakeApi()
    sent = _Sent()
    result = await handle_material_command(
        normalized=_msg(
            "/material", reply_to_text="just text, no attachment"
        ),
        api_client=api,
        send_dm=sent,
        primary_operator_username="@op",
        admin_username="@admin",
        internal_token="bot-tok",
        downloader_factory=lambda _: _FakeDownloader(path=tmp_path / "x.mp4"),
        storage_root=tmp_path,
    )
    assert result is not None
    assert result["decision"] == "no_media"
    assert sent.calls == [
        (
            42,
            (
                "Использование: ответьте на видео/фото/документ командой "
                "/material [подпись]."
            ),
        )
    ]


@pytest.mark.asyncio
async def test_material_command_video_reply_downloads_and_registers(
    tmp_path: Path,
) -> None:
    api = FakeApi()
    sent = _Sent()
    storage_dir = tmp_path / "sales_materials"
    downloaded = tmp_path / "downloaded.mp4"
    downloaded.write_bytes(b"vid-bytes")
    downloader = _FakeDownloader(path=downloaded, byte_size=9)
    attachment = TelegramAttachment(
        file_id="TG-VID-1", kind="video", mime_type="video/mp4", file_size=9
    )
    result = await handle_material_command(
        normalized=_msg(
            "/material",
            reply_to_attachment=attachment,
        ),
        api_client=api,
        send_dm=sent,
        primary_operator_username="@op",
        admin_username="@admin",
        internal_token="bot-tok",
        downloader_factory=lambda storage_dir: downloader,
        storage_root=storage_dir,
    )
    assert result is not None
    assert result["status"] == "ok"
    assert downloader.calls == [
        {
            "file_id": "TG-VID-1",
            "suggested_extension": "mp4",
            "mime_type": "video/mp4",
        }
    ]
    api.add_sales_material.assert_awaited_once()
    kwargs = api.add_sales_material.await_args.kwargs
    assert kwargs["project_id"] == 7
    assert kwargs["kind"] == "video"
    assert kwargs["byte_size"] == 9
    assert kwargs["telegram_file_id"] == "TG-VID-1"
    assert kwargs["internal_token"] == "bot-tok"
    assert sent.calls == [
        (42, 'Добавлено: video id=11 (caption="")')
    ]


@pytest.mark.asyncio
async def test_material_command_caption_arg_overrides(
    tmp_path: Path,
) -> None:
    api = FakeApi()
    sent = _Sent()
    downloaded = tmp_path / "x.jpg"
    downloaded.write_bytes(b"x")
    attachment = TelegramAttachment(
        file_id="TG-PH-1", kind="photo", mime_type="image/jpeg", file_size=1
    )
    await handle_material_command(
        normalized=_msg(
            "/material Гора Ачишхо на закате",
            reply_to_attachment=attachment,
            reply_to_caption="original caption",
        ),
        api_client=api,
        send_dm=sent,
        primary_operator_username="@op",
        admin_username="@admin",
        internal_token="bot-tok",
        downloader_factory=lambda _: _FakeDownloader(path=downloaded),
        storage_root=tmp_path,
    )
    kwargs = api.add_sales_material.await_args.kwargs
    assert kwargs["caption"] == "Гора Ачишхо на закате"
    assert sent.calls == [
        (42, 'Добавлено: photo id=11 (caption="Гора Ачишхо на закате")')
    ]


@pytest.mark.asyncio
async def test_material_command_falls_back_to_reply_caption_when_no_arg(
    tmp_path: Path,
) -> None:
    api = FakeApi()
    sent = _Sent()
    downloaded = tmp_path / "x.mp4"
    downloaded.write_bytes(b"x")
    attachment = TelegramAttachment(
        file_id="TG-V", kind="video", mime_type="video/mp4", file_size=1
    )
    await handle_material_command(
        normalized=_msg(
            "/material",
            reply_to_attachment=attachment,
            reply_to_caption="оригинальная подпись",
        ),
        api_client=api,
        send_dm=sent,
        primary_operator_username="@op",
        admin_username="@admin",
        internal_token="bot-tok",
        downloader_factory=lambda _: _FakeDownloader(path=downloaded),
        storage_root=tmp_path,
    )
    kwargs = api.add_sales_material.await_args.kwargs
    assert kwargs["caption"] == "оригинальная подпись"


@pytest.mark.asyncio
async def test_material_command_document_pdf_routes_kind_pdf(
    tmp_path: Path,
) -> None:
    api = FakeApi()
    sent = _Sent()
    downloaded = tmp_path / "x.pdf"
    downloaded.write_bytes(b"x")
    attachment = TelegramAttachment(
        file_id="TG-DOC",
        kind="document",
        mime_type="application/pdf",
        file_size=1,
        file_name="catalog.pdf",
    )
    await handle_material_command(
        normalized=_msg(
            "/material каталог",
            reply_to_attachment=attachment,
        ),
        api_client=api,
        send_dm=sent,
        primary_operator_username="@op",
        admin_username="@admin",
        internal_token="bot-tok",
        downloader_factory=lambda _: _FakeDownloader(path=downloaded),
        storage_root=tmp_path,
    )
    kwargs = api.add_sales_material.await_args.kwargs
    assert kwargs["kind"] == "pdf"


@pytest.mark.asyncio
async def test_material_command_unsupported_attachment_rejected(
    tmp_path: Path,
) -> None:
    api = FakeApi()
    sent = _Sent()
    attachment = TelegramAttachment(
        file_id="TG-AUD", kind="audio", mime_type="audio/mp3", file_size=1
    )
    result = await handle_material_command(
        normalized=_msg("/material", reply_to_attachment=attachment),
        api_client=api,
        send_dm=sent,
        primary_operator_username="@op",
        admin_username="@admin",
        internal_token="bot-tok",
        downloader_factory=lambda _: _FakeDownloader(path=tmp_path / "x"),
        storage_root=tmp_path,
    )
    assert result is not None
    assert result["decision"] == "unsupported_attachment"
    api.add_sales_material.assert_not_awaited()


@pytest.mark.asyncio
async def test_material_command_unauthorized_silently_dropped(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    api = FakeApi()
    api.find_operator_by_username = AsyncMock(return_value=None)
    sent = _Sent()
    attachment = TelegramAttachment(
        file_id="TG-V", kind="video", mime_type="video/mp4", file_size=1
    )
    result = await handle_material_command(
        normalized=_msg(
            "/material caption",
            username="@stranger",
            reply_to_attachment=attachment,
        ),
        api_client=api,
        send_dm=sent,
        primary_operator_username="@op",
        admin_username="@admin",
        internal_token="bot-tok",
        downloader_factory=lambda _: _FakeDownloader(path=tmp_path / "x"),
        storage_root=tmp_path,
    )
    assert result is not None
    assert result["status"] == "ignored"
    assert result["reason"] == "unauthorized_material_command"
    assert sent.calls == []
    api.add_sales_material.assert_not_awaited()


@pytest.mark.asyncio
async def test_material_list_renders_active_rows(tmp_path: Path) -> None:
    api = FakeApi()
    api.list_sales_materials = AsyncMock(
        return_value={
            "materials": [
                {
                    "id": 1,
                    "kind": "video",
                    "caption": "Гора Ачишхо",
                    "tags": ["tour_preview"],
                    "is_active": True,
                },
                {
                    "id": 2,
                    "kind": "photo",
                    "caption": None,
                    "tags": [],
                    "is_active": True,
                },
            ]
        }
    )
    sent = _Sent()
    result = await handle_material_command(
        normalized=_msg("/material_list"),
        api_client=api,
        send_dm=sent,
        primary_operator_username="@op",
        admin_username="@admin",
        internal_token="bot-tok",
        downloader_factory=lambda _: _FakeDownloader(path=tmp_path / "x"),
        storage_root=tmp_path,
    )
    assert result is not None
    assert result["status"] == "ok"
    assert result["route"] == "material_list"
    assert "1. video" in sent.calls[0][1]
    assert "Гора Ачишхо" in sent.calls[0][1]


@pytest.mark.asyncio
async def test_material_list_empty_shows_hint(tmp_path: Path) -> None:
    api = FakeApi()
    sent = _Sent()
    await handle_material_command(
        normalized=_msg("/material_list"),
        api_client=api,
        send_dm=sent,
        primary_operator_username="@op",
        admin_username="@admin",
        internal_token="bot-tok",
        downloader_factory=lambda _: _FakeDownloader(path=tmp_path / "x"),
        storage_root=tmp_path,
    )
    assert sent.calls == [(42, "Материалов пока нет.")]


@pytest.mark.asyncio
async def test_material_remove_calls_delete_endpoint(
    tmp_path: Path,
) -> None:
    api = FakeApi()
    sent = _Sent()
    result = await handle_material_command(
        normalized=_msg("/material_remove 7"),
        api_client=api,
        send_dm=sent,
        primary_operator_username="@op",
        admin_username="@admin",
        internal_token="bot-tok",
        downloader_factory=lambda _: _FakeDownloader(path=tmp_path / "x"),
        storage_root=tmp_path,
    )
    assert result is not None
    assert result["status"] == "ok"
    api.delete_sales_material.assert_awaited_once_with(
        material_id=7, internal_token="bot-tok"
    )
    assert sent.calls == [(42, "Удалено: id=7")]


@pytest.mark.asyncio
async def test_material_remove_unknown_returns_one_line_error(
    tmp_path: Path,
) -> None:
    api = FakeApi()
    api.delete_sales_material = AsyncMock(
        side_effect=ApiError(
            "err",
            request=httpx.Request("DELETE", "http://api/x"),
            response=httpx.Response(
                404,
                content=_json.dumps(
                    {"detail": "material_not_found"}
                ).encode(),
                request=httpx.Request("DELETE", "http://api/x"),
            ),
            detail="material_not_found",
        )
    )
    sent = _Sent()
    await handle_material_command(
        normalized=_msg("/material_remove 99"),
        api_client=api,
        send_dm=sent,
        primary_operator_username="@op",
        admin_username="@admin",
        internal_token="bot-tok",
        downloader_factory=lambda _: _FakeDownloader(path=tmp_path / "x"),
        storage_root=tmp_path,
    )
    assert sent.calls == [(42, "Не найдено: id=99")]


@pytest.mark.asyncio
async def test_material_remove_invalid_id_returns_usage(
    tmp_path: Path,
) -> None:
    api = FakeApi()
    sent = _Sent()
    await handle_material_command(
        normalized=_msg("/material_remove not-a-number"),
        api_client=api,
        send_dm=sent,
        primary_operator_username="@op",
        admin_username="@admin",
        internal_token="bot-tok",
        downloader_factory=lambda _: _FakeDownloader(path=tmp_path / "x"),
        storage_root=tmp_path,
    )
    assert sent.calls == [(42, "Использование: /material_remove <id>")]
    api.delete_sales_material.assert_not_awaited()


@pytest.mark.asyncio
async def test_material_command_admin_without_project_mapping_dropped(
    tmp_path: Path,
) -> None:
    api = FakeApi()
    api.find_operator_by_username = AsyncMock(return_value=None)
    sent = _Sent()
    attachment = TelegramAttachment(
        file_id="TG-V", kind="video", mime_type="video/mp4", file_size=1
    )
    result = await handle_material_command(
        normalized=_msg(
            "/material",
            username="@admin",
            reply_to_attachment=attachment,
        ),
        api_client=api,
        send_dm=sent,
        primary_operator_username="@op",
        admin_username="@admin",
        internal_token="bot-tok",
        downloader_factory=lambda _: _FakeDownloader(path=tmp_path / "x"),
        storage_root=tmp_path,
    )
    assert result is not None
    assert result["status"] == "ignored"


@pytest.mark.asyncio
async def test_material_command_document_non_pdf_kind_document(
    tmp_path: Path,
) -> None:
    api = FakeApi()
    sent = _Sent()
    downloaded = tmp_path / "x.docx"
    downloaded.write_bytes(b"x")
    attachment = TelegramAttachment(
        file_id="TG-DOCX",
        kind="document",
        mime_type=(
            "application/vnd.openxmlformats-officedocument."
            "wordprocessingml.document"
        ),
        file_size=1,
        file_name="brochure.docx",
    )
    await handle_material_command(
        normalized=_msg("/material", reply_to_attachment=attachment),
        api_client=api,
        send_dm=sent,
        primary_operator_username="@op",
        admin_username="@admin",
        internal_token="bot-tok",
        downloader_factory=lambda _: _FakeDownloader(path=downloaded),
        storage_root=tmp_path,
    )
    assert api.add_sales_material.await_args.kwargs["kind"] == "document"


@pytest.mark.asyncio
async def test_material_command_caption_too_long_rejected(
    tmp_path: Path,
) -> None:
    api = FakeApi()
    sent = _Sent()
    attachment = TelegramAttachment(
        file_id="TG-V", kind="video", mime_type="video/mp4", file_size=1
    )
    over = "Я" * 201
    result = await handle_material_command(
        normalized=_msg(
            f"/material {over}", reply_to_attachment=attachment
        ),
        api_client=api,
        send_dm=sent,
        primary_operator_username="@op",
        admin_username="@admin",
        internal_token="bot-tok",
        downloader_factory=lambda _: _FakeDownloader(path=tmp_path / "x.mp4"),
        storage_root=tmp_path,
    )
    assert result is not None
    assert result["decision"] == "caption_too_long"
    api.add_sales_material.assert_not_awaited()


@pytest.mark.asyncio
async def test_material_command_download_failure_returns_message(
    tmp_path: Path,
) -> None:
    api = FakeApi()
    sent = _Sent()

    class _ExplodingDownloader:
        async def download(self, **_: Any) -> Any:
            raise RuntimeError("boom")

    attachment = TelegramAttachment(
        file_id="TG-V", kind="video", mime_type="video/mp4", file_size=1
    )
    result = await handle_material_command(
        normalized=_msg("/material", reply_to_attachment=attachment),
        api_client=api,
        send_dm=sent,
        primary_operator_username="@op",
        admin_username="@admin",
        internal_token="bot-tok",
        downloader_factory=lambda _: _ExplodingDownloader(),
        storage_root=tmp_path,
    )
    assert result is not None
    assert result["decision"] == "download_failed"
    assert "Не удалось скачать" in sent.calls[0][1]
    api.add_sales_material.assert_not_awaited()


@pytest.mark.asyncio
async def test_material_command_api_error_falls_through_to_generic_dm(
    tmp_path: Path,
) -> None:
    api = FakeApi()
    api.add_sales_material = AsyncMock(
        side_effect=ApiError(
            "err",
            request=httpx.Request("POST", "http://api/x"),
            response=httpx.Response(
                500,
                content=_json.dumps({"detail": "boom"}).encode(),
                request=httpx.Request("POST", "http://api/x"),
            ),
            detail="boom",
        )
    )
    sent = _Sent()
    downloaded = tmp_path / "x.mp4"
    downloaded.write_bytes(b"x")
    attachment = TelegramAttachment(
        file_id="TG-V", kind="video", mime_type="video/mp4", file_size=1
    )
    result = await handle_material_command(
        normalized=_msg("/material", reply_to_attachment=attachment),
        api_client=api,
        send_dm=sent,
        primary_operator_username="@op",
        admin_username="@admin",
        internal_token="bot-tok",
        downloader_factory=lambda _: _FakeDownloader(path=downloaded),
        storage_root=tmp_path,
    )
    assert result is not None
    assert result["status"] == "error"
    assert sent.calls[0][1] == "Сервис временно недоступен, попробуйте позже."


@pytest.mark.asyncio
async def test_material_list_api_error_falls_through_to_generic_dm(
    tmp_path: Path,
) -> None:
    api = FakeApi()
    api.list_sales_materials = AsyncMock(
        side_effect=httpx.RequestError("network down")
    )
    sent = _Sent()
    result = await handle_material_command(
        normalized=_msg("/material_list"),
        api_client=api,
        send_dm=sent,
        primary_operator_username="@op",
        admin_username="@admin",
        internal_token="bot-tok",
        downloader_factory=lambda _: _FakeDownloader(path=tmp_path / "x"),
        storage_root=tmp_path,
    )
    assert result is not None
    assert result["status"] == "error"


@pytest.mark.asyncio
async def test_material_remove_zero_id_returns_usage(tmp_path: Path) -> None:
    api = FakeApi()
    sent = _Sent()
    await handle_material_command(
        normalized=_msg("/material_remove 0"),
        api_client=api,
        send_dm=sent,
        primary_operator_username="@op",
        admin_username="@admin",
        internal_token="bot-tok",
        downloader_factory=lambda _: _FakeDownloader(path=tmp_path / "x"),
        storage_root=tmp_path,
    )
    assert sent.calls == [(42, "Использование: /material_remove <id>")]
    api.delete_sales_material.assert_not_awaited()


@pytest.mark.asyncio
async def test_material_remove_other_api_error_falls_through(
    tmp_path: Path,
) -> None:
    api = FakeApi()
    api.delete_sales_material = AsyncMock(
        side_effect=ApiError(
            "err",
            request=httpx.Request("DELETE", "http://api/x"),
            response=httpx.Response(
                500,
                content=_json.dumps({"detail": "boom"}).encode(),
                request=httpx.Request("DELETE", "http://api/x"),
            ),
            detail="boom",
        )
    )
    sent = _Sent()
    result = await handle_material_command(
        normalized=_msg("/material_remove 7"),
        api_client=api,
        send_dm=sent,
        primary_operator_username="@op",
        admin_username="@admin",
        internal_token="bot-tok",
        downloader_factory=lambda _: _FakeDownloader(path=tmp_path / "x"),
        storage_root=tmp_path,
    )
    assert result is not None
    assert result["detail"] == "boom"
    assert sent.calls[0][1] == "Сервис временно недоступен, попробуйте позже."


@pytest.mark.asyncio
async def test_material_remove_httpx_status_error_falls_through(
    tmp_path: Path,
) -> None:
    api = FakeApi()
    request = httpx.Request("DELETE", "http://api/x")
    api.delete_sales_material = AsyncMock(
        side_effect=httpx.HTTPStatusError(
            "boom",
            request=request,
            response=httpx.Response(503, request=request),
        )
    )
    sent = _Sent()
    result = await handle_material_command(
        normalized=_msg("/material_remove 7"),
        api_client=api,
        send_dm=sent,
        primary_operator_username="@op",
        admin_username="@admin",
        internal_token="bot-tok",
        downloader_factory=lambda _: _FakeDownloader(path=tmp_path / "x"),
        storage_root=tmp_path,
    )
    assert result is not None
    assert result["status"] == "error"


@pytest.mark.asyncio
async def test_material_command_factory_receives_per_project_storage_dir(
    tmp_path: Path,
) -> None:
    """Spec exit criterion: files MUST land under
    ``<storage_root>/<project_id>/`` — the factory is invoked with the
    per-project subdir so the downloader writes into the right place."""
    api = FakeApi()
    sent = _Sent()
    storage_root = tmp_path / "sales_materials"
    downloaded = storage_root / "7" / "vid.mp4"
    downloaded.parent.mkdir(parents=True, exist_ok=True)
    downloaded.write_bytes(b"x")
    factory_storage_dirs: list[Path] = []

    def factory(storage_dir: Path) -> _FakeDownloader:
        factory_storage_dirs.append(storage_dir)
        return _FakeDownloader(path=downloaded, byte_size=1)

    attachment = TelegramAttachment(
        file_id="TG-V", kind="video", mime_type="video/mp4", file_size=1
    )
    result = await handle_material_command(
        normalized=_msg("/material", reply_to_attachment=attachment),
        api_client=api,
        send_dm=sent,
        primary_operator_username="@op",
        admin_username="@admin",
        internal_token="bot-tok",
        downloader_factory=factory,
        storage_root=storage_root,
    )
    assert result is not None
    assert result["status"] == "ok"
    assert factory_storage_dirs == [storage_root / "7"]


@pytest.mark.asyncio
async def test_non_material_command_returns_none(tmp_path: Path) -> None:
    api = FakeApi()
    sent = _Sent()
    result = await handle_material_command(
        normalized=_msg("/service_add foo"),
        api_client=api,
        send_dm=sent,
        primary_operator_username="@op",
        admin_username="@admin",
        internal_token="bot-tok",
        downloader_factory=lambda _: _FakeDownloader(path=tmp_path / "x"),
        storage_root=tmp_path,
    )
    assert result is None

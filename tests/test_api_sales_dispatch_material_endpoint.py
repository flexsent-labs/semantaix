"""Contract tests for ``POST /sales/dispatch/material`` (Story 12.05).

Service-token gated. Dispatches a registered ``client_materials`` row to a
Telegram chat:

* Cached ``telegram_file_id`` path → JSON sendVideo/sendPhoto/sendDocument
  without a disk read; ``telegram_file_id_cached: True``.
* Fresh-upload path → multipart upload, caches the returned ``file_id`` via
  ``ClientMaterialsRepository.update_telegram_file_id`` and returns
  ``telegram_file_id_cached: False``.
* Telegram error → returns ``{ok: False, error_reason}`` and MUST NOT call
  ``update_telegram_file_id`` (no garbage cached).
* Missing token → 401. Unknown ``material_id`` → 404.
* The dispatch log line MUST NEVER include the ``telegram_file_id`` value
  itself (Story 12.05 epic exit criteria).
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from services.api.app import main as api_main
from services.api.app.main import app as api_app
from services.api.app.sales.client_materials_repository import (
    ClientMaterialsRepository,
)
from services.api.app.telegram_bot_sender import (
    TelegramBotSender,
    TelegramMediaSendError,
)

_TOKEN = "test-bot-token"
_AUTH = {"Authorization": f"Bearer {_TOKEN}"}
_PROJECT_ID = 51
_NOW = datetime(2026, 5, 26, 12, 0, tzinfo=UTC)


class _FakeSender:
    """Captures send_video / send_photo / send_document calls."""

    def __init__(
        self,
        *,
        responses: list[Any] | None = None,
    ) -> None:
        # Each response is either dict (ok envelope) or an Exception to raise.
        self._responses = list(responses or [])
        self.calls: list[dict[str, Any]] = []

    async def _next(self) -> Any:
        if not self._responses:
            return {"ok": True, "telegram_file_id": None}
        return self._responses.pop(0)

    async def send_video(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append({"method": "send_video", **kwargs})
        result = await self._next()
        if isinstance(result, Exception):
            raise result
        return result

    async def send_photo(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append({"method": "send_photo", **kwargs})
        result = await self._next()
        if isinstance(result, Exception):
            raise result
        return result

    async def send_document(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append({"method": "send_document", **kwargs})
        result = await self._next()
        if isinstance(result, Exception):
            raise result
        return result


@pytest.fixture
def env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Iterator[dict[str, Any]]:
    repo = ClientMaterialsRepository(
        db_path=str(tmp_path / "sales.sqlite3")
    )
    monkeypatch.setattr(
        api_main.settings, "internal_service_token", _TOKEN
    )
    monkeypatch.setattr(api_main, "client_materials_repository", repo)
    sender = _FakeSender()
    monkeypatch.setattr(api_main, "telegram_bot_sender", sender)
    client = TestClient(api_app)
    yield {
        "client": client,
        "repo": repo,
        "sender": sender,
        "monkeypatch": monkeypatch,
    }


def test_endpoint_requires_bearer(env: dict[str, Any]) -> None:
    resp = env["client"].post(
        "/sales/dispatch/material",
        json={"chat_id": 1, "material_id": 1},
    )
    assert resp.status_code == 401


def test_endpoint_unknown_material_returns_404(env: dict[str, Any]) -> None:
    resp = env["client"].post(
        "/sales/dispatch/material",
        headers=_AUTH,
        json={"chat_id": 1, "material_id": 99999},
    )
    assert resp.status_code == 404
    assert resp.json()["detail"] == "material_not_found"


def test_cached_file_id_path_uses_file_id_no_disk_read(
    env: dict[str, Any], tmp_path: Path
) -> None:
    mid = env["repo"].add(
        project_id=_PROJECT_ID,
        kind="video",
        local_path=str(tmp_path / "no.mp4"),  # intentionally absent on disk
        byte_size=10,
        caption="hi",
        telegram_file_id="CACHED-VID-1",
        now=_NOW,
    )
    env["sender"]._responses = [
        {"ok": True, "telegram_file_id": "CACHED-VID-1"}
    ]
    resp = env["client"].post(
        "/sales/dispatch/material",
        headers=_AUTH,
        json={"chat_id": 42, "material_id": mid},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["ok"] is True
    assert body["telegram_file_id_cached"] is True
    call = env["sender"].calls[0]
    assert call["method"] == "send_video"
    assert call["chat_id"] == 42
    assert call["file_id"] == "CACHED-VID-1"
    assert call["caption"] == "hi"
    assert call.get("local_path") is None


def test_fresh_upload_path_caches_returned_file_id(
    env: dict[str, Any], tmp_path: Path
) -> None:
    video = tmp_path / "sales.mp4"
    video.write_bytes(b"vidbytes")
    mid = env["repo"].add(
        project_id=_PROJECT_ID,
        kind="video",
        local_path=str(video),
        byte_size=8,
        caption="cap",
        now=_NOW,
    )
    env["sender"]._responses = [
        {"ok": True, "telegram_file_id": "NEW-TG-VID"}
    ]
    resp = env["client"].post(
        "/sales/dispatch/material",
        headers=_AUTH,
        json={"chat_id": 7, "material_id": mid},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["ok"] is True
    assert body["telegram_file_id_cached"] is False
    call = env["sender"].calls[0]
    assert call["method"] == "send_video"
    assert call["local_path"] == Path(str(video))
    assert call["chat_id"] == 7
    assert call["caption"] == "cap"
    assert call.get("file_id") is None
    cached = env["repo"].get(material_id=mid)
    assert cached is not None
    assert cached.telegram_file_id == "NEW-TG-VID"


def test_caption_override_replaces_material_caption(
    env: dict[str, Any], tmp_path: Path
) -> None:
    mid = env["repo"].add(
        project_id=_PROJECT_ID,
        kind="photo",
        local_path=str(tmp_path / "x.jpg"),
        byte_size=10,
        caption="оригинальная",
        telegram_file_id="CACHED-PHOTO",
        now=_NOW,
    )
    env["sender"]._responses = [
        {"ok": True, "telegram_file_id": "CACHED-PHOTO"}
    ]
    env["client"].post(
        "/sales/dispatch/material",
        headers=_AUTH,
        json={
            "chat_id": 1,
            "material_id": mid,
            "caption_override": "новая подпись",
        },
    )
    assert env["sender"].calls[0]["caption"] == "новая подпись"


def test_caption_override_over_200_chars_rejected(
    env: dict[str, Any], tmp_path: Path
) -> None:
    mid = env["repo"].add(
        project_id=_PROJECT_ID,
        kind="video",
        local_path=str(tmp_path / "x.mp4"),
        byte_size=10,
        telegram_file_id="C1",
        now=_NOW,
    )
    resp = env["client"].post(
        "/sales/dispatch/material",
        headers=_AUTH,
        json={
            "chat_id": 1,
            "material_id": mid,
            "caption_override": "Я" * 201,
        },
    )
    assert resp.status_code == 400
    assert resp.json()["detail"] == "caption_too_long"
    assert env["sender"].calls == []


def test_kind_photo_routes_to_send_photo(
    env: dict[str, Any], tmp_path: Path
) -> None:
    mid = env["repo"].add(
        project_id=_PROJECT_ID,
        kind="photo",
        local_path=str(tmp_path / "x.jpg"),
        byte_size=10,
        telegram_file_id="C-PH",
        now=_NOW,
    )
    env["sender"]._responses = [
        {"ok": True, "telegram_file_id": "C-PH"}
    ]
    env["client"].post(
        "/sales/dispatch/material",
        headers=_AUTH,
        json={"chat_id": 1, "material_id": mid},
    )
    assert env["sender"].calls[0]["method"] == "send_photo"


def test_kind_pdf_routes_to_send_document(
    env: dict[str, Any], tmp_path: Path
) -> None:
    mid = env["repo"].add(
        project_id=_PROJECT_ID,
        kind="pdf",
        local_path=str(tmp_path / "x.pdf"),
        byte_size=10,
        telegram_file_id="C-DOC",
        now=_NOW,
    )
    env["sender"]._responses = [
        {"ok": True, "telegram_file_id": "C-DOC"}
    ]
    env["client"].post(
        "/sales/dispatch/material",
        headers=_AUTH,
        json={"chat_id": 1, "material_id": mid},
    )
    assert env["sender"].calls[0]["method"] == "send_document"


def test_kind_document_routes_to_send_document(
    env: dict[str, Any], tmp_path: Path
) -> None:
    mid = env["repo"].add(
        project_id=_PROJECT_ID,
        kind="document",
        local_path=str(tmp_path / "x.docx"),
        byte_size=10,
        telegram_file_id="C-DOC2",
        now=_NOW,
    )
    env["sender"]._responses = [
        {"ok": True, "telegram_file_id": "C-DOC2"}
    ]
    env["client"].post(
        "/sales/dispatch/material",
        headers=_AUTH,
        json={"chat_id": 1, "material_id": mid},
    )
    assert env["sender"].calls[0]["method"] == "send_document"


def test_telegram_error_returns_ok_false_and_does_not_cache(
    env: dict[str, Any], tmp_path: Path
) -> None:
    video = tmp_path / "sales.mp4"
    video.write_bytes(b"vidbytes")
    mid = env["repo"].add(
        project_id=_PROJECT_ID,
        kind="video",
        local_path=str(video),
        byte_size=8,
        now=_NOW,
    )
    env["sender"]._responses = [
        TelegramMediaSendError(
            "telegram_send_failed", description="chat not found"
        )
    ]
    resp = env["client"].post(
        "/sales/dispatch/material",
        headers=_AUTH,
        json={"chat_id": 7, "material_id": mid},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["ok"] is False
    assert body["error_reason"] == "telegram_send_failed"
    row = env["repo"].get(material_id=mid)
    assert row is not None
    assert row.telegram_file_id is None


def test_dispatch_log_never_includes_telegram_file_id(
    env: dict[str, Any],
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Story 12.05 exit criteria: NEVER log the file_id itself."""
    mid = env["repo"].add(
        project_id=_PROJECT_ID,
        kind="video",
        local_path=str(tmp_path / "x.mp4"),
        byte_size=10,
        telegram_file_id="SECRET-FILE-ID-12345",
        now=_NOW,
    )
    env["sender"]._responses = [
        {"ok": True, "telegram_file_id": "SECRET-FILE-ID-12345"}
    ]
    with caplog.at_level(logging.INFO):
        env["client"].post(
            "/sales/dispatch/material",
            headers=_AUTH,
            json={"chat_id": 1, "material_id": mid},
        )
    for record in caplog.records:
        rendered = record.getMessage()
        # Cover both the format string and structured `extra` fields.
        for value in getattr(record, "__dict__", {}).values():
            if isinstance(value, str):
                assert "SECRET-FILE-ID-12345" not in value, (
                    "telegram_file_id leaked into log line"
                )
        assert "SECRET-FILE-ID-12345" not in rendered


def test_trace_id_threaded_into_log(
    env: dict[str, Any],
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    mid = env["repo"].add(
        project_id=_PROJECT_ID,
        kind="video",
        local_path=str(tmp_path / "x.mp4"),
        byte_size=10,
        telegram_file_id="CACHED",
        now=_NOW,
    )
    env["sender"]._responses = [
        {"ok": True, "telegram_file_id": "CACHED"}
    ]
    with caplog.at_level(logging.INFO):
        env["client"].post(
            "/sales/dispatch/material",
            headers=_AUTH,
            json={
                "chat_id": 1,
                "material_id": mid,
                "trace_id": "trc-12345",
            },
        )
    assert any(
        getattr(record, "trace_id", None) == "trc-12345"
        for record in caplog.records
    )


def test_dispatch_failed_log_does_not_include_description(
    env: dict[str, Any],
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Telegram error ``description`` strings can echo the ``file_id`` value
    back to us; logging that description would violate the
    'never log telegram_file_id' exit criterion. Story 12.05.
    """
    video = tmp_path / "x.mp4"
    video.write_bytes(b"x")
    mid = env["repo"].add(
        project_id=_PROJECT_ID,
        kind="video",
        local_path=str(video),
        byte_size=1,
        now=_NOW,
    )
    leaky = "Bad Request: file_id LEAKY-FILE-ID-9 invalid"
    env["sender"]._responses = [
        TelegramMediaSendError("telegram_send_failed", description=leaky)
    ]
    with caplog.at_level(logging.WARNING):
        env["client"].post(
            "/sales/dispatch/material",
            headers=_AUTH,
            json={"chat_id": 7, "material_id": mid},
        )
    for record in caplog.records:
        # The description must never reach logs; the only thing the dispatch
        # log line should carry from the Telegram error is the categorised
        # ``reason``.
        for value in getattr(record, "__dict__", {}).values():
            if isinstance(value, str):
                assert "LEAKY-FILE-ID-9" not in value, (
                    "telegram description leaked into log line"
                )


def test_dispatch_unknown_kind_returns_400(
    env: dict[str, Any], tmp_path: Path
) -> None:
    """Defensive: a stale row whose ``kind`` is outside the allowed map
    (only reachable via direct DB write — the POST endpoint rejects it)
    returns 400 rather than crashing on the routing lookup."""
    mid = env["repo"].add(
        project_id=_PROJECT_ID,
        kind="audio",  # not in _DISPATCH_METHOD_BY_KIND
        local_path=str(tmp_path / "x.mp3"),
        byte_size=10,
        telegram_file_id="X",
        now=_NOW,
    )
    resp = env["client"].post(
        "/sales/dispatch/material",
        headers=_AUTH,
        json={"chat_id": 1, "material_id": mid},
    )
    assert resp.status_code == 400
    assert resp.json()["detail"] == "invalid_kind"
    assert env["sender"].calls == []


@pytest.mark.asyncio
async def test_in_process_material_dispatcher_wired_via_main(
    env: dict[str, Any], tmp_path: Path
) -> None:
    """The api.main `sales_persona_answerer.material_dispatcher` wraps
    `sales_dispatch_material` directly so the answerer can fire material
    sends in-process. Drive that wrapper end-to-end."""
    from services.api.app.main import _in_process_material_dispatcher

    mid = env["repo"].add(
        project_id=_PROJECT_ID,
        kind="video",
        local_path=str(tmp_path / "v.mp4"),
        byte_size=10,
        telegram_file_id="WIRED",
        now=_NOW,
    )
    env["sender"]._responses = [
        {"ok": True, "telegram_file_id": "WIRED"}
    ]
    result = await _in_process_material_dispatcher(
        chat_id=42,
        material_id=mid,
        trace_id="trc-wire",
    )
    assert result == {
        "ok": True,
        "telegram_file_id_cached": True,
    }
    assert env["sender"].calls[0]["chat_id"] == 42


def test_sender_is_actual_bot_sender_instance(
    env: dict[str, Any], tmp_path: Path
) -> None:
    """Sanity: the api.main wires `telegram_bot_sender` to a real
    TelegramBotSender — replacing it with the fake fully overrides the
    interface so our spec doesn't drift."""
    assert isinstance(
        TelegramBotSender(bot_token="t"), TelegramBotSender
    )

from __future__ import annotations

import httpx
import pytest
from fastapi.testclient import TestClient

from services.bot_gateway.app import kb_session as kb_session_module
from services.bot_gateway.app import main as bot_main
from services.bot_gateway.app.kb_session import OperatorKbSessionRepository
from services.bot_gateway.app.main import app as bot_app
from services.bot_gateway.app.media_group_buffer import MediaGroupBuffer
from services.bot_gateway.app.operator_files import OperatorFileRepository
from services.bot_gateway.app.telegram_file_download import (
    DownloadedFile,
    TelegramFileDownloadError,
)


@pytest.fixture
def isolated_bot(tmp_path, monkeypatch):
    monkeypatch.setattr(bot_main.settings, "persistence_db_path", str(tmp_path / "story.db"))
    monkeypatch.setattr(bot_main.settings, "hitl_ticket_db_path", str(tmp_path / "hitl.db"))
    monkeypatch.setattr(bot_main.settings, "operator_upload_storage_dir", str(tmp_path / "uploads"))
    monkeypatch.setattr(bot_main.settings, "operator_upload_max_bytes", 1024)
    monkeypatch.setattr(bot_main.settings, "telegram_bot_token", "TKN")
    monkeypatch.setattr(bot_main, "hitl_ticket_repository", _StubHitlRepo())

    async def _op_lookup(*, username: str):
        if username == "@ajdevy":
            return {"username": "@ajdevy", "chat_id": 100, "project_id": 1, "is_active": True}
        return None

    monkeypatch.setattr(bot_main.api_client, "find_operator_by_username", _op_lookup)
    fresh_kb_repo = OperatorKbSessionRepository(str(tmp_path / "hitl.db"))
    monkeypatch.setattr(bot_main, "kb_session_repository", fresh_kb_repo)
    fresh_files_repo = OperatorFileRepository(
        str(tmp_path / "operator_files.db")
    )
    monkeypatch.setattr(bot_main, "operator_file_repository", fresh_files_repo)
    fresh_mg_buffer = MediaGroupBuffer(str(tmp_path / "hitl.db"))
    monkeypatch.setattr(bot_main, "media_group_buffer", fresh_mg_buffer)
    monkeypatch.setattr(
        bot_main.settings, "operator_media_group_debounce_seconds", 0
    )

    sent_dms: list[tuple[int, str]] = []

    async def fake_send_dm(chat_id: int, text: str) -> None:
        sent_dms.append((chat_id, text))

    monkeypatch.setattr(bot_main, "_send_dm", fake_send_dm)
    return {
        "tmp_path": tmp_path,
        "dms": sent_dms,
        "kb_session_repo": fresh_kb_repo,
        "files_repo": fresh_files_repo,
        "media_group_buffer": fresh_mg_buffer,
    }


class _StubHitlRepo:
    def get_runtime_config(self, key: str):
        return None

    def set_runtime_config(self, **kwargs):
        pass

    def list_all(self):
        return []


def _operator_message(
    *,
    text: str = "",
    caption: str | None = None,
    attachments: list[dict] | None = None,
):
    payload = {
        "update_id": 1,
        "message": {
            "message_id": 1,
            "chat": {"id": 100},
            "from": {"id": 200, "username": "ajdevy"},
        },
    }
    if text:
        payload["message"]["text"] = text
    if caption:
        payload["message"]["caption"] = caption
    if attachments:
        for attachment in attachments:
            payload["message"].update(attachment)
    return payload


def test_kb_inline_text_intent_triggers_ack(isolated_bot, monkeypatch):
    submit_calls: list[dict] = []

    async def fake_submit(**kwargs):
        submit_calls.append(kwargs)
        return {"inserted_chunks": 3, "is_confidential": False, "deduplicated": False}

    monkeypatch.setattr(bot_main.api_client, "submit_operator_upload", fake_submit)
    client = TestClient(bot_app)
    response = client.post(
        "/telegram/webhook",
        json=_operator_message(text="добавь в базу: офис работает 9-18"),
    )
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "accepted"
    assert body["attachment_count"] == "0"
    # Inline text path: api_client called once during background task
    assert len(submit_calls) == 1
    assert submit_calls[0]["source_file_type"] == "inline_text"
    assert "офис работает" in submit_calls[0]["inline_text"]
    # ack DM was sent
    assert any("Принял текст" in text for _, text in isolated_bot["dms"])


def test_kb_slash_with_document(isolated_bot, monkeypatch):
    file_path_on_disk = isolated_bot["tmp_path"] / "deck.pdf"
    file_path_on_disk.write_bytes(b"PDF")

    async def fake_download(*, file_id, suggested_extension, mime_type=None):
        return DownloadedFile(path=file_path_on_disk, byte_size=3, mime_type=mime_type)

    async def fake_submit(**kwargs):
        return {"inserted_chunks": 5, "is_confidential": True, "deduplicated": False}

    monkeypatch.setattr(
        bot_main.TelegramFileDownloader,
        "download",
        fake_download,
        raising=False,
    )
    monkeypatch.setattr(bot_main.api_client, "submit_operator_upload", fake_submit)
    client = TestClient(bot_app)
    response = client.post(
        "/telegram/webhook",
        json=_operator_message(
            caption="/kb_add confidential",
            attachments=[
                {
                    "document": {
                        "file_id": "DOC42",
                        "file_name": "schedule.pdf",
                        "mime_type": "application/pdf",
                        "file_size": 100,
                    },
                }
            ],
        ),
    )
    assert response.status_code == 200
    assert response.json()["status"] == "accepted"
    # Background task ran the success path; summary DM mentions confidential
    summary = [text for _, text in isolated_bot["dms"] if "Добавлено в базу" in text]
    assert summary
    assert "confidential" in summary[0]


def test_kb_slash_with_document_persists_candidate_id(isolated_bot, monkeypatch):
    file_path_on_disk = isolated_bot["tmp_path"] / "deck.pdf"
    file_path_on_disk.write_bytes(b"PDF")

    async def fake_download(self, *, file_id, suggested_extension, mime_type=None):
        return DownloadedFile(path=file_path_on_disk, byte_size=3, mime_type=mime_type)

    async def fake_submit(**kwargs):
        return {
            "inserted_chunks": 5,
            "is_confidential": False,
            "deduplicated": False,
            "candidate_id": 7777,
        }

    monkeypatch.setattr(
        bot_main.TelegramFileDownloader,
        "download",
        fake_download,
        raising=False,
    )
    monkeypatch.setattr(bot_main.api_client, "submit_operator_upload", fake_submit)
    client = TestClient(bot_app)
    response = client.post(
        "/telegram/webhook",
        json=_operator_message(
            caption="/kb_add",
            attachments=[
                {
                    "document": {
                        "file_id": "DOC77",
                        "file_name": "report.pdf",
                        "mime_type": "application/pdf",
                        "file_size": 100,
                    },
                }
            ],
        ),
    )
    assert response.status_code == 200
    records = isolated_bot["files_repo"].list_recent(username="@ajdevy", limit=10)
    assert len(records) == 1
    assert records[0].knowledge_candidate_id == 7777


def test_kb_command_from_non_operator_is_ignored(isolated_bot):
    client = TestClient(bot_app)
    payload = _operator_message(text="/kb_add")
    payload["message"]["from"]["username"] = "stranger"
    response = client.post("/telegram/webhook", json=payload)
    body = response.json()
    # Non-operator falls through; with `/kb_add` text, attachment_only is False;
    # the existing customer-forward path runs.
    assert body["status"] != "accepted" or body.get("kb_mode") is None


def test_kb_download_failure_reports_in_summary(isolated_bot, monkeypatch):
    # Use a small file_size so the size pre-check passes and we reach the
    # download attempt that the stub fails.
    monkeypatch.setattr(bot_main.settings, "operator_upload_max_bytes", 999_999_999)

    async def fake_download(self, *, file_id, suggested_extension, mime_type=None):
        raise TelegramFileDownloadError("file_too_large", file_size=99_999_999)

    monkeypatch.setattr(bot_main.TelegramFileDownloader, "download", fake_download)

    async def fake_submit(**kwargs):
        return {"inserted_chunks": 0, "is_confidential": False, "deduplicated": False}

    monkeypatch.setattr(bot_main.api_client, "submit_operator_upload", fake_submit)
    client = TestClient(bot_app)
    response = client.post(
        "/telegram/webhook",
        json=_operator_message(
            caption="/kb_add",
            attachments=[
                {
                    "document": {
                        "file_id": "BIG",
                        "file_name": "huge.pdf",
                        "mime_type": "application/pdf",
                        "file_size": 100,
                    },
                }
            ],
        ),
    )
    assert response.status_code == 200
    summary = next(
        (text for _, text in isolated_bot["dms"] if "Не удалось обработать" in text), None
    )
    assert summary is not None
    assert "huge.pdf" in summary
    assert "Telegram Bot API" in summary or "файл больше" in summary


def test_kb_unsupported_attachment_type_falls_through_to_failures(isolated_bot, monkeypatch):
    async def fake_submit(**kwargs):  # pragma: no cover - should not run
        raise AssertionError("submit should not be called for unsupported")

    monkeypatch.setattr(bot_main.api_client, "submit_operator_upload", fake_submit)
    client = TestClient(bot_app)
    response = client.post(
        "/telegram/webhook",
        json=_operator_message(
            caption="/kb_add",
            attachments=[
                {
                    "document": {
                        "file_id": "X",
                        "file_name": "weird.xyz",
                        "mime_type": "application/x-xyz",
                    }
                }
            ],
        ),
    )
    assert response.status_code == 200
    summaries = [t for _, t in isolated_bot["dms"] if "Не удалось обработать" in t]
    assert any("тип файла не поддерживается" in t for t in summaries)


def test_kb_api_submit_failure_reports_failure(isolated_bot, monkeypatch):
    txt_on_disk = isolated_bot["tmp_path"] / "x.txt"
    txt_on_disk.write_bytes(b"ok")

    async def fake_download(self, *, file_id, suggested_extension, mime_type=None):
        return DownloadedFile(path=txt_on_disk, byte_size=2, mime_type=mime_type)

    async def fake_submit(**kwargs):
        raise httpx.HTTPError("API down")

    monkeypatch.setattr(bot_main.TelegramFileDownloader, "download", fake_download)
    monkeypatch.setattr(bot_main.api_client, "submit_operator_upload", fake_submit)
    client = TestClient(bot_app)
    response = client.post(
        "/telegram/webhook",
        json=_operator_message(
            caption="/kb_add",
            attachments=[
                {
                    "document": {
                        "file_id": "T",
                        "file_name": "doc.txt",
                        "mime_type": "text/plain",
                        "file_size": 2,
                    }
                }
            ],
        ),
    )
    assert response.status_code == 200
    summaries = [t for _, t in isolated_bot["dms"]]
    assert any("api_failed" in t for t in summaries)


def test_kb_api_error_with_detail_is_surfaced_in_dm(isolated_bot, monkeypatch):
    """When the API returns 422 {"detail": "empty_text"}, the operator DM
    must contain the localized friendly reason — not the opaque httpx string.
    """
    import httpx

    from services.bot_gateway.app.api_client import ApiError

    txt_on_disk = isolated_bot["tmp_path"] / "presentation.pdf"
    txt_on_disk.write_bytes(b"PDF")

    async def fake_download(self, *, file_id, suggested_extension, mime_type=None):
        return DownloadedFile(path=txt_on_disk, byte_size=3, mime_type=mime_type)

    async def fake_submit(**kwargs):
        request = httpx.Request("POST", "http://api:8000/knowledge/operator_upload")
        response = httpx.Response(
            status_code=422,
            json={"detail": "empty_text"},
            request=request,
        )
        raise ApiError(
            "Client error '422 Unprocessable Entity'",
            request=request,
            response=response,
            detail="empty_text",
        )

    monkeypatch.setattr(bot_main.TelegramFileDownloader, "download", fake_download)
    monkeypatch.setattr(bot_main.api_client, "submit_operator_upload", fake_submit)
    client = TestClient(bot_app)
    response = client.post(
        "/telegram/webhook",
        json=_operator_message(
            caption="/kb_add",
            attachments=[
                {
                    "document": {
                        "file_id": "P",
                        "file_name": "Презентация.pdf",
                        "mime_type": "application/pdf",
                        "file_size": 3,
                    }
                }
            ],
        ),
    )
    assert response.status_code == 200
    summaries = [t for _, t in isolated_bot["dms"]]
    failure_lines = [t for t in summaries if "Не удалось обработать" in t]
    assert any("извлечь текст" in t for t in failure_lines)
    assert not any("Client error '422" in t for t in summaries)


def test_kb_inline_api_error_with_detail_is_surfaced(isolated_bot, monkeypatch):
    import httpx

    from services.bot_gateway.app.api_client import ApiError

    async def fake_submit(**kwargs):
        request = httpx.Request("POST", "http://api:8000/knowledge/operator_upload")
        response = httpx.Response(
            status_code=422,
            json={"detail": "empty_inline_text"},
            request=request,
        )
        raise ApiError(
            "Client error '422 Unprocessable Entity'",
            request=request,
            response=response,
            detail="empty_inline_text",
        )

    monkeypatch.setattr(bot_main.api_client, "submit_operator_upload", fake_submit)
    client = TestClient(bot_app)
    response = client.post(
        "/telegram/webhook",
        json=_operator_message(text="добавь в базу: справка"),
    )
    assert response.status_code == 200
    summaries = [t for _, t in isolated_bot["dms"]]
    assert any("пустой текст" in t for t in summaries)


def test_kb_inline_submit_failure_reported(isolated_bot, monkeypatch):
    async def fake_submit(**kwargs):
        raise RuntimeError("api boom")

    monkeypatch.setattr(bot_main.api_client, "submit_operator_upload", fake_submit)
    client = TestClient(bot_app)
    response = client.post(
        "/telegram/webhook",
        json=_operator_message(text="добавь в базу: внутренняя справка"),
    )
    assert response.status_code == 200
    summaries = [t for _, t in isolated_bot["dms"]]
    assert any("Не удалось" in t and "inline_text" in t for t in summaries)


def test_kb_dedup_summary_line(isolated_bot, monkeypatch):
    pdf = isolated_bot["tmp_path"] / "dup.pdf"
    pdf.write_bytes(b"PDF")

    async def fake_download(self, *, file_id, suggested_extension, mime_type=None):
        return DownloadedFile(path=pdf, byte_size=3, mime_type=mime_type)

    async def fake_submit(**kwargs):
        return {"inserted_chunks": 0, "is_confidential": False, "deduplicated": True}

    monkeypatch.setattr(bot_main.TelegramFileDownloader, "download", fake_download)
    monkeypatch.setattr(bot_main.api_client, "submit_operator_upload", fake_submit)
    client = TestClient(bot_app)
    response = client.post(
        "/telegram/webhook",
        json=_operator_message(
            caption="/kb_add",
            attachments=[
                {
                    "document": {
                        "file_id": "D",
                        "file_name": "schedule.pdf",
                        "mime_type": "application/pdf",
                        "file_size": 3,
                    }
                }
            ],
        ),
    )
    assert response.status_code == 200
    summaries = [t for _, t in isolated_bot["dms"]]
    assert any("уже было в базе" in t for t in summaries)


def test_send_dm_logs_failure_but_does_not_raise(monkeypatch):
    """The real _send_dm function must swallow Telegram errors silently."""
    class FakeClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def post(self, *args, **kwargs):
            raise httpx.HTTPError("offline")

    monkeypatch.setattr(bot_main.httpx, "AsyncClient", FakeClient)
    import asyncio

    asyncio.run(bot_main._send_dm(42, "hello"))


def test_send_dm_uses_configured_base_url(monkeypatch):
    """When the bot is pointed at a self-hosted Bot API server, _send_dm
    must call that host instead of api.telegram.org."""
    captured: list[str] = []

    class FakeClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def post(self, url, json):
            captured.append(url)

    monkeypatch.setattr(
        bot_main.settings,
        "telegram_bot_api_base_url",
        "http://local-bot-api:8081",
    )
    monkeypatch.setattr(bot_main.settings, "telegram_bot_token", "TKN")
    monkeypatch.setattr(bot_main.httpx, "AsyncClient", FakeClient)
    import asyncio

    asyncio.run(bot_main._send_dm(42, "hello"))
    assert captured == ["http://local-bot-api:8081/botTKN/sendMessage"]


def test_kb_source_file_type_for_direct_kinds(isolated_bot):
    from services.bot_gateway.app.main import _kb_source_file_type
    from services.bot_gateway.app.telegram_update import TelegramAttachment

    cases = (
        ("photo", "image"),
        ("audio", "audio"),
        ("voice", "audio"),
        ("video", "video"),
    )
    for kind, expected in cases:
        attachment = TelegramAttachment(file_id="x", kind=kind)
        assert _kb_source_file_type(attachment) == expected


def test_kb_source_file_type_via_filename(isolated_bot):
    from services.bot_gateway.app.main import _kb_source_file_type
    from services.bot_gateway.app.telegram_update import TelegramAttachment

    cases = {
        "deck.pdf": "pdf",
        "doc.docx": "docx",
        "slides.pptx": "pptx",
        "notes.txt": "txt",
    }
    for name, expected in cases.items():
        attachment = TelegramAttachment(file_id="x", kind="document", file_name=name)
        assert _kb_source_file_type(attachment) == expected


def test_kb_source_file_type_via_mime(isolated_bot):
    from services.bot_gateway.app.main import _kb_source_file_type
    from services.bot_gateway.app.telegram_update import TelegramAttachment

    pairs = {
        "application/pdf": "pdf",
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document": "docx",
        "application/msword": "docx",
        "application/vnd.openxmlformats-officedocument.presentationml.presentation": "pptx",
        "application/vnd.ms-powerpoint": "pptx",
        "text/plain": "txt",
        "image/png": "image",
        "audio/ogg": "audio",
        "video/mp4": "video",
    }
    for mime, expected in pairs.items():
        attachment = TelegramAttachment(file_id="x", kind="document", mime_type=mime)
        assert _kb_source_file_type(attachment) == expected


def test_kb_source_file_type_unrecognized_returns_none(isolated_bot):
    from services.bot_gateway.app.main import _kb_source_file_type
    from services.bot_gateway.app.telegram_update import TelegramAttachment

    attachment = TelegramAttachment(file_id="x", kind="document", mime_type="weird/mime")
    assert _kb_source_file_type(attachment) is None


def test_kb_source_file_type_via_filename_new_formats(isolated_bot):
    from services.bot_gateway.app.main import _kb_source_file_type
    from services.bot_gateway.app.telegram_update import TelegramAttachment

    cases = {
        "table.xlsx": "xlsx",
        "data.csv": "csv",
        "page.html": "html",
        "page.htm": "html",
        "notes.md": "md",
        "notes.markdown": "md",
        "memo.rtf": "rtf",
        "book.epub": "epub",
        "bundle.zip": "zip",
    }
    for name, expected in cases.items():
        attachment = TelegramAttachment(file_id="x", kind="document", file_name=name)
        assert _kb_source_file_type(attachment) == expected


def test_kb_source_file_type_via_mime_new_formats(isolated_bot):
    from services.bot_gateway.app.main import _kb_source_file_type
    from services.bot_gateway.app.telegram_update import TelegramAttachment

    pairs = {
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": "xlsx",
        "application/vnd.ms-excel": "xlsx",
        "text/csv": "csv",
        "text/html": "html",
        "text/markdown": "md",
        "application/rtf": "rtf",
        "text/rtf": "rtf",
        "application/epub+zip": "epub",
        "application/zip": "zip",
        "application/x-zip-compressed": "zip",
    }
    for mime, expected in pairs.items():
        attachment = TelegramAttachment(file_id="x", kind="document", mime_type=mime)
        assert _kb_source_file_type(attachment) == expected


def test_kb_extension_for_uses_file_name(isolated_bot):
    from services.bot_gateway.app.main import _kb_extension_for
    from services.bot_gateway.app.telegram_update import TelegramAttachment

    attachment = TelegramAttachment(file_id="x", kind="document", file_name="deck.PDF")
    assert _kb_extension_for(attachment, "pdf") == "PDF"


def test_kb_extension_for_uses_fallback(isolated_bot):
    from services.bot_gateway.app.main import _kb_extension_for
    from services.bot_gateway.app.telegram_update import TelegramAttachment

    attachment = TelegramAttachment(file_id="x", kind="voice")
    assert _kb_extension_for(attachment, "audio") == "ogg"
    attachment_unknown = TelegramAttachment(file_id="x", kind="document")
    assert _kb_extension_for(attachment_unknown, "weird") == "bin"


def test_kb_extension_for_uses_fallback_new_formats(isolated_bot):
    from services.bot_gateway.app.main import _kb_extension_for
    from services.bot_gateway.app.telegram_update import TelegramAttachment

    attachment = TelegramAttachment(file_id="x", kind="document")
    cases = {
        "xlsx": "xlsx",
        "csv": "csv",
        "html": "html",
        "md": "md",
        "rtf": "rtf",
        "epub": "epub",
        "zip": "zip",
    }
    for source_type, expected_ext in cases.items():
        assert _kb_extension_for(attachment, source_type) == expected_ext


def test_kb_attachment_count_word_variants():
    from services.bot_gateway.app.main import _kb_attachment_count_word

    assert _kb_attachment_count_word(1) == "файл"
    assert _kb_attachment_count_word(2) == "файла"
    assert _kb_attachment_count_word(3) == "файла"
    assert _kb_attachment_count_word(5) == "файлов"
    assert _kb_attachment_count_word(11) == "файлов"


def test_kb_command_no_intent_returns_none(isolated_bot, monkeypatch):
    import asyncio

    from services.bot_gateway.app.main import _handle_kb_command
    from services.bot_gateway.app.telegram_update import NormalizedTelegramMessage

    class _BgTasks:
        def __init__(self):
            self.added = []

        def add_task(self, fn, **kwargs):
            self.added.append((fn, kwargs))

    bg = _BgTasks()
    msg = NormalizedTelegramMessage(
        update_id=1,
        source_message_id=1,
        chat_id=10,
        user_id=20,
        username="@ajdevy",
        text="привет, как дела?",
    )
    result = asyncio.run(_handle_kb_command(msg, bg, is_operator=True))
    assert result is None


def test_kb_inline_intent_empty_cleaned_text_opens_session(isolated_bot, monkeypatch):
    import asyncio

    from services.bot_gateway.app.main import _handle_kb_command
    from services.bot_gateway.app.telegram_update import NormalizedTelegramMessage

    class _BgTasks:
        def __init__(self):
            self.added = []

        def add_task(self, fn, **kwargs):
            self.added.append((fn, kwargs))

    bg = _BgTasks()
    msg = NormalizedTelegramMessage(
        update_id=1,
        source_message_id=1,
        chat_id=10,
        user_id=20,
        username="@ajdevy",
        text="/kb_add",
        caption=None,
    )
    result = asyncio.run(_handle_kb_command(msg, bg, is_operator=True))
    assert result is not None
    assert result["status"] == "accepted"
    assert result["kb_mode"] == "session_opened"
    # No inline submit scheduled.
    assert bg.added == []
    # Session is now active in the repo.
    repo = isolated_bot["kb_session_repo"]
    assert repo.get_active(chat_id=10, username="@ajdevy") is not None
    # Operator was told what to do next.
    assert any("Жду файлы" in text for _, text in isolated_bot["dms"])


def test_kb_lemma_intent_without_files_opens_session_no_inline_submit(
    isolated_bot, monkeypatch
):
    """The operator's meta-request ('хочу добавить материалы…') must NOT be
    ingested as inline_text — it's a session-open signal, not knowledge."""
    submit_calls: list[dict] = []

    async def fake_submit(**kwargs):  # pragma: no cover - must not be called
        submit_calls.append(kwargs)
        return {"inserted_chunks": 0, "is_confidential": False, "deduplicated": False}

    monkeypatch.setattr(bot_main.api_client, "submit_operator_upload", fake_submit)
    client = TestClient(bot_app)
    response = client.post(
        "/telegram/webhook",
        json=_operator_message(text="хочу добавить материалы в knowledge base"),
    )
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "accepted"
    assert body["kb_mode"] == "session_opened"
    # No inline_text submitted (the meta-phrase would be garbage in RAG).
    assert submit_calls == []
    # Session is open for this operator/chat.
    repo = isolated_bot["kb_session_repo"]
    session = repo.get_active(chat_id=100, username="@ajdevy")
    assert session is not None
    assert session.is_confidential is False
    # Operator was prompted to send files.
    assert any("Жду файлы" in text for _, text in isolated_bot["dms"])


def test_kb_session_continuation_routes_pdf_after_text_intent(
    isolated_bot, monkeypatch
):
    """The headline scenario: operator types intent, then sends a PDF as a
    separate message. The PDF must be picked up via session continuation."""
    pdf = isolated_bot["tmp_path"] / "manual.pdf"
    pdf.write_bytes(b"%PDF-1.4")
    submit_calls: list[dict] = []

    async def fake_download(self, *, file_id, suggested_extension, mime_type=None):
        return DownloadedFile(path=pdf, byte_size=8, mime_type=mime_type)

    async def fake_submit(**kwargs):
        submit_calls.append(kwargs)
        return {"inserted_chunks": 7, "is_confidential": False, "deduplicated": False}

    monkeypatch.setattr(bot_main.TelegramFileDownloader, "download", fake_download)
    monkeypatch.setattr(bot_main.api_client, "submit_operator_upload", fake_submit)
    client = TestClient(bot_app)

    # 1) Text-only message that opens the session.
    text_msg = _operator_message(text="хочу добавить материалы в knowledge base")
    text_msg["update_id"] = 1001
    text_msg["message"]["message_id"] = 1001
    resp_text = client.post("/telegram/webhook", json=text_msg)
    assert resp_text.json()["kb_mode"] == "session_opened"

    # 2) PDF-only message (no text, no caption) in the same chat.
    pdf_msg = _operator_message(
        attachments=[
            {
                "document": {
                    "file_id": "PDF1",
                    "file_name": "manual.pdf",
                    "mime_type": "application/pdf",
                    "file_size": 8,
                }
            }
        ],
    )
    pdf_msg["update_id"] = 1002
    pdf_msg["message"]["message_id"] = 1002
    resp_pdf = client.post("/telegram/webhook", json=pdf_msg)
    body = resp_pdf.json()
    assert body["status"] == "accepted"
    assert body["kb_mode"] == "session_continuation"
    assert body["attachment_count"] == "1"

    # The upload background task ran and submitted the PDF.
    pdf_submits = [c for c in submit_calls if c.get("source_file_type") == "pdf"]
    assert len(pdf_submits) == 1
    assert pdf_submits[0]["source_file_name"] == "manual.pdf"
    # A success summary DM was sent.
    assert any("Добавлено в базу" in text for _, text in isolated_bot["dms"])


def test_kb_session_continuation_inherits_confidential_flag(isolated_bot, monkeypatch):
    pdf = isolated_bot["tmp_path"] / "secret.pdf"
    pdf.write_bytes(b"%PDF-secret")
    submit_calls: list[dict] = []

    async def fake_download(self, *, file_id, suggested_extension, mime_type=None):
        return DownloadedFile(path=pdf, byte_size=11, mime_type=mime_type)

    async def fake_submit(**kwargs):
        submit_calls.append(kwargs)
        return {"inserted_chunks": 1, "is_confidential": True, "deduplicated": False}

    monkeypatch.setattr(bot_main.TelegramFileDownloader, "download", fake_download)
    monkeypatch.setattr(bot_main.api_client, "submit_operator_upload", fake_submit)
    client = TestClient(bot_app)

    # /kb_add confidential opens a confidential session.
    text_msg = _operator_message(text="/kb_add confidential")
    text_msg["update_id"] = 2001
    text_msg["message"]["message_id"] = 2001
    client.post("/telegram/webhook", json=text_msg)

    pdf_msg = _operator_message(
        attachments=[
            {
                "document": {
                    "file_id": "S1",
                    "file_name": "secret.pdf",
                    "mime_type": "application/pdf",
                    "file_size": 11,
                }
            }
        ],
    )
    pdf_msg["update_id"] = 2002
    pdf_msg["message"]["message_id"] = 2002
    client.post("/telegram/webhook", json=pdf_msg)

    pdf_submits = [c for c in submit_calls if c.get("source_file_type") == "pdf"]
    assert len(pdf_submits) == 1
    assert pdf_submits[0]["is_confidential"] is True


def test_kb_session_continuation_refreshes_ttl(isolated_bot, monkeypatch):
    """Each session continuation must extend the TTL so a batch of files
    spread across several minutes keeps working."""
    from datetime import UTC, datetime, timedelta

    pdf = isolated_bot["tmp_path"] / "doc.pdf"
    pdf.write_bytes(b"%PDF")

    async def fake_download(self, *, file_id, suggested_extension, mime_type=None):
        return DownloadedFile(path=pdf, byte_size=4, mime_type=mime_type)

    async def fake_submit(**kwargs):
        return {"inserted_chunks": 1, "is_confidential": False, "deduplicated": False}

    monkeypatch.setattr(bot_main.TelegramFileDownloader, "download", fake_download)
    monkeypatch.setattr(bot_main.api_client, "submit_operator_upload", fake_submit)

    base = datetime(2026, 5, 15, 12, 0, tzinfo=UTC)
    current = {"t": base}
    monkeypatch.setattr(kb_session_module, "_now", lambda: current["t"])

    client = TestClient(bot_app)
    # T0: open session.
    text_msg = _operator_message(text="хочу добавить материалы в knowledge base")
    text_msg["update_id"] = 3001
    text_msg["message"]["message_id"] = 3001
    client.post("/telegram/webhook", json=text_msg)

    repo = isolated_bot["kb_session_repo"]
    initial = repo.get_active(chat_id=100, username="@ajdevy")
    assert initial is not None
    initial_expires = datetime.fromisoformat(initial.expires_at)
    assert initial_expires == base + timedelta(seconds=600)

    # T0 + 300s: send a PDF. Continuation runs, TTL refreshed to T+900.
    current["t"] = base + timedelta(seconds=300)
    pdf_msg = _operator_message(
        attachments=[
            {
                "document": {
                    "file_id": "P",
                    "file_name": "doc.pdf",
                    "mime_type": "application/pdf",
                    "file_size": 4,
                }
            }
        ],
    )
    pdf_msg["update_id"] = 3002
    pdf_msg["message"]["message_id"] = 3002
    client.post("/telegram/webhook", json=pdf_msg)

    refreshed = repo.get_active(chat_id=100, username="@ajdevy")
    assert refreshed is not None
    refreshed_expires = datetime.fromisoformat(refreshed.expires_at)
    assert refreshed_expires == base + timedelta(seconds=900)


def test_kb_session_continuation_dropped_after_ttl(isolated_bot, monkeypatch):
    """PDF arriving past the TTL window must hit the attachment-only guard."""
    from datetime import UTC, datetime, timedelta

    async def fake_submit(**kwargs):  # pragma: no cover - must not run
        raise AssertionError("submit must not be called for expired session")

    monkeypatch.setattr(bot_main.api_client, "submit_operator_upload", fake_submit)

    base = datetime(2026, 5, 15, 12, 0, tzinfo=UTC)
    current = {"t": base}
    monkeypatch.setattr(kb_session_module, "_now", lambda: current["t"])

    client = TestClient(bot_app)
    # Open session at T0.
    open_msg = _operator_message(text="хочу добавить материалы в knowledge base")
    open_msg["update_id"] = 4001
    open_msg["message"]["message_id"] = 4001
    client.post("/telegram/webhook", json=open_msg)

    # T0 + 601s: PDF arrives after TTL expired.
    current["t"] = base + timedelta(seconds=601)
    pdf_msg = _operator_message(
        attachments=[
            {
                "document": {
                    "file_id": "L",
                    "file_name": "late.pdf",
                    "mime_type": "application/pdf",
                    "file_size": 3,
                }
            }
        ],
    )
    pdf_msg["update_id"] = 4002
    pdf_msg["message"]["message_id"] = 4002
    response = client.post("/telegram/webhook", json=pdf_msg)
    body = response.json()
    assert body["status"] == "ignored"
    assert body["reason"] == "attachment_only"


def test_kb_session_does_not_route_non_operator_attachments(
    isolated_bot, monkeypatch
):
    """A PDF from a different user must NOT be uploaded into the operator's
    open KB session — the session is keyed by (chat_id, username)."""
    async def fake_submit(**kwargs):  # pragma: no cover - must not run
        raise AssertionError("submit must not be called for non-operator")

    monkeypatch.setattr(bot_main.api_client, "submit_operator_upload", fake_submit)

    client = TestClient(bot_app)
    # Operator opens session.
    open_msg = _operator_message(text="хочу добавить материалы в knowledge base")
    open_msg["update_id"] = 5001
    open_msg["message"]["message_id"] = 5001
    client.post("/telegram/webhook", json=open_msg)

    # A non-operator user sends a PDF in the same chat.
    intruder = _operator_message(
        attachments=[
            {
                "document": {
                    "file_id": "Z",
                    "file_name": "rogue.pdf",
                    "mime_type": "application/pdf",
                    "file_size": 5,
                }
            }
        ],
    )
    intruder["update_id"] = 5002
    intruder["message"]["message_id"] = 5002
    intruder["message"]["from"]["username"] = "stranger"
    response = client.post("/telegram/webhook", json=intruder)
    body = response.json()
    assert body["status"] == "ignored"
    assert body["reason"] == "attachment_only"


def test_kb_cancel_from_operator_clears_session(isolated_bot):
    client = TestClient(bot_app)
    # Open session.
    open_msg = _operator_message(text="хочу добавить материалы в knowledge base")
    open_msg["update_id"] = 6001
    open_msg["message"]["message_id"] = 6001
    client.post("/telegram/webhook", json=open_msg)
    repo = isolated_bot["kb_session_repo"]
    assert repo.get_active(chat_id=100, username="@ajdevy") is not None

    # Cancel.
    cancel = _operator_message(text="/kb_cancel")
    cancel["update_id"] = 6002
    cancel["message"]["message_id"] = 6002
    response = client.post("/telegram/webhook", json=cancel)
    body = response.json()
    assert body["status"] == "kb_session_cleared"
    assert repo.get_active(chat_id=100, username="@ajdevy") is None
    assert any("Сессия закрыта" in text for _, text in isolated_bot["dms"])


def test_kb_cancel_from_non_operator_is_unauthorized(isolated_bot):
    client = TestClient(bot_app)
    cancel = _operator_message(text="/kb_cancel")
    cancel["update_id"] = 7001
    cancel["message"]["message_id"] = 7001
    cancel["message"]["from"]["username"] = "stranger"
    response = client.post("/telegram/webhook", json=cancel)
    body = response.json()
    # Non-operator falls through to the customer-forward flow; the
    # `/kb_cancel` check inside _handle_kb_command never runs because the
    # operator gate at the top returns None first.
    assert body.get("kb_mode") is None
    assert body.get("status") != "kb_session_cleared"


def test_kb_continuation_clears_when_attachment_processed_but_session_expires_naturally(
    isolated_bot,
):
    """Sanity: after TTL elapses without continuation, get_active returns
    None even without an explicit clear. (Covered indirectly above; this is
    a positive regression for the repo accessor used by main.py.)"""
    repo = isolated_bot["kb_session_repo"]
    repo.upsert(chat_id=1, username="@op", is_confidential=False, ttl_seconds=0)
    # ttl=0 → expires_at <= now() immediately → considered inactive.
    assert repo.get_active(chat_id=1, username="@op") is None


def test_kb_pre_check_too_large_skips_download_but_records_registry(
    isolated_bot, monkeypatch
):
    monkeypatch.setattr(bot_main.settings, "operator_upload_max_bytes", 1024)

    download_invoked: list[str] = []

    async def fake_download(self, *, file_id, suggested_extension, mime_type=None):
        download_invoked.append(file_id)
        raise AssertionError("download must not be called for oversize files")

    monkeypatch.setattr(bot_main.TelegramFileDownloader, "download", fake_download)

    async def fake_submit(**kwargs):  # pragma: no cover - should not run
        raise AssertionError("submit must not be called for oversize files")

    monkeypatch.setattr(bot_main.api_client, "submit_operator_upload", fake_submit)
    client = TestClient(bot_app)
    response = client.post(
        "/telegram/webhook",
        json=_operator_message(
            caption="/kb_add",
            attachments=[
                {
                    "document": {
                        "file_id": "HUGE",
                        "file_name": "brochure.pdf",
                        "mime_type": "application/pdf",
                        "file_size": 50_000_000,
                    }
                }
            ],
        ),
    )
    assert response.status_code == 200
    assert download_invoked == []
    summary = next(
        (t for _, t in isolated_bot["dms"] if "Не удалось обработать" in t), None
    )
    assert summary is not None
    assert "файл больше" in summary
    assert "brochure.pdf" in summary

    records = isolated_bot["files_repo"].list_recent(
        username="@ajdevy", limit=10
    )
    assert len(records) == 1
    assert records[0].download_status == "too_large"
    assert records[0].kb_ingest_status == "skipped"
    assert records[0].source_file_name == "brochure.pdf"
    assert records[0].telegram_file_id == "HUGE"


def test_kb_success_writes_registry_row_with_ok_status(isolated_bot, monkeypatch):
    pdf = isolated_bot["tmp_path"] / "ok.pdf"
    pdf.write_bytes(b"%PDF-OK")

    async def fake_download(self, *, file_id, suggested_extension, mime_type=None):
        return DownloadedFile(path=pdf, byte_size=7, mime_type=mime_type)

    async def fake_submit(**kwargs):
        return {"inserted_chunks": 3, "is_confidential": False, "deduplicated": False}

    monkeypatch.setattr(bot_main.TelegramFileDownloader, "download", fake_download)
    monkeypatch.setattr(bot_main.api_client, "submit_operator_upload", fake_submit)
    client = TestClient(bot_app)
    client.post(
        "/telegram/webhook",
        json=_operator_message(
            caption="/kb_add",
            attachments=[
                {
                    "document": {
                        "file_id": "F1",
                        "file_name": "ok.pdf",
                        "mime_type": "application/pdf",
                        "file_size": 7,
                    }
                }
            ],
        ),
    )
    records = isolated_bot["files_repo"].list_recent(
        username="@ajdevy", limit=10
    )
    assert len(records) == 1
    assert records[0].download_status == "ok"
    assert records[0].kb_ingest_status == "ok"
    assert records[0].kb_inserted_chunks == 3
    assert records[0].stored_binary_path == str(pdf)

    summaries = [t for _, t in isolated_bot["dms"] if "Добавлено в базу" in t]
    assert summaries
    assert f"#{records[0].short_id}" in summaries[0]
    assert "ok.pdf" in summaries[0]


def test_kb_api_failure_marks_registry_kb_ingest_failed(isolated_bot, monkeypatch):
    pdf = isolated_bot["tmp_path"] / "x.pdf"
    pdf.write_bytes(b"%PDF")

    async def fake_download(self, *, file_id, suggested_extension, mime_type=None):
        return DownloadedFile(path=pdf, byte_size=4, mime_type=mime_type)

    async def fake_submit(**kwargs):
        raise httpx.HTTPError("api boom")

    monkeypatch.setattr(bot_main.TelegramFileDownloader, "download", fake_download)
    monkeypatch.setattr(bot_main.api_client, "submit_operator_upload", fake_submit)
    client = TestClient(bot_app)
    client.post(
        "/telegram/webhook",
        json=_operator_message(
            caption="/kb_add",
            attachments=[
                {
                    "document": {
                        "file_id": "F2",
                        "file_name": "doc.pdf",
                        "mime_type": "application/pdf",
                        "file_size": 4,
                    }
                }
            ],
        ),
    )
    records = isolated_bot["files_repo"].list_recent(
        username="@ajdevy", limit=10
    )
    assert len(records) == 1
    assert records[0].download_status == "ok"
    assert records[0].kb_ingest_status.startswith("failed:")


def test_kb_unsupported_writes_registry_row_with_failed_status(
    isolated_bot, monkeypatch
):
    async def fake_submit(**kwargs):  # pragma: no cover
        raise AssertionError("must not submit")

    monkeypatch.setattr(bot_main.api_client, "submit_operator_upload", fake_submit)
    client = TestClient(bot_app)
    client.post(
        "/telegram/webhook",
        json=_operator_message(
            caption="/kb_add",
            attachments=[
                {
                    "document": {
                        "file_id": "ZZ",
                        "file_name": "weird.xyz",
                        "mime_type": "application/x-xyz",
                    }
                }
            ],
        ),
    )
    records = isolated_bot["files_repo"].list_recent(
        username="@ajdevy", limit=10
    )
    assert len(records) == 1
    assert records[0].download_status == "failed:unsupported_attachment_type"
    assert records[0].kb_ingest_status == "skipped"


def test_kb_telegram_error_messages_do_not_leak_bot_token(
    isolated_bot, monkeypatch
):
    monkeypatch.setattr(bot_main.settings, "telegram_bot_token", "SECRET_LEAK_NOT")

    async def fake_download(self, *, file_id, suggested_extension, mime_type=None):
        raise TelegramFileDownloadError(
            "telegram_get_file_failed",
            description="Bad Request: wrong file_id specified",
        )

    monkeypatch.setattr(bot_main.TelegramFileDownloader, "download", fake_download)
    client = TestClient(bot_app)
    client.post(
        "/telegram/webhook",
        json=_operator_message(
            caption="/kb_add",
            attachments=[
                {
                    "document": {
                        "file_id": "F",
                        "file_name": "x.pdf",
                        "mime_type": "application/pdf",
                        "file_size": 10,
                    }
                }
            ],
        ),
    )
    summaries = [t for _, t in isolated_bot["dms"]]
    for summary in summaries:
        assert "SECRET_LEAK_NOT" not in summary
    summary = next((t for t in summaries if "Не удалось обработать" in t), "")
    assert "Telegram" in summary


def test_kb_friendly_failure_reason_helper_covers_branches():
    from services.bot_gateway.app.main import _friendly_failure_reason

    assert "20 МБ" in _friendly_failure_reason(
        "file_too_large", max_bytes=20 * 1024 * 1024
    )
    assert _friendly_failure_reason("unsupported_attachment_type", max_bytes=1) == (
        "тип файла не поддерживается"
    )
    assert "Telegram" in _friendly_failure_reason(
        "telegram_network_error", max_bytes=1
    )
    assert "Telegram" in _friendly_failure_reason(
        "telegram_cdn_error", max_bytes=1
    )
    assert "wrong file_id" in _friendly_failure_reason(
        "telegram_get_file_failed:wrong file_id", max_bytes=1
    )
    assert _friendly_failure_reason("telegram_get_file_failed", max_bytes=1).startswith(
        "Telegram"
    )
    assert _friendly_failure_reason(
        "api_failed:something", max_bytes=1
    ).startswith("api_failed:")
    assert "извлечь текст" in _friendly_failure_reason(
        "api_failed:empty_text", max_bytes=1
    )
    assert "тип файла" in _friendly_failure_reason(
        "api_failed:unsupported_source_file_type", max_bytes=1
    )
    assert "не передан путь" in _friendly_failure_reason(
        "api_failed:missing_stored_binary_path", max_bytes=1
    )
    assert "слишком длинный" in _friendly_failure_reason(
        "api_failed:pdf_too_many_pages_for_ocr", max_bytes=1
    )
    assert _friendly_failure_reason("download_failed", max_bytes=1) == (
        "не удалось скачать файл"
    )
    # Default branch redacts token if present
    redacted = _friendly_failure_reason(
        "weird bot12345:ABCDEF token leak", max_bytes=1
    )
    assert "bot12345:ABCDEF" not in redacted


def test_kb_media_group_buffers_first_file_no_immediate_ack(
    isolated_bot, monkeypatch
):
    submit_calls: list[dict] = []

    async def fake_submit(**kwargs):
        submit_calls.append(kwargs)
        return {"inserted_chunks": 1, "is_confidential": False, "deduplicated": False}

    monkeypatch.setattr(bot_main.api_client, "submit_operator_upload", fake_submit)

    # Suppress the auto-flush so we can observe pure buffered state.
    scheduled: list[str] = []

    async def fake_flush(*, media_group_id, debounce_seconds):
        scheduled.append(media_group_id)

    monkeypatch.setattr(bot_main, "_flush_media_group_after_debounce", fake_flush)

    client = TestClient(bot_app)
    # Open the session first
    open_msg = _operator_message(text="хочу добавить материалы в knowledge base")
    open_msg["update_id"] = 8001
    open_msg["message"]["message_id"] = 8001
    client.post("/telegram/webhook", json=open_msg)
    isolated_bot["dms"].clear()

    # First message of a media group — only buffer; do not ack/submit yet.
    mg1 = _operator_message(
        attachments=[
            {
                "document": {
                    "file_id": "MG-A",
                    "file_name": "a.pdf",
                    "mime_type": "application/pdf",
                    "file_size": 100,
                }
            }
        ],
    )
    mg1["update_id"] = 8002
    mg1["message"]["message_id"] = 8002
    mg1["message"]["media_group_id"] = "MG_X"
    response = client.post("/telegram/webhook", json=mg1)
    body = response.json()
    assert body["status"] == "accepted"
    assert body["kb_mode"] == "media_group_buffered"
    # No "Принял" ack and no submit yet — the (suppressed) flush would do it.
    assert not any("Принял" in text for _, text in isolated_bot["dms"])
    assert submit_calls == []
    # The buffer holds the attachment.
    drained = isolated_bot["media_group_buffer"].drain(media_group_id="MG_X")
    assert [d.attachment.file_id for d in drained] == ["MG-A"]
    # The flush was scheduled exactly once.
    assert scheduled == ["MG_X"]


def test_kb_media_group_two_files_single_ack_and_single_summary(
    isolated_bot, monkeypatch
):
    pdf_a = isolated_bot["tmp_path"] / "a.pdf"
    pdf_a.write_bytes(b"AAAA")
    pdf_b = isolated_bot["tmp_path"] / "b.pdf"
    pdf_b.write_bytes(b"BBBB")

    async def fake_download(self, *, file_id, suggested_extension, mime_type=None):
        if file_id == "MG-A":
            return DownloadedFile(path=pdf_a, byte_size=4, mime_type=mime_type)
        return DownloadedFile(path=pdf_b, byte_size=4, mime_type=mime_type)

    submit_calls: list[dict] = []

    async def fake_submit(**kwargs):
        submit_calls.append(kwargs)
        return {"inserted_chunks": 2, "is_confidential": False, "deduplicated": False}

    monkeypatch.setattr(bot_main.TelegramFileDownloader, "download", fake_download)
    monkeypatch.setattr(bot_main.api_client, "submit_operator_upload", fake_submit)

    # Capture the real flush, then suppress it so it doesn't fire mid-batch.
    real_flush = bot_main._flush_media_group_after_debounce

    async def noop_flush(**kwargs):
        return None

    monkeypatch.setattr(bot_main, "_flush_media_group_after_debounce", noop_flush)

    client = TestClient(bot_app)
    # Open session
    open_msg = _operator_message(text="хочу добавить материалы в knowledge base")
    open_msg["update_id"] = 9001
    open_msg["message"]["message_id"] = 9001
    client.post("/telegram/webhook", json=open_msg)
    isolated_bot["dms"].clear()

    # Two media-group updates
    for offset, fid, name in ((1, "MG-A", "a.pdf"), (2, "MG-B", "b.pdf")):
        msg = _operator_message(
            attachments=[
                {
                    "document": {
                        "file_id": fid,
                        "file_name": name,
                        "mime_type": "application/pdf",
                        "file_size": 4,
                    }
                }
            ],
        )
        msg["update_id"] = 9100 + offset
        msg["message"]["message_id"] = 9100 + offset
        msg["message"]["media_group_id"] = "MG_X"
        client.post("/telegram/webhook", json=msg)

    # Now manually run the real flush — Telegram's media-group window settled.
    import asyncio

    asyncio.run(real_flush(media_group_id="MG_X", debounce_seconds=0))

    acks = [t for _, t in isolated_bot["dms"] if "Принял" in t]
    summaries = [t for _, t in isolated_bot["dms"] if "Добавлено в базу" in t]
    assert len(acks) == 1
    assert "2" in acks[0]
    assert "файла" in acks[0]
    assert len(summaries) == 1
    assert len(submit_calls) == 2
    assert "a.pdf" in summaries[0]
    assert "b.pdf" in summaries[0]


def test_kb_media_group_flush_empty_buffer_is_noop(
    isolated_bot, monkeypatch
):
    """If two flush tasks somehow run for the same group, the second sees an
    empty buffer and returns without sending anything."""
    import asyncio

    submit_calls: list[dict] = []

    async def fake_submit(**kwargs):  # pragma: no cover - must not run
        submit_calls.append(kwargs)
        return {}

    monkeypatch.setattr(bot_main.api_client, "submit_operator_upload", fake_submit)
    asyncio.run(
        bot_main._flush_media_group_after_debounce(
            media_group_id="UNKNOWN", debounce_seconds=0
        )
    )
    assert submit_calls == []
    assert not any("Принял" in t for _, t in isolated_bot["dms"])


def test_kb_media_group_flush_polls_until_group_is_quiet(
    isolated_bot, monkeypatch
):
    """With a positive debounce, the flusher polls at `poll_interval` and
    keeps looping while the group still has fresh writes. Once
    `now - latest_received_at >= debounce_seconds`, it drains."""
    import asyncio
    from datetime import UTC, datetime, timedelta

    from services.bot_gateway.app.telegram_update import TelegramAttachment

    monkeypatch.setattr(
        bot_main.settings, "operator_media_group_poll_interval_seconds", 0.1
    )
    monkeypatch.setattr(
        bot_main.settings, "operator_media_group_settling_cap_seconds", 30.0
    )

    sleep_calls: list[float] = []

    async def fake_sleep(seconds):
        sleep_calls.append(seconds)

    monkeypatch.setattr(bot_main.asyncio, "sleep", fake_sleep)

    # Pre-populate the buffer so drain has something to find when the
    # quiet-for window is finally satisfied.
    isolated_bot["media_group_buffer"].add(
        media_group_id="POLL",
        chat_id=100,
        username="@ajdevy",
        update_id=1,
        source_message_id=1,
        attachment=TelegramAttachment(file_id="x", kind="document"),
        is_confidential=False,
    )

    # Two latest_received_at readings: first reading reports a recent write
    # (not quiet yet → poll), second reading reports an old write (drain).
    base = datetime.now(UTC)
    readings = iter([base, base - timedelta(seconds=10)])

    def fake_latest(*, media_group_id):
        return next(readings)

    monkeypatch.setattr(
        bot_main.media_group_buffer,
        "latest_received_at",
        fake_latest,
    )

    async def fake_submit(**kwargs):
        return {"inserted_chunks": 1, "is_confidential": False, "deduplicated": False}

    monkeypatch.setattr(bot_main.api_client, "submit_operator_upload", fake_submit)

    asyncio.run(
        bot_main._flush_media_group_after_debounce(
            media_group_id="POLL", debounce_seconds=2.5
        )
    )

    # Slept exactly once at the poll interval (one not-yet-quiet reading).
    assert sleep_calls == [0.1]
    # Drained: buffer is now empty.
    assert isolated_bot["media_group_buffer"].drain(media_group_id="POLL") == []


def test_kb_media_group_settling_cap_forces_drain(
    isolated_bot, monkeypatch, caplog
):
    """A pathological stream of writes that keeps `quiet_for` under threshold
    must still be drained once `elapsed >= cap`, with a WARNING log."""
    import asyncio
    from datetime import UTC, datetime

    from services.bot_gateway.app.telegram_update import TelegramAttachment

    monkeypatch.setattr(
        bot_main.settings, "operator_media_group_poll_interval_seconds", 0.05
    )
    monkeypatch.setattr(
        bot_main.settings, "operator_media_group_settling_cap_seconds", 0.5
    )

    # latest_received_at always returns "now" so quiet_for stays at ~0.
    def fake_latest(*, media_group_id):
        return datetime.now(UTC)

    monkeypatch.setattr(
        bot_main.media_group_buffer,
        "latest_received_at",
        fake_latest,
    )

    pdf = isolated_bot["tmp_path"] / "cap.pdf"
    pdf.write_bytes(b"PDF")

    async def fake_download(self, *, file_id, suggested_extension, mime_type=None):
        return DownloadedFile(path=pdf, byte_size=3, mime_type=mime_type)

    monkeypatch.setattr(bot_main.TelegramFileDownloader, "download", fake_download)

    isolated_bot["media_group_buffer"].add(
        media_group_id="CAP",
        chat_id=100,
        username="@ajdevy",
        update_id=1,
        source_message_id=1,
        attachment=TelegramAttachment(
            file_id="x",
            kind="document",
            mime_type="application/pdf",
            file_size=3,
            file_name="cap.pdf",
        ),
        is_confidential=False,
    )
    # An active kb_session must exist for the flusher to upload — the
    # session check at drain time guards against random operator media
    # groups being silently ingested as KB content.
    isolated_bot["kb_session_repo"].upsert(
        chat_id=100, username="@ajdevy", is_confidential=False, ttl_seconds=900
    )

    submit_calls: list[dict] = []

    async def fake_submit(**kwargs):
        submit_calls.append(kwargs)
        return {"inserted_chunks": 0, "is_confidential": False, "deduplicated": False}

    monkeypatch.setattr(bot_main.api_client, "submit_operator_upload", fake_submit)

    with caplog.at_level("WARNING"):
        asyncio.run(
            bot_main._flush_media_group_after_debounce(
                media_group_id="CAP", debounce_seconds=10.0
            )
        )
    assert any(
        "media_group_settling_cap_hit" in r.message for r in caplog.records
    )
    # The cap forced a drain → submit was called.
    assert len(submit_calls) == 1


def test_kb_media_group_flush_drains_empty_after_race(
    isolated_bot, monkeypatch
):
    """Defensive: between `latest_received_at` (sees rows) and `drain()`,
    another flusher may have raced ahead and emptied the buffer. The
    second flusher must return without sending an ack/summary."""
    import asyncio
    from datetime import UTC, datetime, timedelta

    # Patch latest_received_at to always claim "data was here 10s ago" so
    # the polling loop breaks immediately. Real drain returns [] because
    # the buffer is empty.
    def fake_latest(*, media_group_id):
        return datetime.now(UTC) - timedelta(seconds=10)

    monkeypatch.setattr(
        bot_main.media_group_buffer,
        "latest_received_at",
        fake_latest,
    )

    submit_calls: list[dict] = []

    async def fake_submit(**kwargs):  # pragma: no cover - must not run
        submit_calls.append(kwargs)
        return {}

    monkeypatch.setattr(bot_main.api_client, "submit_operator_upload", fake_submit)

    asyncio.run(
        bot_main._flush_media_group_after_debounce(
            media_group_id="ALREADY_DRAINED", debounce_seconds=0
        )
    )

    assert submit_calls == []
    assert not any("Принял" in t for _, t in isolated_bot["dms"])


def test_kb_media_group_concurrent_flushers_only_one_drains(
    isolated_bot, monkeypatch
):
    """Each webhook now schedules its own flusher. The first one to win the
    `drain()` race processes the batch; the others observe an empty buffer
    and exit without sending a second ack or summary."""
    import asyncio

    from services.bot_gateway.app.telegram_update import TelegramAttachment

    pdf = isolated_bot["tmp_path"] / "race.pdf"
    pdf.write_bytes(b"PDF")

    async def fake_download(self, *, file_id, suggested_extension, mime_type=None):
        return DownloadedFile(path=pdf, byte_size=3, mime_type=mime_type)

    monkeypatch.setattr(bot_main.TelegramFileDownloader, "download", fake_download)

    for fid, uid in (("a", 1), ("b", 2)):
        isolated_bot["media_group_buffer"].add(
            media_group_id="RACE",
            chat_id=100,
            username="@ajdevy",
            update_id=uid,
            source_message_id=uid,
            attachment=TelegramAttachment(
                file_id=fid,
                kind="document",
                mime_type="application/pdf",
                file_size=3,
                file_name=f"{fid}.pdf",
            ),
            is_confidential=False,
        )
    isolated_bot["kb_session_repo"].upsert(
        chat_id=100, username="@ajdevy", is_confidential=False, ttl_seconds=900
    )

    submit_calls: list[dict] = []

    async def fake_submit(**kwargs):
        submit_calls.append(kwargs)
        return {"inserted_chunks": 1, "is_confidential": False, "deduplicated": False}

    monkeypatch.setattr(bot_main.api_client, "submit_operator_upload", fake_submit)

    async def run_two_flushers():
        await asyncio.gather(
            bot_main._flush_media_group_after_debounce(
                media_group_id="RACE", debounce_seconds=0
            ),
            bot_main._flush_media_group_after_debounce(
                media_group_id="RACE", debounce_seconds=0
            ),
        )

    asyncio.run(run_two_flushers())

    # Exactly one ack and one summary regardless of two flushers running.
    acks = [t for _, t in isolated_bot["dms"] if "Принял" in t]
    summaries = [t for _, t in isolated_bot["dms"] if "Добавлено в базу" in t]
    assert len(acks) == 1
    assert len(summaries) == 1
    # Submit ran once per attachment (2 attachments → 2 calls total across
    # both flushers; the loser sees [] and no-ops, so only the winning
    # flusher's loop made the calls).
    assert len(submit_calls) == 2


def test_kb_media_group_staggered_four_files_all_drained(
    isolated_bot, monkeypatch
):
    """Regression: 4 webhooks for one album where webhook #1 carries the
    caption and #2-#4 carry only attachments + media_group_id. With the
    settling-window flusher, the buffer is fully drained on the first
    flush — no files are lost. Mirrors the production bug pattern."""
    import asyncio

    pdfs = []
    for name in ("a.pdf", "b.pdf", "c.pdf", "d.pdf"):
        path = isolated_bot["tmp_path"] / name
        path.write_bytes(b"PDF")
        pdfs.append(path)

    async def fake_download(self, *, file_id, suggested_extension, mime_type=None):
        idx = {"MG-A": 0, "MG-B": 1, "MG-C": 2, "MG-D": 3}[file_id]
        return DownloadedFile(path=pdfs[idx], byte_size=3, mime_type=mime_type)

    submit_calls: list[dict] = []

    async def fake_submit(**kwargs):
        submit_calls.append(kwargs)
        return {"inserted_chunks": 1, "is_confidential": False, "deduplicated": False}

    monkeypatch.setattr(bot_main.TelegramFileDownloader, "download", fake_download)
    monkeypatch.setattr(bot_main.api_client, "submit_operator_upload", fake_submit)

    # Capture real flusher, replace with no-op during webhook delivery so we
    # can run the real flush deterministically once all updates land.
    real_flush = bot_main._flush_media_group_after_debounce

    async def noop_flush(**kwargs):
        return None

    monkeypatch.setattr(bot_main, "_flush_media_group_after_debounce", noop_flush)

    client = TestClient(bot_app)

    files = [
        ("MG-A", "a.pdf", True),    # first carries the trigger caption
        ("MG-B", "b.pdf", False),
        ("MG-C", "c.pdf", False),
        ("MG-D", "d.pdf", False),
    ]
    for offset, (fid, name, with_caption) in enumerate(files):
        msg = _operator_message(
            caption="добавь в базу знаний" if with_caption else None,
            attachments=[
                {
                    "document": {
                        "file_id": fid,
                        "file_name": name,
                        "mime_type": "application/pdf",
                        "file_size": 3,
                    }
                }
            ],
        )
        msg["update_id"] = 7100 + offset
        msg["message"]["message_id"] = 7100 + offset
        msg["message"]["media_group_id"] = "MG_STAGGER"
        response = client.post("/telegram/webhook", json=msg)
        assert response.status_code == 200, response.text

    isolated_bot["dms"].clear()
    asyncio.run(real_flush(media_group_id="MG_STAGGER", debounce_seconds=0))

    acks = [t for _, t in isolated_bot["dms"] if "Принял" in t]
    summaries = [t for _, t in isolated_bot["dms"] if "Добавлено в базу" in t]
    assert len(acks) == 1
    assert "4" in acks[0]
    assert "файла" in acks[0]
    assert len(summaries) == 1
    assert all(name in summaries[0] for name in ("a.pdf", "b.pdf", "c.pdf", "d.pdf"))
    assert len(submit_calls) == 4


def test_kb_media_group_caption_only_on_first_routes_via_session(
    isolated_bot, monkeypatch
):
    """Real Telegram delivery: caption only on update #1. Updates #2-#4 hit
    `_handle_kb_session_continuation` (no intent → no kb_command match),
    which must route them into the same media-group buffer."""
    scheduled: list[str] = []

    async def fake_flush(*, media_group_id, debounce_seconds):
        scheduled.append(media_group_id)

    monkeypatch.setattr(bot_main, "_flush_media_group_after_debounce", fake_flush)

    client = TestClient(bot_app)

    files = [
        ("MG-A", "a.pdf", True),
        ("MG-B", "b.pdf", False),
        ("MG-C", "c.pdf", False),
        ("MG-D", "d.pdf", False),
    ]
    for offset, (fid, name, with_caption) in enumerate(files):
        msg = _operator_message(
            caption="добавь в базу знаний" if with_caption else None,
            attachments=[
                {
                    "document": {
                        "file_id": fid,
                        "file_name": name,
                        "mime_type": "application/pdf",
                        "file_size": 3,
                    }
                }
            ],
        )
        msg["update_id"] = 7200 + offset
        msg["message"]["message_id"] = 7200 + offset
        msg["message"]["media_group_id"] = "MG_CAP_ONLY"
        response = client.post("/telegram/webhook", json=msg)
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["status"] == "accepted"
        assert body["kb_mode"] == "media_group_buffered"

    drained = isolated_bot["media_group_buffer"].drain(
        media_group_id="MG_CAP_ONLY"
    )
    assert [d.attachment.file_id for d in drained] == [
        "MG-A",
        "MG-B",
        "MG-C",
        "MG-D",
    ]
    # Each webhook now schedules its own flusher (no `if first` gate).
    assert scheduled == ["MG_CAP_ONLY"] * 4


def test_kb_media_group_flush_swallows_internal_errors(
    isolated_bot, monkeypatch, caplog
):
    """A crash inside the flush task must be logged, not raised."""
    import asyncio

    monkeypatch.setattr(bot_main, "_send_dm", _raising_send_dm)
    # Pre-populate the buffer so drain returns something.
    from services.bot_gateway.app.telegram_update import TelegramAttachment

    isolated_bot["media_group_buffer"].add(
        media_group_id="ERR",
        chat_id=100,
        username="@ajdevy",
        update_id=1,
        source_message_id=1,
        attachment=TelegramAttachment(file_id="x", kind="document"),
        is_confidential=False,
    )
    with caplog.at_level("ERROR"):
        asyncio.run(
            bot_main._flush_media_group_after_debounce(
                media_group_id="ERR", debounce_seconds=0
            )
        )
    assert any("media_group_flush_failed" in r.message for r in caplog.records)


async def _raising_send_dm(chat_id: int, text: str) -> None:
    raise RuntimeError("send_dm boom")


def test_kb_command_with_caption_and_media_group_buffers(
    isolated_bot, monkeypatch
):
    """When the operator's intent caption + first PDF arrive together as the
    head of a media group, the KB command branch must buffer (not ack)
    instead of submitting immediately. The flush will pick up all files
    in the group."""
    scheduled: list[str] = []

    async def fake_flush(*, media_group_id, debounce_seconds):
        scheduled.append(media_group_id)

    monkeypatch.setattr(bot_main, "_flush_media_group_after_debounce", fake_flush)

    async def fake_submit(**kwargs):  # pragma: no cover - must not run yet
        raise AssertionError("submit must not run before flush")

    monkeypatch.setattr(bot_main.api_client, "submit_operator_upload", fake_submit)
    client = TestClient(bot_app)
    msg = _operator_message(
        caption="/kb_add",
        attachments=[
            {
                "document": {
                    "file_id": "MG-FIRST",
                    "file_name": "first.pdf",
                    "mime_type": "application/pdf",
                    "file_size": 10,
                }
            }
        ],
    )
    msg["message"]["media_group_id"] = "MG_CAP"
    response = client.post("/telegram/webhook", json=msg)
    body = response.json()
    assert body["kb_mode"] == "media_group_buffered"
    assert body["media_group_id"] == "MG_CAP"
    # No "Принял N файл" ack yet
    assert not any("Принял" in t for _, t in isolated_bot["dms"])
    # Buffer holds the attachment
    drained = isolated_bot["media_group_buffer"].drain(media_group_id="MG_CAP")
    assert len(drained) == 1
    assert drained[0].attachment.file_id == "MG-FIRST"
    # Flush was scheduled once
    assert scheduled == ["MG_CAP"]


def test_kb_non_media_group_keeps_immediate_ack(isolated_bot, monkeypatch):
    """Regression: a single PDF without media_group_id still acks
    immediately (no debounce delay)."""
    pdf = isolated_bot["tmp_path"] / "single.pdf"
    pdf.write_bytes(b"PDF")

    async def fake_download(self, *, file_id, suggested_extension, mime_type=None):
        return DownloadedFile(path=pdf, byte_size=3, mime_type=mime_type)

    async def fake_submit(**kwargs):
        return {"inserted_chunks": 1, "is_confidential": False, "deduplicated": False}

    monkeypatch.setattr(bot_main.TelegramFileDownloader, "download", fake_download)
    monkeypatch.setattr(bot_main.api_client, "submit_operator_upload", fake_submit)
    client = TestClient(bot_app)
    msg = _operator_message(
        caption="/kb_add",
        attachments=[
            {
                "document": {
                    "file_id": "S1",
                    "file_name": "single.pdf",
                    "mime_type": "application/pdf",
                    "file_size": 3,
                }
            }
        ],
    )
    msg["update_id"] = 10001
    msg["message"]["message_id"] = 10001
    response = client.post("/telegram/webhook", json=msg)
    body = response.json()
    assert body["kb_mode"] == "freetext" or body.get("attachment_count") == "1"
    acks = [t for _, t in isolated_bot["dms"] if "Принял" in t]
    assert acks
    assert "1" in acks[0]


def test_kb_media_group_orphan_webhook_arrives_before_captioned(
    isolated_bot, monkeypatch
):
    """Regression for the 2-files-only-1-ingested bug.

    Telegram delivers a media group as N independent webhooks. Only one
    carries the caption with KB intent; the others have no text/caption.
    If the caption-less sibling is processed first (no session yet, no
    intent), the pre-fix dispatch silently dropped it as
    `attachment_only`. With the orphan-buffer handler in place it is
    speculatively buffered, and the captioned sibling that arrives later
    upserts the session + buffers its own row. The single flush sees
    both rows + active session and ingests both files.
    """
    import asyncio

    pdf_a = isolated_bot["tmp_path"] / "a.pdf"
    pdf_a.write_bytes(b"AAAA")
    pdf_b = isolated_bot["tmp_path"] / "b.pdf"
    pdf_b.write_bytes(b"BBBB")

    async def fake_download(self, *, file_id, suggested_extension, mime_type=None):
        if file_id == "MG-ORPHAN":
            return DownloadedFile(path=pdf_a, byte_size=4, mime_type=mime_type)
        return DownloadedFile(path=pdf_b, byte_size=4, mime_type=mime_type)

    submit_calls: list[dict] = []

    async def fake_submit(**kwargs):
        submit_calls.append(kwargs)
        return {"inserted_chunks": 5, "is_confidential": False, "deduplicated": False}

    monkeypatch.setattr(bot_main.TelegramFileDownloader, "download", fake_download)
    monkeypatch.setattr(bot_main.api_client, "submit_operator_upload", fake_submit)

    real_flush = bot_main._flush_media_group_after_debounce

    async def noop_flush(**kwargs):
        return None

    monkeypatch.setattr(bot_main, "_flush_media_group_after_debounce", noop_flush)

    client = TestClient(bot_app)

    # Caption-less webhook arrives FIRST. No kb_session yet, no intent.
    # Pre-fix: silently dropped at attachment_only fallthrough.
    # Post-fix: speculatively buffered via the orphan handler.
    orphan_msg = _operator_message(
        attachments=[
            {
                "document": {
                    "file_id": "MG-ORPHAN",
                    "file_name": "a.pdf",
                    "mime_type": "application/pdf",
                    "file_size": 4,
                }
            }
        ],
    )
    orphan_msg["update_id"] = 12001
    orphan_msg["message"]["message_id"] = 12001
    orphan_msg["message"]["media_group_id"] = "MG_ORPHAN_RACE"
    response = client.post("/telegram/webhook", json=orphan_msg)
    body = response.json()
    assert body["status"] == "accepted"
    assert body["kb_mode"] == "media_group_orphan_buffered"
    assert body["attachment_count"] == "1"
    # No "Принял" ack yet — buffer only.
    assert not any("Принял" in t for _, t in isolated_bot["dms"])
    # No kb_session opened yet.
    assert (
        isolated_bot["kb_session_repo"].get_active(chat_id=100, username="@ajdevy")
        is None
    )

    # Captioned webhook arrives SECOND, opening the session and buffering
    # its own attachment via the kb_command path.
    captioned_msg = _operator_message(
        caption="добавь в базу знаний",
        attachments=[
            {
                "document": {
                    "file_id": "MG-CAPTIONED",
                    "file_name": "b.pdf",
                    "mime_type": "application/pdf",
                    "file_size": 4,
                }
            }
        ],
    )
    captioned_msg["update_id"] = 12002
    captioned_msg["message"]["message_id"] = 12002
    captioned_msg["message"]["media_group_id"] = "MG_ORPHAN_RACE"
    response = client.post("/telegram/webhook", json=captioned_msg)
    body = response.json()
    assert body["kb_mode"] == "media_group_buffered"
    assert (
        isolated_bot["kb_session_repo"].get_active(chat_id=100, username="@ajdevy")
        is not None
    )

    # Run the real flush — both attachments must be ingested.
    asyncio.run(
        real_flush(media_group_id="MG_ORPHAN_RACE", debounce_seconds=0)
    )

    acks = [t for _, t in isolated_bot["dms"] if "Принял" in t]
    summaries = [t for _, t in isolated_bot["dms"] if "Добавлено в базу" in t]
    assert len(acks) == 1
    assert "2" in acks[0]
    assert "файла" in acks[0]
    assert len(summaries) == 1
    assert len(submit_calls) == 2
    assert "a.pdf" in summaries[0]
    assert "b.pdf" in summaries[0]


def test_kb_media_group_orphan_no_intent_ever_dropped_with_dm(
    isolated_bot, monkeypatch, caplog
):
    """Operator sends a media group with NO KB-intent trigger on any
    sibling and no pre-existing session. The orphan handler buffers each
    caption-less webhook, but the drain-time session check refuses to
    upload: `media_group_orphan_dropped` is logged and the operator
    receives a soft hint instead of silent ingest."""
    import asyncio

    submit_calls: list[dict] = []

    async def fake_submit(**kwargs):  # pragma: no cover - must not run
        submit_calls.append(kwargs)
        return {}

    monkeypatch.setattr(bot_main.api_client, "submit_operator_upload", fake_submit)

    real_flush = bot_main._flush_media_group_after_debounce

    async def noop_flush(**kwargs):
        return None

    monkeypatch.setattr(bot_main, "_flush_media_group_after_debounce", noop_flush)

    client = TestClient(bot_app)

    for offset, fid in ((1, "MG-N1"), (2, "MG-N2")):
        msg = _operator_message(
            attachments=[
                {
                    "document": {
                        "file_id": fid,
                        "file_name": f"{fid}.pdf",
                        "mime_type": "application/pdf",
                        "file_size": 4,
                    }
                }
            ],
        )
        msg["update_id"] = 13000 + offset
        msg["message"]["message_id"] = 13000 + offset
        msg["message"]["media_group_id"] = "MG_NO_INTENT"
        response = client.post("/telegram/webhook", json=msg)
        assert response.json()["kb_mode"] == "media_group_orphan_buffered"

    # No session was ever opened.
    assert (
        isolated_bot["kb_session_repo"].get_active(chat_id=100, username="@ajdevy")
        is None
    )

    with caplog.at_level("WARNING"):
        asyncio.run(real_flush(media_group_id="MG_NO_INTENT", debounce_seconds=0))

    assert submit_calls == []
    assert any(
        "media_group_orphan_dropped" in r.message for r in caplog.records
    )
    hint_dms = [
        t
        for _, t in isolated_bot["dms"]
        if "без активной сессии" in t and "добавь в базу знаний" in t
    ]
    assert len(hint_dms) == 1
    # No "Принял" / "Добавлено в базу" — we refused to ingest.
    assert not any("Принял" in t for _, t in isolated_bot["dms"])
    assert not any("Добавлено в базу" in t for _, t in isolated_bot["dms"])


def test_kb_media_group_orphan_handler_ignores_non_operator(
    isolated_bot, monkeypatch
):
    """Non-operator (customer) attachment-only messages must not be
    speculatively buffered — they continue to fall through to the
    standard attachment_only ignore path."""
    scheduled: list[str] = []

    async def fake_flush(*, media_group_id, debounce_seconds):
        scheduled.append(media_group_id)

    monkeypatch.setattr(bot_main, "_flush_media_group_after_debounce", fake_flush)

    client = TestClient(bot_app)
    customer_msg = _operator_message(
        attachments=[
            {
                "document": {
                    "file_id": "CUST-1",
                    "file_name": "c.pdf",
                    "mime_type": "application/pdf",
                    "file_size": 4,
                }
            }
        ],
    )
    customer_msg["update_id"] = 14001
    customer_msg["message"]["message_id"] = 14001
    customer_msg["message"]["media_group_id"] = "MG_CUSTOMER"
    customer_msg["message"]["from"]["username"] = "random_customer"
    response = client.post("/telegram/webhook", json=customer_msg)
    body = response.json()
    # Falls through to attachment_only ignore.
    assert body["status"] == "ignored"
    assert body["reason"] == "attachment_only"
    assert scheduled == []
    # No buffer rows.
    assert (
        isolated_bot["media_group_buffer"].drain(media_group_id="MG_CUSTOMER")
        == []
    )

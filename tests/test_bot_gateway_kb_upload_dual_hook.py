"""Bot-gateway dual hook: 12.05b materials analyzer + 12.05c services extractor.

After a successful KB ingest the bot fans out two parallel calls via
``asyncio.gather``:

1. ``POST /sales/materials/analyze-kb-file`` — client materials (12.05b).
2. ``POST /sales/services/extract-from-kb-file`` — services (12.05c).

Each may contribute zero or one line to the existing KB-upload ack.
When both have content, the materials line comes BEFORE the services
line (documented order). When only one has content, only that line
appears. When neither does, the ack stays bare.
"""

from __future__ import annotations

from typing import Any

import pytest
from fastapi.testclient import TestClient

from services.bot_gateway.app import main as bot_main
from services.bot_gateway.app.kb_session import OperatorKbSessionRepository
from services.bot_gateway.app.main import app as bot_app
from services.bot_gateway.app.media_group_buffer import MediaGroupBuffer
from services.bot_gateway.app.operator_files import OperatorFileRepository
from services.bot_gateway.app.telegram_file_download import DownloadedFile


class _StubHitlRepo:
    def get_runtime_config(self, key: str) -> str | None:
        return None

    def set_runtime_config(self, **kwargs: Any) -> None:  # pragma: no cover
        pass

    def list_all(self) -> list:  # pragma: no cover
        return []


@pytest.fixture
def isolated_bot(tmp_path, monkeypatch):
    monkeypatch.setattr(
        bot_main.settings, "persistence_db_path", str(tmp_path / "story.db")
    )
    monkeypatch.setattr(
        bot_main.settings, "hitl_ticket_db_path", str(tmp_path / "hitl.db")
    )
    monkeypatch.setattr(
        bot_main.settings,
        "operator_upload_storage_dir",
        str(tmp_path / "uploads"),
    )
    monkeypatch.setattr(bot_main.settings, "operator_upload_max_bytes", 1024)
    monkeypatch.setattr(bot_main.settings, "telegram_bot_token", "TKN")
    monkeypatch.setattr(
        bot_main.settings, "hitl_primary_operator_username", "@ajdevy"
    )
    monkeypatch.setattr(
        bot_main.settings, "internal_service_token", "bot-token-x"
    )
    monkeypatch.setattr(bot_main, "hitl_ticket_repository", _StubHitlRepo())
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
    return {"tmp_path": tmp_path, "dms": sent_dms}


def _operator_kb_payload(*, caption: str = "/kb_add") -> dict[str, Any]:
    return {
        "update_id": 1,
        "message": {
            "message_id": 1,
            "chat": {"id": 100},
            "from": {"id": 200, "username": "ajdevy"},
            "caption": caption,
            "document": {
                "file_id": "DOCXYZ",
                "file_name": "tours.pdf",
                "mime_type": "application/pdf",
                "file_size": 100,
            },
        },
    }


def _patch_download(monkeypatch, tmp_path) -> None:
    file_path = tmp_path / "tours.pdf"
    file_path.write_bytes(b"PDF")

    async def fake_download(self, *, file_id, suggested_extension, mime_type=None):
        return DownloadedFile(
            path=file_path, byte_size=3, mime_type=mime_type
        )

    monkeypatch.setattr(
        bot_main.TelegramFileDownloader, "download", fake_download, raising=False
    )


def _patch_submit(monkeypatch, *, is_confidential: bool = False) -> None:
    async def fake_submit(**kwargs):
        return {
            "inserted_chunks": 4,
            "is_confidential": is_confidential,
            "deduplicated": False,
            "candidate_id": 11,
            "project_id": 7,
        }

    monkeypatch.setattr(
        bot_main.api_client, "submit_operator_upload", fake_submit
    )


def test_both_hooks_contribute_lines_in_documented_order(
    isolated_bot, monkeypatch
) -> None:
    """Materials line FIRST, then services line — per the story rules."""
    _patch_download(monkeypatch, isolated_bot["tmp_path"])
    _patch_submit(monkeypatch)

    async def fake_analyze(**_kwargs):
        return {"registered": True, "material_id": 42, "reason": "ok"}

    async def fake_extract(**_kwargs):
        return {
            "added": [
                {"service_id": 1, "name": "Медовеевка Лайт"},
                {"service_id": 2, "name": "Каньонинг"},
            ],
            "skipped_existing": [],
            "reason": "ok",
        }

    monkeypatch.setattr(
        bot_main.api_client, "analyze_kb_material", fake_analyze, raising=False
    )
    monkeypatch.setattr(
        bot_main.api_client, "extract_kb_services", fake_extract, raising=False
    )

    response = TestClient(bot_app).post(
        "/telegram/webhook", json=_operator_kb_payload()
    )
    assert response.status_code == 200

    summary = [
        text for _, text in isolated_bot["dms"] if "Добавлено в базу" in text
    ][0]

    materials_line = "📎 Добавлен в материалы для клиентов (id=42)."
    services_line = "📦 Услуги добавлены: Медовеевка Лайт, Каньонинг."
    assert materials_line in summary
    assert services_line in summary
    # Materials line must precede services line per the story rules.
    assert summary.index(materials_line) < summary.index(services_line)


def test_only_services_line_when_materials_empty(
    isolated_bot, monkeypatch
) -> None:
    _patch_download(monkeypatch, isolated_bot["tmp_path"])
    _patch_submit(monkeypatch)

    async def fake_analyze(**_kwargs):
        return {
            "registered": False,
            "material_id": None,
            "reason": "internal invoice",
        }

    async def fake_extract(**_kwargs):
        return {
            "added": [{"service_id": 1, "name": "Каньонинг"}],
            "skipped_existing": [],
            "reason": "ok",
        }

    monkeypatch.setattr(
        bot_main.api_client, "analyze_kb_material", fake_analyze, raising=False
    )
    monkeypatch.setattr(
        bot_main.api_client, "extract_kb_services", fake_extract, raising=False
    )

    response = TestClient(bot_app).post(
        "/telegram/webhook", json=_operator_kb_payload()
    )
    assert response.status_code == 200

    summary = [
        text for _, text in isolated_bot["dms"] if "Добавлено в базу" in text
    ][0]
    assert "📎 Добавлен в материалы" not in summary
    assert "📦 Услуги добавлены: Каньонинг." in summary


def test_only_materials_line_when_services_empty(
    isolated_bot, monkeypatch
) -> None:
    _patch_download(monkeypatch, isolated_bot["tmp_path"])
    _patch_submit(monkeypatch)

    async def fake_analyze(**_kwargs):
        return {"registered": True, "material_id": 99, "reason": "ok"}

    async def fake_extract(**_kwargs):
        return {"added": [], "skipped_existing": [], "reason": "personal letter"}

    monkeypatch.setattr(
        bot_main.api_client, "analyze_kb_material", fake_analyze, raising=False
    )
    monkeypatch.setattr(
        bot_main.api_client, "extract_kb_services", fake_extract, raising=False
    )

    response = TestClient(bot_app).post(
        "/telegram/webhook", json=_operator_kb_payload()
    )
    assert response.status_code == 200

    summary = [
        text for _, text in isolated_bot["dms"] if "Добавлено в базу" in text
    ][0]
    assert "📎 Добавлен в материалы для клиентов (id=99)." in summary
    assert "📦 Услуги добавлены" not in summary


def test_both_empty_no_extra_lines(isolated_bot, monkeypatch) -> None:
    _patch_download(monkeypatch, isolated_bot["tmp_path"])
    _patch_submit(monkeypatch)

    async def fake_analyze(**_kwargs):
        return {"registered": False, "material_id": None, "reason": "x"}

    async def fake_extract(**_kwargs):
        return {"added": [], "skipped_existing": [], "reason": "x"}

    monkeypatch.setattr(
        bot_main.api_client, "analyze_kb_material", fake_analyze, raising=False
    )
    monkeypatch.setattr(
        bot_main.api_client, "extract_kb_services", fake_extract, raising=False
    )

    response = TestClient(bot_app).post(
        "/telegram/webhook", json=_operator_kb_payload()
    )
    assert response.status_code == 200

    summary = [
        text for _, text in isolated_bot["dms"] if "Добавлено в базу" in text
    ][0]
    assert "📎" not in summary
    assert "📦" not in summary


def test_skipped_existing_only_does_not_add_services_line(
    isolated_bot, monkeypatch
) -> None:
    """Re-uploading the same catalog yields ``added=[]`` and a non-empty
    ``skipped_existing`` — but the ack only shows newly-added names, so the
    📦 line MUST NOT appear.
    """
    _patch_download(monkeypatch, isolated_bot["tmp_path"])
    _patch_submit(monkeypatch)

    async def fake_analyze(**_kwargs):
        return {"registered": False, "material_id": None, "reason": "x"}

    async def fake_extract(**_kwargs):
        return {
            "added": [],
            "skipped_existing": ["Медовеевка Лайт", "Каньонинг"],
            "reason": "ok",
        }

    monkeypatch.setattr(
        bot_main.api_client, "analyze_kb_material", fake_analyze, raising=False
    )
    monkeypatch.setattr(
        bot_main.api_client, "extract_kb_services", fake_extract, raising=False
    )

    response = TestClient(bot_app).post(
        "/telegram/webhook", json=_operator_kb_payload()
    )
    assert response.status_code == 200

    summary = [
        text for _, text in isolated_bot["dms"] if "Добавлено в базу" in text
    ][0]
    assert "📦 Услуги добавлены" not in summary
